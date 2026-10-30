# 基础镜像可通过 build-arg 换成镜像源（国内直连 Docker Hub 常超时）：
#   docker build --build-arg NODE_IMAGE=<mirror>/library/node:22-bookworm-slim \
#                --build-arg PYTHON_IMAGE=<mirror>/library/python:3.12-slim -t sortie-deck .
ARG NODE_IMAGE=node:22-bookworm-slim
ARG PYTHON_IMAGE=python:3.12-slim-bookworm

# ---------- 前端构建 ----------
FROM ${NODE_IMAGE} AS web
WORKDIR /web
COPY apps/web/package.json apps/web/package-lock.json* ./
RUN npm ci --no-audit --no-fund || npm install --no-audit --no-fund
COPY apps/web/ ./
RUN npm run build

# ---------- 运行时 ----------
FROM ${PYTHON_IMAGE} AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TDT_HOST=0.0.0.0 \
    TDT_PORT=8787 \
    TDT_DATA_DIR=/data \
    TDT_WEB_DIST=/app/apps/web/dist

WORKDIR /app

# git 是编码执行器 clone/publish 要用的；健康检查用 python，少一个 curl 依赖。
# 内网/受限网络可用 --build-arg APT_MIRROR=https://<internal-mirror>/debian 切换源。
ARG APT_MIRROR=""
RUN if [ -n "$APT_MIRROR" ]; then \
        sed -i "s|http://deb.debian.org/debian|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources 2>/dev/null || true; \
        sed -i "s|http://deb.debian.org/debian|${APT_MIRROR}|g" /etc/apt/sources.list 2>/dev/null || true; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 先装依赖再拷源码，最大化利用构建缓存
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install -e .

# 前端产物：直接在仓库布局下（TDT_WEB_DIST 指向这里，不依赖 __file__ 推导）
COPY --from=web /web/dist ./apps/web/dist
COPY packages/ ./packages/
COPY docker/entrypoint.sh /usr/local/bin/sortie-entrypoint
RUN chmod +x /usr/local/bin/sortie-entrypoint \
    && useradd --create-home --uid 10001 sortie \
    && mkdir -p /data && chown -R sortie:sortie /data /app

USER sortie
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('TDT_PORT','8787'), timeout=4)" || exit 1

ENTRYPOINT ["/usr/local/bin/sortie-entrypoint"]
CMD ["api"]
