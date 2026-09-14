# 下一窗口接续说明

新窗口开始时，请先读取：

1. `REVIEW.md`
2. `docs/review/v0.1_review_packet.md`
3. 审核方提供的完整反馈
4. `docs/product/v0.1_status.md`
5. `docs/ai_assistance/development_log.md`

处理顺序固定为：阻断问题、法律口径问题、数据安全问题、核心算法问题、UI与展示问题、扩展建议。
不要在阻断反馈解决前增加 OCR、RAG、图谱、多智能体或部署功能。

开始修改前先运行：

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

环境窗口完成 Streamlit 和 pytest 安装后，再追加浏览器视觉验收和 `pytest -q`。每项审核反馈应当
对应代码或文档修改、回归测试、开发记录和独立 Git 提交。

---

## V0.2 之后的接续状态（2026-09-14）

### 必读文档追加

除上述 5 份外，请补读：

6. `docs/product/v0.2_semantic_layer.md` —— V0.2 语义提取层与模型增强的完整说明，
   含四条贯穿约束（数字只能来自确定性代码、引文必须逐字命中、模型只能补充不能覆盖、降级必须可见）
7. `docs/evaluation/README.md` —— 评测基线口径、漂移判定规则与重记流程

### 当前状态

- 自动化测试：**217 项 + 3 项参数化子测试**，全部通过（`pytest -q`）；
- 三组离线回归：Gold Cases 5/5、内部留出集 5/5、agent-bridge 5/5；
- 评测基线已重记为 `baseline_deepseek_v0.2.json` / `holdout_deepseek_v0.2.json`（带溯源）；
- 模型增强路径已完成浏览器端到端验收（服务端 0 异常）。

### 已知限制（不要误判为缺陷）

1. **Streamlit 按钮无法被自动化工具点击**：`agent-browser` 下 `find text` 能找到元素、
   点击无报错，但服务端不触发脚本重跑。因此「逐笔处置 → 签署 → 生成底稿」的完整流程
   需要人工在真实浏览器中验收。
2. **评测基线中的输出 token 不可逐字节复现**：同一留出集连跑两次，输入 token 完全一致、
   输出 token 相差 2。输入 token 可作复现依据，输出 token 只能视为近似值。

### 未完成项

- 浏览器内实际点击「导出」下载文件并核验内容（受限制 1 影响，需人工）；
- 主体归并候选（`AliasProposal`）尚无界面入口，目前只能从库层调用。

### 待产品决策

- 叙述摘要是否需要在案件概览页也展示（目前仅在底稿页）；
- 漏提复核的疑似漏项是否允许一键补录为主张（当前一律要求人工补录）。

