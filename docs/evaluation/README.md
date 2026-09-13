# 评测基线说明

本目录存放冻结的评测基线。**基线只有在能自证「是谁、用什么提示词、什么时候产出的」时才有意义**，
所以从 v0.2 起，每份基线都带 `provenance` 块。

---

## 一、文件清单

| 文件 | 口径 | 是否带溯源 | 状态 |
| --- | --- | --- | --- |
| `baseline_mock_v0.1.json` | 仅主张提取 | 否 | 有效（Mock 确定性，可重复） |
| `holdout_mock_v0.1.json` | 仅主张提取 | 否 | 有效 |
| `baseline_deepseek_v0.1.json` | 仅主张提取 | **否** | **历史记录，token 数已不可复现** |
| `holdout_deepseek_v0.1.json` | 仅主张提取 | **否** | 同上 |
| `baseline_deepseek_v0.2.json` | 主张 + 陈述 + 漏提复核 | **是** | 当前基线 |
| `holdout_deepseek_v0.2.json` | 主张 + 陈述 + 漏提复核 | **是** | 当前基线 |
| `holdout_v0.1_seal.json` | 首次调用前的 SHA-256 封存清单 | — | 有效 |

---

## 二、为什么有 v0.2

`deepseek-chat` 是**浮动别名**，指向服务商当前服务的版本。实测发现：

| 项 | v0.1 基线记录 | 2026-09-12 实测 |
| --- | --- | --- |
| 输入 tokens（5 案） | 790（158/案） | 805（161/案） |
| 输出 tokens（5 案） | 425 | 425（**完全一致**） |

排查结论：

1. 提示词与 `git show HEAD` 版本**逐字节一致**（长度 294）；
2. 该提示词在全部提交历史中**从未变过**（两次提交 md5 均为 `4544dd13db`）；
3. 同一请求连跑两次，`prompt_tokens` **稳定 161**，不是抖动；
4. 输出 tokens 与基线**完全相同**，说明模型输出行为没变。

⇒ 只可能是**服务端模型或分词器发生了漂移**。

v0.1 基线的结论仍然有效（5/5、45/45），但它的 token 数字不再可复现，
且**无法证明当时用的是哪份提示词**（没有指纹字段）。因此另记 v0.2，而不是覆盖历史记录。

---

## 三、口径差异

v0.1 只跑主张提取；v0.2 跑完整语义层，所以**两者的 token 数不可直接比较**。
每份报告同时给出两个口径：

| 汇总字段 | 含义 | 可比对象 |
| --- | --- | --- |
| `total_input_tokens` / `total_output_tokens` | 仅主张提取 | v0.1 基线 |
| `all_calls_model_calls` / `all_calls_input_tokens` / `all_calls_output_tokens` | 全链路真实成本 | v0.2 基线 |

v0.2 实测成本（每 5 案）：

| 回归集 | 调用次数 | 输入 | 输出 |
| --- | --- | --- | --- |
| Gold Cases | 15 | 3315 | 805 |
| 内部留出集 | 15 | 3400 | 842 |

---

## 四、如何重记基线

```powershell
$env:PYTHONPATH='src'
$env:LLM_PROVIDER='deepseek'
$env:DEEPSEEK_API_KEY='<仅在当前进程设置，不得写入任何文件>'

.\.venv\Scripts\python.exe -m legal_funds_agent.evaluation.gold_cases `
  --provider deepseek --with-statement-model --with-claim-audit --continue-on-error `
  --output docs\evaluation\baseline_deepseek_v0.2.json
```

**校验基线是否仍然复现**（这是关键步骤，不要跳过）：

```powershell
.\.venv\Scripts\python.exe -m legal_funds_agent.evaluation.gold_cases `
  --provider deepseek --with-statement-model --with-claim-audit --continue-on-error `
  --baseline docs\evaluation\baseline_deepseek_v0.2.json
```

判定规则：

| 情形 | 判定 | 含义 |
| --- | --- | --- |
| 结论一致 + token 一致 | 完全复现 | 基线仍然有效 |
| 结论一致 + **仅输入** token 漂移 | 指向服务端模型/分词器变更 | 基线数字不再可复现 |
| 结论一致 + **仅输出** token 漂移 | 服务端生成的非确定性 | 不构成模型变更的证据 |
| 结论一致 + 两个方向同时漂移 | 需人工判断 | 分词器变更与生成波动难以区分 |
| 结论出现分歧 | 最高优先级 | 必须人工复核后才能继续 |

`--baseline` 同时输出 `prompt_fingerprints_unchanged`：若为 `false`，说明**提示词被改过**，
此时 token 差异是正常的，不能归因于服务端。

### 为什么区分输入与输出

`temperature=0` 不保证逐字节可复现。实测：同一份留出集连跑两次，
输入 token 完全一致（842 / 全链路 3400），输出 token 差 2（456 → 454）。

- **输入** token 是提示词与材料分词后的确定性结果。输入变了而提示词没变，
  只能是服务端分词器或模型变了；
- **输出** token 反映生成过程，受服务端批处理、路由等影响，小幅波动属正常。

所以「仅输入漂移」才是模型变更的信号；把它和输出波动混在一起会误报。

---

## 五、模型生成的非确定性（实测记录）

| 项 | 第 1 次 | 第 2 次 | 差异 |
| --- | --- | --- | --- |
| 输入 tokens（仅主张提取） | 842 | 842 | 0 |
| 输出 tokens（仅主张提取） | 456 | 454 | −2 |
| 输入 tokens（全链路） | 3400 | 3400 | 0 |
| 输出 tokens（全链路） | 842 | 840 | −2 |

两次的**结论完全相同**（5/5、45/45）。这说明基线里的输出 token 数应当视为近似值，
不应作为逐字节复现的依据；而输入 token 数可以。

---

## 六、密钥规则

`DEEPSEEK_API_KEY` 只能通过**当前进程环境变量**设置，不得写入源代码、本目录任何文件、
README、测试材料、日志、SQLite 快照或 Git 提交。基线文件中只记录
`provider` / `model` / `base_url` 与提示词指纹，不含任何凭据。

---

## 七、表述边界

Gold Cases 与内部留出集均为项目内部构造的**公开虚构材料**。
两者的通过率只说明公开回归集上的表现，**不得表述为独立盲测准确率**，
也不构成任何法律结论。

## 八、每案 9 项检查的构成（"有几项测了模型"）

每案 9 项检查分两类，口径如下：

| 类别 | 检查项 | 数据来源 |
| --- | --- | --- |
| 模型能力相关（5 项/案，合计 25/45） | `candidate_ids`、`system_status`、`risk_codes`、`duplicate_group_count`、`source_reference` | LLM 提取输出 + 确定性代码 |
| 确定性回放校验（4 项/案，合计 20/45） | `human_status`、`covered_amount`、`uncovered_amount`、`disputed_amount` | `review_actions` 喂入引擎后与 `expected` 比对 |

后一类验证规则引擎（状态机、金额汇总）的正确性，不直接反映模型能力；
前一类才与提取质量相关。因此 45/45 的正确解读是"回归集全过 = 提取链路 25 项
+ 引擎回放 20 项"，不应整体表述为模型准确率。默认 provider 为 mock
（离线确定性回归路径）；能力证据以带 provenance 的 deepseek v0.2 基线为准。
