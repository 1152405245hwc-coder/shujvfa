# 资金链证审

面向诈骗类刑事案件的涉案资金证据审查与指控金额一致性核验工具。

项目当前状态为 `V0.1 MVP core frozen`。审核请从 [REVIEW.md](REVIEW.md) 开始。

## 当前可运行能力

当前已实现离线 Vertical Slice 核心：Mock Claim 提取及显式人工确认、CSV 流水解析、
候选匹配、逐笔处置及理由、确定性金额校验、版本化决定、不可静默覆盖的 SQLite 保存、
来源定位、三种报告和最小审计日志。语义提取层可切换 Mock、OpenAI 或 DeepSeek，
业务流程和确定性金额核心不依赖具体 Provider。V0.1 明确只支持单 Claim。

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m legal_funds_agent.workflow.vertical_slice
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

安装 UI 依赖后启动工作台：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[ui,dev]"
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m streamlit run ui\streamlit_app.py
```

正式测试命令为：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

## 语义提取 Provider

默认配置不联网，也不会产生 API 费用：

```env
LLM_PROVIDER=mock
```

开发联调可使用 OpenAI Responses API 的 Structured Outputs：

```env
LLM_PROVIDER=openai
OPENAI_API_KEY=
OPENAI_MODEL=gpt-5.6-luna
```

最终 Provider 对照或 smoke test 可切换为 DeepSeek：

```env
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=
DEEPSEEK_MODEL=deepseek-chat
```

`OPENAI_API_KEY` 和 `DEEPSEEK_API_KEY` 不得提交到仓库。真实 API 只负责生成候选 Claim JSON；
Pydantic 校验、`source_text` 原文防伪、人工确认、流水匹配和金额计算仍由现有流程完成。

## Gold Case 评测

五个公开虚构 Gold Cases 用于冻结后的机制回归，不是独立盲测集。默认 Mock baseline 不联网：

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m legal_funds_agent.evaluation.gold_cases --provider mock
```

显式配置相应 API Key 后，可将 `--provider` 改为 `openai` 或 `deepseek`。真实 Provider
默认遇到首个错误即停止，避免在配置错误或限流时继续产生请求；只有明确需要收集全部失败时才加
`--continue-on-error`。当前冻结的 Mock baseline 见
[baseline_mock_v0.1.json](docs/evaluation/baseline_mock_v0.1.json)，DeepSeek 同输入实测见
[baseline_deepseek_v0.1.json](docs/evaluation/baseline_deepseek_v0.1.json)。DeepSeek 五案均通过，
总计输入790 tokens、输出425 tokens，平均延迟888.4ms；该结果只说明公开回归集表现，不能替代独立盲测。

另有一组在首次真实调用前以 SHA-256 封存的内部留出集，运行方式为：

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m legal_funds_agent.evaluation.gold_cases `
  --provider mock --gold-root sample_data\holdout_cases
```

封存清单见 [holdout_v0.1_seal.json](docs/evaluation/holdout_v0.1_seal.json)，Mock 与 DeepSeek
均为5/5案例、45/45检查通过。它独立于公开 Gold Cases，但仍是项目内部构造的虚构材料，
不能表述为外部专家盲测。

## V0.1 文档

- [产品范围与责任边界](docs/product/v0.1_scope.md)
- [资金证据审查协议](docs/legal/review_protocol.md)
- [规范数据模型](docs/design/canonical_schema.md)
- [演示案例与验收矩阵](docs/evaluation/demo_cases.md)
- [系统架构](docs/design/system_architecture.md)
- [实际完成状态](docs/product/v0.1_status.md)
- [数据处理说明](docs/security/data_handling.md)

## 核心原则

系统输出是可复核的资金证据审查意见，不是定罪、量刑或犯罪金额的最终司法认定。
大语言模型只参与材料语义提取；金额计算、去重、状态汇总和一致性校验由确定性代码完成。

> 仅可使用完全虚构材料进行演示。V0.1 不支持真实案件、多 Claim、PDF 或 OCR；
> DeepSeek 已完成五个公开虚构 Gold Cases 的真实 API 回归，默认演示仍始终使用 Mock。
