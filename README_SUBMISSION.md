# 比赛提交运行说明（评审方专用）

## 一句话

本仓库为**单容器**交付：只需 `docker build` + `docker run`，无需安装 Python、uv 或任何其他环境，无需 Docker Compose。

## 构建与运行

```bash
# 在仓库根目录执行
docker build -t legal-funds-agent .
docker run --rm -p 8501:8501 legal-funds-agent
```

> 网络受限环境（无法访问 docker.io）可在构建时覆盖基础镜像，例如：
> `docker build --build-arg BASE_IMAGE=docker.m.daocloud.io/library/python:3.12-slim -t legal-funds-agent .`

容器启动后，评审方在浏览器自行访问：

```
http://localhost:8501
```

容器健康检查地址：`http://localhost:8501/healthz`（Docker HEALTHCHECK 已内置）。

## 架构形态

- **单容器**：Streamlit 界面、检索与分析逻辑（Python 模块）、SQLite 持久化全部在同一容器内，无独立搜索服务容器，无外部数据库/消息队列。
- **无 Compose**：本提交不提供也不需要 `docker-compose.yml`。
- **不调用宿主机浏览器**：容器仅监听 `0.0.0.0:8501`，页面由评审方自行打开。

## API Key 与运行模式

- 仓库内**不含任何真实 API Key**；`.env.example` 中所有 Key 均为空。
- 默认 `LLM_PROVIDER=mock`，完全离线运行，评审零配置即可体验全部流程。
- 如需接入真实 LLM（可选，非必需），运行时注入环境变量：

```bash
docker run --rm -p 8501:8501 \
  -e LLM_PROVIDER=deepseek \
  -e DEEPSEEK_API_KEY=<评审方自备 Key> \
  legal-funds-agent
```

## 数据与知识库来源说明

- **示例数据**（`sample_data/`）：全部为项目组自行合成的虚构材料，仅用于功能演示与回归验证，**不属于智能体运行的必需组成部分**；删除示例数据后，系统仍可通过界面上传外部案卷材料完成完整处理。详见 `sample_data/README_DATA.md`。
- **知识库**：**无**。本版本不依赖任何外部领域知识库 / RAG 知识库，所有核验规则与法律边界说明均为代码内置的确定性逻辑。
- **模型**：不使用任何图像生成模型。可选的 OCR（扫描件识别）依赖未包含在比赛镜像中，不影响正式主链（DOCX / XLSX / CSV / 文本型 PDF）。

## 容器内回归自验（可选）

镜像已包含测试套件与演示案例，可在容器内复跑：

```bash
docker run --rm legal-funds-agent python -m pytest tests -q
```

## 目录说明（评审相关）

| 路径 | 说明 |
| --- | --- |
| `Dockerfile` / `.dockerignore` | 单容器构建定义 |
| `src/legal_funds_agent/` | 确定性核验引擎、持久化、评测代码 |
| `ui/streamlit_app.py` | Streamlit 审查工作台（界面全中文） |
| `sample_data/` | 虚构演示与回归材料（非智能体组成部分） |
| `tests/` | pytest 回归套件 |
| `tools/local_windows/` | 开发者本地 Windows 调试脚本，**不属于比赛运行入口** |
