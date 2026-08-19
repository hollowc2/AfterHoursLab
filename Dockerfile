FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_LINK_MODE=copy
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

RUN groupadd --gid 1001 afterhours && useradd --uid 1001 --gid afterhours --no-create-home afterhours
RUN mkdir -p /app/data && chown -R afterhours:afterhours /app
USER 1001:1001
ENV PATH="/app/.venv/bin:${PATH}"

CMD ["afterhours-lab-archive-earnings", "--watchlist", "/app/data/watchlist.json"]
