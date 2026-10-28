FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app
COPY wayline/pyproject.toml wayline/uv.lock wayline/.python-version /app/wayline/
RUN uv sync --python 3.13 --frozen --no-dev --no-install-project --project /app/wayline
COPY wayline/src /app/wayline/src
RUN uv sync --python 3.13 --frozen --no-dev --no-editable --project /app/wayline

ENV PATH="/app/wayline/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    TZ=UTC
EXPOSE 8000
CMD ["wayline", "serve", "--host", "0.0.0.0", "--no-access-log"]
