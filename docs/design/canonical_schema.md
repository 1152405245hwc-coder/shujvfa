# V0.1 Canonical Schema

状态：与 `docs/legal/review_protocol.md` 配套  
版本：0.1.0  
实现目标：Pydantic v2，金额只使用 `Decimal`

## 1. 标识与不可变性

所有实体使用 UUID 或带前缀的稳定字符串 ID。ID 一旦写入证据或日志不得复用。
原始材料、CSV 原始行和规范化交易均保留创建后不可变版本；纠错通过新版本和来源关系完成。

推荐前缀：`CASE-`、`CLM-`、`PER-`、`ACC-`、`TX-`、`EVI-`、`LNK-`、`DEC-`、`TASK-`。

## 2. Value Objects

### SourceLocator

描述某个事实在原始材料中的位置：

```text
evidence_id: str
locator_type: enum(text_span | csv_row)
start_offset: int | None
end_offset: int | None
line_number: int | None
label: str | None
```

约束：`text_span` 必须有起止字符偏移；`csv_row` 必须有从1开始的行号；两者不能同时缺失。

### ObjectRef

用于 EvidenceLink 的跨对象引用：

```text
object_type: enum(case | claim | person | account | transaction | evidence | decision)
object_id: str
```

## 3. 实体

### Case

```text
id: str
title: str
offense_category: literal["fraud"]
status: enum(draft | processing | review_required | review_complete)
created_at: datetime
updated_at: datetime
claim_ids: list[str]
evidence_ids: list[str]
```

V0.1 只允许 `offense_category=fraud`。案件状态不得在存在阻断校验错误时成为 `review_complete`。

### Person

```text
id: str
display_name: str
role: enum(defendant | victim | recipient | other)
aliases: list[str]
source_locator_ids: list[str]
```

姓名只用于匹配提示，不是身份认定。身份证号、完整银行卡号等敏感字段不进入 V0.1 模型。

### Account

```text
id: str
masked_number: str
institution: str | None
holder_person_id: str | None
source_locator_ids: list[str]
```

系统内部可以保存加密或哈希后的比对值，但 UI、日志和导出默认只显示末四位。

### EvidenceItem

```text
id: str
evidence_type: enum(indictment | victim_statement | bank_csv | manual_note)
filename: str
sha256: str
mime_type: str
text: str | None
row_count: int | None
created_at: datetime
```

原始文件哈希用于证明审查输入没有被静默替换。`manual_note` 不得冒充原始证据。

### Claim

对应产品层的 `PaymentClaim`：

```text
id: str
case_id: str
victim_person_id: str
victim_name: str
victim_account: str | None
alleged_recipient_person_id: str | None
alleged_recipient_account_id: str | None
alleged_recipient_name: str | None
alleged_recipient_account: str | None
claimed_amount: Decimal
currency: literal["CNY"]
time_start: date
time_end: date
payment_method: literal["bank_transfer"]
source_locator_ids: list[str]
extraction_status: enum(model_extracted | human_confirmed | human_corrected | extraction_review_required)
```

`claimed_amount` 是材料中的指控金额，不是系统认定金额。必须大于零，且 `time_start <= time_end`。

### Transaction

```text
id: str
case_id: str
transaction_id: str
date: date
time: time | None
payer_person_id: str | None
payer_account_id: str | None
payee_person_id: str | None
payee_account_id: str | None
payer_name: str | None
payer_account: str | None
payee_name: str | None
payee_account: str | None
amount: Decimal
currency: literal["CNY"]
remark: str | None
source_evidence_id: str
source_row: int
dedup_fingerprint: str
```

`amount > 0`。`transaction_id` 在案件内应唯一；重复行仍记录为导入异常，不直接删除原始记录。

### EvidenceLink

描述对象与证据位置的关系：

```text
id: str
from_ref: ObjectRef
to_evidence_id: str
locator: SourceLocator
relation: enum(supports | contradicts | mentions | derived_from)
note: str | None
```

`to_evidence_id` 必须与 `locator.evidence_id` 相同，且两端对象必须存在。

### ReviewDecision

既表示 Claim 汇总决定，也保留人工逐笔决定：

```text
id: str
case_id: str
claim_id: str
version: int
decision_type: enum(system_proposed | human_confirmed | human_rejected)
supersedes_decision_id: str | None
status: enum(conflicting | pending_review | fully_corroborated | partially_corroborated | unsupported)
included_transaction_ids: list[str]
excluded_transaction_ids: list[str]
disputed_transaction_ids: list[str]
covered_amount: Decimal
uncovered_amount: Decimal
disputed_amount: Decimal
reason_codes: list[str]
reviewer: str | None
reviewed_at: datetime | None
note: str | None
verification_error_codes: list[str]
```

系统建议必须为 `system_proposed` 且版本为1；人工确认必须新建版本、指向上一版本并填写复核人和时间。
任何 `verification_error_codes` 非空的决定都不得为 `human_confirmed`。

## 4. 候选匹配（派生对象）

`CandidateMatch` 不属于八个持久化核心实体，但必须可序列化：

```text
claim_id: str
transaction_id: str
matched_rules: list[str]
payer_match: enum(exact | fuzzy | mismatch | unavailable)
payee_match: enum(exact | fuzzy | mismatch | unavailable)
amount_match: enum(exact | partial | exceeds | mismatch)
date_match: enum(exact | window | mismatch)
blocking_conflict: bool
risk_codes: list[str]
```

它只能驱动人工复核界面，不能直接产生 `covered_amount` 或 `human_confirmed`。

## 5. 跨对象不变量

实现必须在服务层或 Verification Engine 中强制检查：

1. 所有引用 ID 存在且类型正确。
2. 金额序列化、比较和求和均使用 `Decimal`。
3. `covered_amount` 等于唯一 `included_transaction_ids` 的交易金额之和。
4. `uncovered_amount = max(claimed_amount - covered_amount, 0)`。
5. `disputed_amount` 等于唯一争议交易金额之和。
6. included、excluded、disputed 三个集合互不相交。
7. 同一交易不得被同一案件的两个 Claim 同时纳入。
8. 每个 Claim 至少有一个 EvidenceLink，且每个 Transaction 有来源证据和行号。
9. 决定版本连续递增，历史版本不可覆盖。
10. `HUMAN_CONFIRMED` 只能由人工操作创建，不能由 LLM 或批处理脚本创建。

## 6. 序列化约定

JSON 中 Decimal 使用字符串，例如 `"50000.00"`；日期使用 ISO-8601；时间戳使用带时区的 ISO-8601。
导出 CSV 时金额保留两位小数。所有导出包含 schema_version、case_id、generated_at 和 disclaimer。
