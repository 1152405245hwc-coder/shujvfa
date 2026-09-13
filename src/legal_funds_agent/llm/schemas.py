"""语义提取契约注册表。

所有会调用大模型的语义步骤都在这里登记：系统提示词、结果顶层键、以及把模型返回的
原始 JSON 归一化成流程可消费结构的函数。

设计约束（见 PROJECT_MUST_READ.md）：

- 模型只做「非结构化文本 → 结构化事实」的翻译，不产出任何法律判断或金额结论；
- 每条事实都必须携带 ``source_text``，由流程做原文唯一性校验；
- 不同 Provider（DeepSeek / OpenAI / Mock）共用同一份契约，避免提示词漂移。

新增一个语义步骤时，只需要：

1. 在这里加一个 ``SchemaSpec``；
2. 在 ``llm/mock_provider.py`` 里给它一个离线实现（保证 Mock 默认路径可回归）；
3. 在调用侧用 ``generate_structured(text=..., schema_name=SPEC.name)``。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable


SCHEMA_PAYMENT_CLAIM = "payment_claim_v0.1"
SCHEMA_STATEMENT_FACT = "statement_fact_v0.1"
SCHEMA_CLAIM_AUDIT = "claim_audit_v0.1"
SCHEMA_PARTY_ALIAS = "party_alias_v0.1"
SCHEMA_EVIDENCE_CONFLICT = "evidence_conflict_v0.1"
SCHEMA_INVESTIGATION_NOTE = "investigation_note_v0.1"
SCHEMA_CASE_NARRATIVE = "case_narrative_v0.1"


# NOTE: this string is byte-identical to the prompt committed with the frozen V0.1
# DeepSeek baseline. Do not reflow it without re-recording the baseline.
PAYMENT_CLAIM_PROMPT = """你是刑事案件材料的结构化信息提取工具。只提取原文明确陈述的付款主张，
不得判断是否构成犯罪，不得认定犯罪金额，不得补充原文没有的事实。返回严格 JSON 对象，
格式为 {"claims":[{"victim_name":"","alleged_recipient_name":"","claimed_amount":"0.00",
"time_start":"YYYY-MM-DD","time_end":"YYYY-MM-DD","source_text":"","start_offset":0,"end_offset":0}]}。
字符偏移以输入文本 Python 字符索引为准。"""


STATEMENT_FACT_PROMPT = """你是刑事案件材料的结构化信息提取工具。只提取被害人在陈述中明确陈述的
一笔付款事实，不得判断是否构成犯罪，不得认定犯罪金额，不得补充原文没有的事实，
不得把多笔付款合并或换算成一个数字。返回严格 JSON 对象，格式为
{"statement_fact":{"victim_name":"","recipient_name":"","amount":"0.00",
"payment_date":"YYYY-MM-DD","source_text":"","start_offset":0,"end_offset":0}}。
如果陈述中没有明确陈述的付款事实，返回 {"statement_fact":null}。
字符偏移以输入文本 Python 字符索引为准。"""


CLAIM_AUDIT_PROMPT = """你是刑事案件材料的漏项复核工具。输入是一个 JSON 对象，包含
"indictment"（起诉书原文）和 "extracted_claims"（已经从该原文提取出的付款主张列表）。
请找出原文中明确陈述、但未被 extracted_claims 覆盖的付款主张。

严格要求：只补充原文明确陈述的事实；不得推导、不得换算、不得把多笔合并成一笔、
不得重复报告 extracted_claims 中已有的主张。返回严格 JSON 对象，格式为
{"missing_claims":[{"victim_name":"","alleged_recipient_name":"","claimed_amount":"0.00",
"time_start":"YYYY-MM-DD","time_end":"YYYY-MM-DD","source_text":"","start_offset":0,"end_offset":0}]}。
source_text 必须逐字取自 indictment。若没有遗漏，返回 {"missing_claims":[]}。
字符偏移以 indictment 的 Python 字符索引为准。"""


PARTY_ALIAS_PROMPT = """你是刑事案件材料的当事人同一性核对工具。输入是一个 JSON 对象，包含
"party_names"（已从材料中提取到的全部人名或称谓）和 "materials"（材料原文列表）。
请找出其中指向同一个人的不同称谓（例如同一人的全名、简称、化名、代称）。

严格要求：只在材料明确支持时才提出合并；不得仅凭姓氏相同、姓名相似或常识推测；
不得合并不同的人；不得因为两个名字同时出现在一段话里就认定是同一人。
**每个别名必须单独给出依据与单独的置信度**，不得用一个别名的高置信度去带高另一个别名。
凡是靠推断而非原文明确陈述得出的，confidence 必须标为 low，并在 reason 中写明是推断。
每条依据的 source_text 必须逐字取自 materials 中的原文。返回严格 JSON 对象，格式为
{"alias_groups":[{"canonical_name":"","aliases":[{"name":"","confidence":"high|medium|low",
"evidence":[{"source_text":"","reason":""}]}]}]}。
若不存在有材料依据的同一性，返回 {"alias_groups":[]}。"""


EVIDENCE_CONFLICT_PROMPT = """你是刑事案件材料的事实比对工具。输入是一个 JSON 对象，包含
"facts"（已经由确定性代码算出的资金事实列表，每条有 fact_id）和 "materials"（材料原文列表）。

对每条 fact，找出 materials 中**与该事实相关的陈述**，并标注该陈述的立场：
supports（印证）、contradicts（矛盾）、qualifies（限定或补充条件）。
不得判断是否构成犯罪，不得认定犯罪金额，不得评价证据证明力，不得补充原文没有的内容。

严格要求：每条陈述的 source_text 必须逐字取自 materials 中的原文；
只报告确有材料依据的比对，没有材料谈及的 fact 不要出现在结果里。
返回严格 JSON 对象，格式为
{"conflicts":[{"fact_id":"","title":"","priority":"高|中|低",
"positions":[{"source":"材料名称","source_text":"","stance":"supports|contradicts|qualifies"}],
"conclusion":"","next_action":""}]}。
conclusion 只能描述材料之间的一致或差异，next_action 只能是建议的回查动作。"""


INVESTIGATION_NOTE_PROMPT = """你是刑事案件审查底稿的措辞助手。输入是一个 JSON 对象，包含 "items"
（已经由确定性代码生成的回查事项列表），每项有 item_id、category、priority、target、facts、
current_suggestion 与 current_next_action。

请为每一项重写 suggestion（建议）与 next_action（下一步动作），使其更贴合该案具体事实、更便于执行。
不得新增或删除事项，不得改变 item_id，不得改变事项的类型与优先级。

**绝对禁止引入输入中没有的数字。** 不得做任何加减乘除，不得换算单位，不得估算，不得补充金额、
笔数、日期或比例。只能原样使用 facts、current_suggestion、current_next_action 中已经出现的数字。

不得判断是否构成犯罪，不得认定犯罪金额，不得评价证据证明力，不得补充材料中没有的事实。
返回严格 JSON 对象，格式为 {"notes":[{"item_id":"","suggestion":"","next_action":""}]}。
若某项无需改写，可以不返回该项。"""


CASE_NARRATIVE_PROMPT = """你是刑事案件资金证据审查底稿的叙述助手。输入是一个 JSON 对象 "facts"，
包含已经由确定性代码算出的全案事实：主张汇总、已覆盖与未覆盖金额、疑似转回流水、
逐笔复核处置、证据冲突焦点、回查事项统计。

请据此写一份便于阅读的底稿摘要，分为三节：
1.「资金证据覆盖情况」——概括指控总额、已确证覆盖、未覆盖缺口、疑似转回参考值；
2.「资金流转与逐笔处置」——概括已复核流水的处置结构（纳入/排除/争议各多少笔、涉及哪些账户）；
3.「争议焦点与待办」——概括当前存在哪些冲突焦点和待核查事项。

严格要求：
- **不得引入 facts 中没有的数字。** 不得做任何加减乘除，不得换算单位，不得估算，
  不得补充金额、笔数、日期或比例。只能原样使用 facts 中已经出现的数字。
- 不得判断是否构成犯罪，不得认定犯罪金额，不得评价证据证明力，不得给出量刑意见。
- 不得补充 facts 中没有的事实。措辞应说明"材料之间的对应与差异"，而非"案件事实成立"。
- 涉及具体流水或主张时，保留其编号（如流水号、主张编号），便于回查。
- 涉及处置状态、复核状态等代码时，一律使用 facts 中 code_labels 给出的中文表述，
  不要直接输出英文枚举（如 DISPUTED、PENDING_REVIEW）。

返回严格 JSON 对象，格式为
{"sections":[{"heading":"","body":""}]}。"""


def _clean_amount(value: Any) -> str:    return str(value if value not in (None, "") else "0.00").replace(",", "").strip()


def _clean_date(value: Any) -> str | None:
    if value in (None, ""):
        return None
    cleaned = str(value).strip()
    return cleaned if cleaned else None


def _clean_claim(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "victim_name": str(row.get("victim_name") or "").strip(),
        "alleged_recipient_name": str(row.get("alleged_recipient_name") or "").strip() or None,
        "claimed_amount": _clean_amount(row.get("claimed_amount")),
        "time_start": _clean_date(row.get("time_start")),
        "time_end": _clean_date(row.get("time_end")),
        "source_text": str(row.get("source_text") or "").strip(),
        "start_offset": int(row.get("start_offset") or 0),
        "end_offset": int(row.get("end_offset") or 0),
    }


def normalize_payment_claims(payload: dict[str, Any]) -> list[dict[str, Any]]:
    claims = payload["claims"]
    if not isinstance(claims, list):
        raise ValueError("claims must be a list")
    return [_clean_claim(row) for row in claims if isinstance(row, dict)]


def normalize_statement_fact(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """A statement carries at most one payment fact; ``null`` means "none found"."""
    fact = payload.get("statement_fact")
    if fact is None:
        return []
    if not isinstance(fact, dict):
        raise ValueError("statement_fact must be an object or null")
    return [{
        "victim_name": str(fact.get("victim_name") or "").strip(),
        "recipient_name": str(fact.get("recipient_name") or "").strip() or None,
        "amount": _clean_amount(fact.get("amount")),
        "payment_date": str(fact.get("payment_date") or "").strip(),
        "source_text": str(fact.get("source_text") or "").strip(),
        "start_offset": int(fact.get("start_offset") or 0),
        "end_offset": int(fact.get("end_offset") or 0),
    }]


def normalize_missing_claims(payload: dict[str, Any]) -> list[dict[str, Any]]:
    missing = payload["missing_claims"]
    if not isinstance(missing, list):
        raise ValueError("missing_claims must be a list")
    return [_clean_claim(row) for row in missing if isinstance(row, dict)]


def normalize_alias_groups(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize proposed party-identity groups. Nothing here is auto-applied.

    Aliases stay individually addressable — each carries its own confidence and its own
    evidence — so a reviewer can confirm a directly-attested alias without also
    accepting an inferred one that happens to sit in the same group.
    """
    groups = payload["alias_groups"]
    if not isinstance(groups, list):
        raise ValueError("alias_groups must be a list")
    normalized: list[dict[str, Any]] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        canonical = str(group.get("canonical_name") or "").strip()
        if not canonical:
            continue
        aliases: list[dict[str, Any]] = []
        for alias in group.get("aliases") or []:
            if isinstance(alias, str):
                name, confidence, evidence = alias.strip(), "low", []
            elif isinstance(alias, dict):
                name = str(alias.get("name") or "").strip()
                confidence = str(alias.get("confidence") or "low").strip().lower()
                evidence = [
                    {
                        "source_text": str(item.get("source_text") or "").strip(),
                        "reason": str(item.get("reason") or "").strip(),
                    }
                    for item in (alias.get("evidence") or [])
                    if isinstance(item, dict) and str(item.get("source_text") or "").strip()
                ]
            else:
                continue
            if not name or name == canonical:
                continue
            aliases.append({
                "name": name,
                "confidence": confidence if confidence in {"high", "medium", "low"} else "low",
                "evidence": evidence,
            })
        if not aliases:
            continue
        normalized.append({"canonical_name": canonical, "aliases": aliases})
    return normalized


def normalize_conflicts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize proposed fact/materials comparisons. The caller re-verifies quotes."""
    conflicts = payload["conflicts"]
    if not isinstance(conflicts, list):
        raise ValueError("conflicts must be a list")
    normalized: list[dict[str, Any]] = []
    for conflict in conflicts:
        if not isinstance(conflict, dict):
            continue
        fact_id = str(conflict.get("fact_id") or "").strip()
        if not fact_id:
            continue
        positions: list[dict[str, Any]] = []
        for position in conflict.get("positions") or []:
            if not isinstance(position, dict):
                continue
            source_text = str(position.get("source_text") or "").strip()
            if not source_text:
                continue
            stance = str(position.get("stance") or "qualifies").strip().lower()
            positions.append({
                "source": str(position.get("source") or "未标注材料").strip(),
                "source_text": source_text,
                "stance": stance if stance in {"supports", "contradicts", "qualifies"} else "qualifies",
            })
        if not positions:
            continue
        priority = str(conflict.get("priority") or "中").strip()
        normalized.append({
            "fact_id": fact_id,
            "title": str(conflict.get("title") or "").strip(),
            "priority": priority if priority in {"高", "中", "低"} else "中",
            "positions": positions,
            "conclusion": str(conflict.get("conclusion") or "").strip(),
            "next_action": str(conflict.get("next_action") or "").strip(),
        })
    return normalized


def build_evidence_conflict_input(facts: list[dict[str, Any]],
                                  materials: list[dict[str, str]]) -> str:
    return json.dumps({"facts": facts, "materials": materials}, ensure_ascii=False)


def normalize_investigation_notes(payload: dict[str, Any]) -> list[dict[str, Any]]:
    notes = payload["notes"]
    if not isinstance(notes, list):
        raise ValueError("notes must be a list")
    normalized: list[dict[str, Any]] = []
    for note in notes:
        if not isinstance(note, dict):
            continue
        item_id = str(note.get("item_id") or "").strip()
        suggestion = str(note.get("suggestion") or "").strip()
        next_action = str(note.get("next_action") or "").strip()
        if not item_id or not (suggestion or next_action):
            continue
        normalized.append({
            "item_id": item_id,
            "suggestion": suggestion,
            "next_action": next_action,
        })
    return normalized


def build_investigation_note_input(items: list[dict[str, Any]]) -> str:
    return json.dumps({"items": items}, ensure_ascii=False)


def normalize_narrative_sections(payload: dict[str, Any]) -> list[dict[str, Any]]:
    sections = payload["sections"]
    if not isinstance(sections, list):
        raise ValueError("sections must be a list")
    normalized: list[dict[str, Any]] = []
    for section in sections:
        if not isinstance(section, dict):
            continue
        heading = str(section.get("heading") or "").strip()
        body = str(section.get("body") or "").strip()
        if not body:
            continue
        normalized.append({"heading": heading or "摘要", "body": body})
    return normalized


def build_case_narrative_input(facts: dict[str, Any]) -> str:
    return json.dumps({"facts": facts}, ensure_ascii=False)


@dataclass(frozen=True)
class SchemaSpec:
    name: str
    system_prompt: str
    payload_key: str
    normalizer: Callable[[dict[str, Any]], list[dict[str, Any]]]


SCHEMAS: dict[str, SchemaSpec] = {
    SCHEMA_PAYMENT_CLAIM: SchemaSpec(
        name=SCHEMA_PAYMENT_CLAIM,
        system_prompt=PAYMENT_CLAIM_PROMPT,
        payload_key="claims",
        normalizer=normalize_payment_claims,
    ),
    SCHEMA_STATEMENT_FACT: SchemaSpec(
        name=SCHEMA_STATEMENT_FACT,
        system_prompt=STATEMENT_FACT_PROMPT,
        payload_key="statement_fact",
        normalizer=normalize_statement_fact,
    ),
    SCHEMA_CLAIM_AUDIT: SchemaSpec(
        name=SCHEMA_CLAIM_AUDIT,
        system_prompt=CLAIM_AUDIT_PROMPT,
        payload_key="missing_claims",
        normalizer=normalize_missing_claims,
    ),
    SCHEMA_PARTY_ALIAS: SchemaSpec(
        name=SCHEMA_PARTY_ALIAS,
        system_prompt=PARTY_ALIAS_PROMPT,
        payload_key="alias_groups",
        normalizer=normalize_alias_groups,
    ),
    SCHEMA_EVIDENCE_CONFLICT: SchemaSpec(
        name=SCHEMA_EVIDENCE_CONFLICT,
        system_prompt=EVIDENCE_CONFLICT_PROMPT,
        payload_key="conflicts",
        normalizer=normalize_conflicts,
    ),
    SCHEMA_INVESTIGATION_NOTE: SchemaSpec(
        name=SCHEMA_INVESTIGATION_NOTE,
        system_prompt=INVESTIGATION_NOTE_PROMPT,
        payload_key="notes",
        normalizer=normalize_investigation_notes,
    ),
    SCHEMA_CASE_NARRATIVE: SchemaSpec(
        name=SCHEMA_CASE_NARRATIVE,
        system_prompt=CASE_NARRATIVE_PROMPT,
        payload_key="sections",
        normalizer=normalize_narrative_sections,
    ),
}


def build_party_alias_input(party_names: list[str], materials: list[dict[str, str]]) -> str:
    """Pack observed names plus the source materials into the single ``text`` channel."""
    return json.dumps(
        {"party_names": sorted({str(name) for name in party_names if str(name).strip()}),
         "materials": materials},
        ensure_ascii=False,
    )


def get_schema(name: str) -> SchemaSpec:
    try:
        return SCHEMAS[name]
    except KeyError as exc:
        raise ValueError(f"unsupported schema: {name}") from exc


def supports_schema(provider: Any, name: str) -> bool:
    """Whether a provider advertises a schema.

    A provider that does not declare ``supported_schemas`` is given the benefit of the
    doubt: the caller tries the call and falls back if it fails. This keeps third-party
    or test providers working without having to know about every schema.
    """
    declared = getattr(provider, "supported_schemas", None)
    if declared is None:
        return True
    return name in declared


def build_claim_audit_input(indictment_text: str, extracted_claims: list[dict[str, Any]]) -> str:
    """Pack both materials into the single ``text`` channel the provider protocol exposes."""
    return json.dumps(
        {"indictment": indictment_text, "extracted_claims": extracted_claims},
        ensure_ascii=False,
    )
