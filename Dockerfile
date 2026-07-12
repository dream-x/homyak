# Образ приложения Homyak (uv). Один образ на все сервисы — команда задаётся в compose.
FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.5.9 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# зависимости отдельным слоем (кэш) — сначала только манифесты
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# затем код + установка самого проекта (entry points)
COPY . .
RUN uv sync --frozen --no-dev

CMD ["homyak-api"]
