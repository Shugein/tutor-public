# Один образ для bot и scheduler. openai-codex приносит pinned CLI runtime;
# Node не нужен.

FROM python:3.11-slim

# supercronic — cron для контейнеров: живёт как обычный процесс, логи в stdout
ARG SUPERCRONIC_VERSION=v0.2.34
ADD https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-amd64 /usr/local/bin/supercronic
RUN chmod +x /usr/local/bin/supercronic

# pango и шрифты — для weasyprint (лист заданий PDF); без них рендер падает.
# poppler-utils — для Read по PDF учебника: SDK рендерит страницы через pdftoppm,
# без него агент слеп на book/ (петли в контейнере падали с "pdftoppm is not installed")
# libespeak-ng1 — G2P для озвучки примеров (Kokoro); нужна библиотека и её
# данные, CLI-пакет espeak-ng не нужен. Сами веса модели не в образе: они
# качаются в logs/tts/ волюмом (scripts/setup_tts.py) — 311 МБ в слое ни к чему
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata ca-certificates \
       libpango-1.0-0 libpangoft2-1.0-0 fonts-dejavu-core poppler-utils \
       libespeak-ng1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.11.21 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1

# слой зависимостей отдельно от кода — кэшируется, пока не меняется lock
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY prompts ./prompts
COPY scripts ./scripts
COPY deploy/crontab ./deploy/crontab
RUN uv sync --frozen --no-dev

# tutors/, logs/, .env, token.json приходят volume'ами — состояние живёт на хосте
ENV PATH="/app/.venv/bin:$PATH" TZ=Europe/Belgrade

CMD ["python", "-m", "tutor.bot"]
