# 基础镜像可通过 build-arg 换成镜像源（国内直连 Docker Hub 常超时）：
#   docker build --build-arg NODE_IMAGE=<mirror>/library/node:22-bookworm-slim \
#                --build-arg PYTHON_IMAGE=<mirror>/library/python:3.12-slim -t sortie-deck .
ARG NODE_IMAGE=node:22-bookworm-slim
ARG PYTHON_IMAGE=python:3.12-slim-bookworm
# pip 源：受限网络/国内构建可传镜像源（dsh 运行时 wheel 约 95MB，直连 pypi 常超时）
ARG PIP_INDEX_URL="https://pypi.org/simple"

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
ARG PIP_INDEX_URL
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --index-url "${PIP_INDEX_URL}" -e .

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

# ---------- harness 运行时（只给 worker，A 方案） ----------------------------
# 队列任务在 worker 里执行，编码/文档阶段由 harness provider 直接 spawn agent CLI：
#   claude_code → claude-agent-sdk 起子进程调 `claude`
# 基础 runtime 镜像只装了 `pip install -e .`（不含可选依赖、不含 node），
# 所以 worker 必须用本 stage。
#
# 注意：Docker 默认构建最后一个 stage。用本文件构建 api 镜像时必须显式
#   docker build --target runtime .
# 或由 docker-compose.yml 里的 target 指定，否则会构建出这个胖镜像。
FROM runtime AS runtime-harness

USER root

# Bash 沙箱依赖（见 docs/plans/2026-11-10-claude-sandbox-替代bypass与任务级依赖.md）：
# bubblewrap 负责文件系统/网络隔离，socat 把沙箱内网络转发到沙箱代理。
# 只有 runtime-harness 需要：api 只跑探针，真正 spawn agent 的是 worker。
ARG APT_MIRROR=""
RUN if [ -n "$APT_MIRROR" ]; then \
        sed -i "s|http://deb.debian.org/debian|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources 2>/dev/null || true; \
        sed -i "s|http://deb.debian.org/debian|${APT_MIRROR}|g" /etc/apt/sources.list 2>/dev/null || true; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends bubblewrap socat \
    && rm -rf /var/lib/apt/lists/*

# Node 直接复用上面 web 构建阶段的二进制（22.x），
# 不走 apt：Debian 12 自带 node 18 偏旧，且省一次 apt 网络依赖。
COPY --from=web /usr/local/bin/node /usr/local/bin/node
COPY --from=web /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -sf /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -sf /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && node -v && npm -v

# harness 依赖都在 optional-dependencies 里（pyproject: harness-claude / harness-dsh），
# 基础镜像的 `pip install -e .` 不会装它们，probe 会直接 missing。
# PIP_INDEX_URL 在文件顶部声明，这里复用（dsh 运行时 wheel 约 95MB）。
ARG PIP_INDEX_URL
RUN pip install --index-url "${PIP_INDEX_URL}" -e ".[harness-claude,harness-dsh]"

# claude-agent-sdk 的 wheel 自带原生 CLI（_bundled/claude，实测 314MB），
# 而 SDK 的 _find_cli() 优先用它。所以不再 npm 安装 @anthropic-ai/claude-code
# （那会重复占 314MB）；只做一条 PATH 软链，让 legacy 编码执行器/降级路径的
# shutil.which("claude") 也能找到。
RUN ln -sf /usr/local/lib/python3.12/site-packages/claude_agent_sdk/_bundled/claude \
        /usr/local/bin/claude \
    && claude --version

# codex 只能走 npm（包内是 musl 静态二进制，按平台可选依赖安装，约 +390MB）。
# 国内构建可用 --build-arg NPM_REGISTRY=https://registry.npmmirror.com
ARG NPM_REGISTRY="https://registry.npmjs.org"
RUN npm i -g --registry="${NPM_REGISTRY}" @openai/codex \
    && codex --version

# 沙箱配置放最后：只改 JSON 时只重建本层，不必重跑 pip / npm 安装。
# TDT_HARNESS_SETTINGS_FILE 指向它即可启用（见 .env.example）。
COPY docker/claude-sandbox.json /etc/sortie/claude-sandbox.json
RUN chmod 644 /etc/sortie/claude-sandbox.json

# cursor 暂不装：它的 install 脚本装到 $HOME/.local/bin（构建期是 /root，运行期是
# /home/sortie，路径对不上，需显式搬到 /usr/local/bin），且必须有 CURSOR_API_KEY
# （容器内无法交互登录）。要启用时：
#   RUN curl -fsSL https://cursor.com/install | bash \
#       && cp /root/.local/bin/cursor-agent /usr/local/bin/ \
#       && chmod +x /usr/local/bin/cursor-agent

# 凭据不进镜像：ANTHROPIC_API_KEY 由 compose env_file/environment 在运行时注入。
USER sortie

# worker 不监听 HTTP，从 runtime 继承来的 /api/health 检查会把它判成 unhealthy，
# 进而影响编排层的重启/依赖判断，这里显式关闭。
HEALTHCHECK NONE
