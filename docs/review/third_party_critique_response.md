# 第三方评议核对回复（勘误）

日期：2026-09-13
对象：《项目比赛缺陷分析报告》（比赛视角缺陷分析）
方法：对评议逐条核对源码、样例数据、文档与 git 状态（探查代理三路并行 + 关键结论人工抽查实测）。结论分三类：**评议有误**、**定性需收窄**、**属实并已修复**。

---

## 一、评议与事实不符的 7 处

### 1. "每案 input_tokens 恒为 161" 作为 manifest 字段 —— 错误

评议称 `manifest.json` 里每案 `input_tokens` 恒为 161，并以此为"起诉书逐字节相同"的佐证。

- `sample_data/gold_cases/manifest.json` **不含** `input_tokens` 字段；每案只有 `id/title/expected_candidate_ids/review_actions/expected`。
- 161 是运行时从审计事件采集、写入**基线文件**的（仅主张提取口径，`docs/evaluation/baseline_deepseek_v0.2.json`）；v0.2 全链路口径为 3315/5 案。且 `docs/evaluation/README.md` 已记录该数字从 158→161 的变化系**服务端分词器漂移**所致，与"文本相同"无关。
- 五案起诉书确实相同（见第二节说明），但该论据本身不成立。

### 2. "最后实质提交停在 2026-08-28 的 V0.1" —— 错误

`git log` 最近提交为 **2026-09-09**（`a8410e9 docs: 日期窗口参数化说明…`，同日另有 `9616d28`、`66bcfcfc` 两个提交）。
"V0.2 语义层未入库"的判断方向正确（见下），但"最后提交日期"这一事实错误。

### 3. "run_checks.ps1 不检查 pytest 退出码，测试全挂仍打印自测完成" —— 错误

脚本首行即 `$ErrorActionPreference = "Stop"`，PowerShell 对外部命令非零退出码会抛异常终止，**pytest 全挂时脚本不会打印"自测完成"**。
真实问题仅为：没有显式检查 `$LASTEXITCODE`、失败时无中文摘要。已按后者修复。

### 4. "_mask_account 四处重复" —— 数量错误

实际为 **3 处** `_mask_account`（`persistence/repository.py:12`、`services/report_service.py:15`、`services/case_report_service.py:62`），另有 2 处功能相近但名为 `_mask`（`ui/streamlit_app.py:874`、`services/topology_service.py:16`）。重复属实（代码卫生问题），数量夸大。

### 5. 报告指纹"依赖 dict 插入顺序——同一内容可产生不同指纹" —— 机制描述错误

`case_report_service.py` 的指纹计算使用 `json.dumps(..., sort_keys=True)`，**不存在**插入顺序导致同一内容不同指纹的问题。
该指控中"无签名、哈希内容不含流水与清单、第三方无法独立验证"三点**成立**，已如实承认（见第三节）。

### 6. "扫描 PDF 超 50 页静默截断"归位于 file_parsers.py —— 位置错误

50 页上限在 `parsers/ocr_service.py:46,59` 的 `extract_text_from_scanned_pdf`（**实验性 OCR 路径**，依赖可选组件，主链文本型 PDF 用 pypdf、无页数限制）。`file_parsers.py` 内无此逻辑。

### 7. "再用默认编号上传评委自备材料，签署时报 immutable claim 错误，流程走死" —— 触发环节错误

`immutable claim already exists` 由 `repository.py:30` 在**人工核准 claim 写入 SQLite 时**抛出，**不在上传材料时**。上传与解析本身不报错；且这是 repository 刻意的唯一性约束（同 ID 不同内容禁止静默覆盖），并非偶然缺陷。真实问题（默认编号可撞库、英文错误直出）成立，已修复：UI 自动建议空闲编号 + 中文提示。

---

## 二、定性成立但需要收窄的 4 处

### 1. "评测 5/5、45/45 是循环论证" —— 部分成立

- **成立部分**：每案 9 项检查中，`human_status / covered_amount / uncovered_amount / disputed_amount` 4 项的输入（`review_actions`）与期望（`expected`）同出自 manifest，属确定性回放校验，不直接测模型能力。合计 20/45 项。
- **被忽略的部分**：另 5 项（`candidate_ids` 候选流水识别、`system_status`、`risk_codes` 风险码、`duplicate_group_count` 判重、`source_reference` 来源定位）来自模型提取 + 确定性代码，合计 25/45 项，与模型能力直接相关。
- **被忽略的上下文**：默认 provider 即 mock（`llm/factory.py:11`，刻意设计的离线回归路径）；`docs/evaluation/README.md`（七、表述边界）已声明 gold cases 为内部构造回归集、"不得表述为独立盲测准确率"；评测还包含**内部留出集**（`sample_data/holdout_cases/`，评议全文未提）。
- **已做改进**：`docs/evaluation/README.md` 新增"检查项构成"小节，把上述口径写成明文，答辩可直接引用。

### 2. "演示主打功能（冲突矩阵）是写死的话术" —— 部分成立

- **成立**：`evidence_conflict_service.py` 的 `showcase_conflict_matrix` 对 GOLD_CASE_001 的四条矩阵标题/结论/建议确为硬编码，UI 按 `case_id == "GOLD_CASE_001"` 路由（`streamlit_app.py` `_conflict_matrix_for`）。
- **被忽略的部分**：仅 GOLD_CASE_001 走 showcase 分支；**其他案件走真实通用实现** `build_evidence_conflict_matrix`（确定性找出"收款但非起诉书指称收款对象"的第三方账户并标记代收代转，可选模型做引文配对）。showcase 中的金额也由实际流水汇总，并非全假。
- 定性建议：向评委表述为"演示案件配置了 showcase 剧本；通用路径已可用，当前覆盖第三方账户类冲突"。

### 3. "不可篡改审计名不副实" —— 对 UI 文案成立，对设计文档不成立

- 设计文档口径克制：README 仅承诺"不可静默覆盖的 SQLite 保存"；`docs/security/data_handling.md:19-20` 明确声明 V0.1 **未实现**威胁建模、访问控制、加密存储。即文档从未承诺密码学不可篡改。
- 过强的是 **UI 文案**（"不可篡改电子签署"等 3 处）：已收敛为与应用层能力一致的表述。
- 评议指出的真实缺陷全部成立并已修复：audit_events 无哈希链（设计选择，已在文档声明边界）、`repository.py` 读取审计事件时对缺失时间戳补 `datetime.now()`（**读路径伪造审计时间，硬伤**）、报告指纹无签名且不含流水清单（承认，作为已知限制列入文档）。

### 4. "HTML 注入未转义" —— 范围夸大

1273-1554 行渲染区约 10 处 `unsafe_allow_html=True` 中，masthead、stat strip、交易卡片、主张列表、证据卡片约 6-7 处未转义（已修复）；但 `render_source_quote`、`render_review_issue` **已**做 `html.escape`。并非"多处原样插入"的全部。

---

## 三、评议属实：已修复清单

### P0 数据真实性与审计可信

| 缺陷（评议位置） | 修复 |
|---|---|
| `schemas.py` 缺失日期补 "2026-01-01" | 置 None + 校验提示；下游时间窗口按"无约束"处理 |
| `mock_provider.py` 正则照抄 GOLD_CASE_001 原句、兜底写死 2025-03-12/12-23 | 通用金额/日期解析；解析不到置 None，**不再编造** |
| `ocr_service.py` 金额正则把年份 2026 当金额 | 候选提取加年份排除（有 ¥/金额/元 上下文仍匹配） |
| `file_parsers.py` 截图合成补 "2026-03-15"、付款人硬编码"被害人" | 缺失置空，宁缺不假 |
| `repository.py` 读审计事件补 `datetime.now()` | 缺失保留 None，**读路径不再伪造审计时间** |
| 防伪/防幻觉校验不认大写中文数字（"壹佰贰拾伍万元"） | 新增 `chinese_numerals.py` 通用解析，守卫与叙述层均接入 |
| PARTIAL 判定无下限（0.01 元也算部分覆盖） | 加下限（<1% 且 <100 元不视为部分覆盖） |
| 判重 key 含时间字段，镜像流水双重计入 | key 归一化为（日期, 金额, 收付方） |
| 案级 system_risks 套用所有主张，跨受害人污染 | 陈述冲突按各主张自身 victim 计算 |

### P1 演示稳定性与数据完整性

| 缺陷 | 修复 |
|---|---|
| GBK 银行 CSV 直接崩溃、英文异常 | utf-8-sig → gb18030 回退；失败抛中文错误 |
| 解析失败行静默丢弃 | 按原因计数，UI 显示"N 行未导入"警告 |
| 恢复历史案件跨案残留（证人证言/选中卡片） | 恢复时清理 supplementary_documents 与全部 selection echo 键 |
| 默认 CASE-0001 撞库走死、英文错误 | 自动建议空闲编号 + 中文提示 |
| OpenAI 无重试 / DeepSeek 对 4xx 盲目重试 | 统一：仅网络错误与 429/5xx 重试 |
| SQLite 无 WAL/busy_timeout、每次连接跑 DDL | WAL + busy_timeout；DDL 仅初始化时执行 |
| HTML 多处未转义 | 全部用户数据插值补 `html.escape` |
| 批量按钮盖不掉手动编辑行 | 批量操作后重置 data_editor 编辑态 |
| 快捷方式 bat 无 BOM 乱码 | 转带 BOM UTF-8 并实测通过 |
| 关闭脚本 taskkill /FI 过滤器语法错误（实测报错） | 改用 PowerShell 按命令行匹配 + 端口兜底 |
| VBS 失败无声卡死 | 增加失败日志/提示 |
| 浏览器固定 2 秒打开 | 改为健康检查轮询后再打开 |

### P2 文档口径与打包卫生

| 缺陷 | 修复 |
|---|---|
| v0.1_review_packet / v0.1_status 仍写 41 项 | 加编者注：当前 180 项 + 3 子测试全通过 |
| 根 README 引 v0.1 基线（790/425） | 改引 v0.2 当前基线并链接评测说明 |
| `.workbuddy-ai/` 未 ignore | 已加入 .gitignore |
| run_checks.ps1 无退出码摘要 | 显式检查 `$LASTEXITCODE`，失败给中文摘要 |
| 用户可见错误为英文原始异常 | 关键路径中文化 |
| mermaid 依赖 CDN，断网必挂 | 加载失败降级为纯文本图谱 |
| `gpt-5.6-luna` 虚构默认模型名 | 移除默认值，未配置即报配置错误 |
| V0.2 全部工作未入库 | 全部提交并打 tag `v0.2.0` |
| `_mask_account` 3 处重复 | 收敛到 `legal_funds_agent.utils.mask_account` |

### 承认但不做/缓做的部分

- 报告 SHA-256 指纹：无签名、第三方无法独立验证、不含完整流水清单——作为已知限制写入文档，不在赛前重设计。
- gold cases 起诉书文本相同、样本多样性不足：属回归集设计，留出集已存在；赛后在 docs 中补充多样性说明。

---

## 四、收窄后的诚实口径（答辩建议）

1. **"45/45 有几项测了模型？"**——按每案 9 项检查：**25/45 项**依赖模型提取输出（候选识别、系统状态、风险码、判重、来源定位），**20/45 项**为确定性引擎回放校验（人工处置动作 → 状态机与金额汇总），验证的是规则引擎正确性而非模型能力。口径已写入 `docs/evaluation/README.md`。
2. **"冲突矩阵是算出来的还是写死的？"**——演示案件（GOLD_CASE_001）配置了 showcase 剧本用于稳定演示；其余案件走通用实现，当前覆盖第三方账户/代收代转类冲突，金额为流水实算。
3. **"审计怎么保证不可篡改？"**——承诺边界是**应用层**：SQLite 唯一性约束防静默覆盖、签署记录 append-only、报告附 SHA-256 指纹便于事后核对；密码学哈希链/签名未实现，已在安全文档声明。赛前已移除"读取路径补当前时间"这一会破坏审计可信的缺陷。
4. **"mock 是不是在假装 AI？"**——mock 是刻意设计的离线默认回归 provider（无网无 key 可复现），文档明示；能力证据以带 provenance 的 DeepSeek v0.2 基线为准。
