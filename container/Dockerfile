FROM node:24-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv ca-certificates git curl jq ripgrep fd-find \
    && rm -rf /var/lib/apt/lists/* \
    && ln -s /usr/bin/fdfind /usr/local/bin/fd

# CLI versions are explicit; native host Pi remains the primary installation route.
RUN npm install -g --ignore-scripts @earendil-works/pi-coding-agent@0.78.0 \
    @openai/codex@0.118.0 @anthropic-ai/claude-code@2.1.98 \
    && pi --offline --version && codex --version && claude --version \
    && npm cache clean --force

RUN mkdir -p /home/node/wayline/workspace && chown -R node:node /home/node/wayline
COPY --chown=node:node AGENTS.md /home/node/wayline/workspace/AGENTS.md
USER node
WORKDIR /home/node/wayline/workspace
ENV TZ=UTC PYTHONUNBUFFERED=1 CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
CMD ["sleep", "infinity"]
