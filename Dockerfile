# ---- node source: official image, copied so node is exact and not an apt guess ----
# Node 24 pinned like claude-code-wrapper: Claude Code requires a current LTS line.
FROM node:24.22.0-bookworm-slim AS node

FROM python:3.12-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /usr/local/bin/uv
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
# Claude Code CLI pinned: used only by the prober (lazy usage probe); the api never runs it.
ARG CLAUDE_CODE_VERSION=2.1.304
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} && npm cache clean --force
WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/app/.venv UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && uv sync --frozen --no-dev \
    && apt-get purge -y git && apt-get autoremove -y && rm -rf /var/lib/apt/lists/* /root/.cache
ENV PATH="/app/.venv/bin:$PATH"
USER 1000
ENTRYPOINT ["usage"]
