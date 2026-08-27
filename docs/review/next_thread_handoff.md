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

