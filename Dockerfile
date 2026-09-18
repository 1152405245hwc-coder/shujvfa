# 资金链证审 —— 单容器交付镜像
# 构建:  docker build -t legal-funds-agent .
# 运行:  docker run --rm -p 8501:8501 legal-funds-agent
# 访问:  http://localhost:8501  (由评审方自行在浏览器打开, 容器不调用宿主机浏览器)
#
# 说明:
#   - 单容器, 不需要 Docker Compose; 检索/分析均为容器内 Python 模块 + SQLite。
#   - 依赖由 uv.lock 冻结, 构建阶段使用 uv 安装, 评审方无需了解 uv。
#   - 不包含任何 API Key; 默认 LLM_PROVIDER=mock 离线运行。
#     如需真实 LLM, 运行时注入环境变量即可, 例如:
#       docker run -e LLM_PROVIDER=deepseek -e DEEPSEEK_API_KEY=... -p 8501:8501 legal-funds-agent

# 默认使用官方 python:3.12-slim; 网络受限环境可在构建时覆盖:
#   docker build --build-arg BASE_IMAGE=docker.m.daocloud.io/library/python:3.12-slim -t legal-funds-agent .
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE} AS base

# uv 仅作为构建期依赖安装工具 (版本与本地开发一致)
COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /uvx /usr/local/bin/

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

# 先拷贝依赖声明与项目源码, 利用构建缓存完成依赖安装
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --frozen --no-editable --extra ui --extra pdf --extra dev

# 再拷贝运行时材料 (界面 / 演示数据 / 测试 / 配置)
COPY ui ./ui
COPY sample_data ./sample_data
COPY tests ./tests
COPY tools ./tools
COPY docs ./docs

EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8501/healthz', timeout=4).status == 200 else 1)"

CMD ["python", "-m", "streamlit", "run", "ui/streamlit_app.py", \
     "--server.address=0.0.0.0", \
     "--server.port=8501", \
     "--server.headless=true", \
     "--server.fileWatcherType=none", \
     "--browser.gatherUsageStats=false", \
     "--client.toolbarMode=minimal", \
     "--server.maxUploadSize=200"]
