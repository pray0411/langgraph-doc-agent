# 通用 AI Agent（LangGraph ReAct）容器镜像
#
# 为什么需要它：README 的安全说明自己写着"请运行在专用隔离环境（Docker/虚拟机），
# 以普通用户而非 root 运行" —— 但仓库里此前并没有提供 Dockerfile，文档与实现脱节。
# 这个文件把那条建议变成可执行的东西。
#
# 用法：
#   docker build -t pray-agent .
#   docker run --rm -p 127.0.0.1:8000:8000 --env-file .env pray-agent
#
# 注意：
#   - 端口只绑定到 127.0.0.1（默认不对外暴露）。本项目的命令执行能力定位是
#     "单机个人开发辅助"，不是安全边界，**不要**把它暴露到公网。
#   - 容器以非 root 用户 `pray` 运行。
#   - .env 通过 --env-file 注入，绝不 COPY 进镜像（见 .dockerignore）。

FROM python:3.12-slim

# 可选：构建时跳过沉重的语义检索依赖（sentence-transformers 会拉取 torch）。
#   docker build --build-arg INSTALL_EMBEDDING=0 -t pray-agent .
# 关掉之后检索自动回退纯 BM25（retriever 已内置降级路径）。
ARG INSTALL_EMBEDDING=1

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/app/models

WORKDIR /app

# 先装依赖再拷代码：依赖不变时复用镜像层缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && if [ "$INSTALL_EMBEDDING" = "0" ]; then \
         pip uninstall -y sentence-transformers torch transformers; \
       fi

COPY . .

# 非 root 运行；工作目录与可写目录归该用户所有
RUN useradd --create-home --shell /usr/sbin/nologin pray \
    && mkdir -p /app/generated /app/index /app/data /app/logs /app/models \
    && chown -R pray:pray /app
USER pray

EXPOSE 8000

# 健康检查：/api/health 为只读探针（不消耗模型额度）
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"

CMD ["python", "-X", "utf8", "main.py", "web"]
