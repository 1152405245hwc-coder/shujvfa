# 资金链证审

面向诈骗类刑事案件的涉案资金证据审查与指控金额一致性核验工具。

项目当前处于 `V0.1` 初版审核候选阶段。审核请从 [REVIEW.md](REVIEW.md) 开始。

## 当前可运行能力

当前已实现离线 Vertical Slice 核心：Mock 指控提取、CSV 流水解析、候选匹配、
人工纳入、确定性金额校验、版本化决定、SQLite 保存、报告和审计日志。

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m legal_funds_agent.workflow.vertical_slice
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

安装 UI 依赖后启动工作台：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[ui]"
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m streamlit run ui\streamlit_app.py
```

正式测试命令在安装开发依赖后为：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q
```

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
