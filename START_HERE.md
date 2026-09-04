# 个人启动与自测

开始前请先阅读 [PROJECT_MUST_READ.md](PROJECT_MUST_READ.md)，尤其是 DeepSeek 与确定性金额核验的职责边界。

### 方式一：直接双击启动（最推荐）

- 直接双击桌面上的 **【资金链证审系统】** 快捷方式；
- 或者直接双击项目文件夹下的 **`双击启动.bat`**。
- 系统会自动拉起控制台并自动在浏览器中打开：<http://localhost:8501>。

### 方式二：命令行启动

在 PowerShell 中进入项目目录后执行：

```powershell
.\start_ui.ps1
```

然后在浏览器打开 <http://localhost:8501>。默认选择“本地 Mock（推荐）”，不会联网或产生 API 费用。

运行自动化自测：

```powershell
.\run_checks.ps1
```

页面上传支持：

- 起诉书、被害人陈述：`.txt`、`.docx`、文本型 `.pdf`
- 银行流水：`.csv`、`.xlsx`、`.xlsm`

扫描型 PDF 如果没有文本层，会提示需要 OCR；当前版本不会把图片内容误当作已识别文字。
XLSX 会尝试识别常见银行流水表头（交易时间、银行流水号、收/支、对方户名/账户、金额、摘要/备注），
并转换为内部标准流水格式。

如需使用 DeepSeek，在启动 UI 前于同一个 PowerShell 窗口设置：

```powershell
$env:DEEPSEEK_API_KEY = "你的密钥"
.\start_ui.ps1
```

密钥不要写入文件或提交到 Git。
