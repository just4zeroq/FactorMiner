# FactorMiner runtime image.
#
# Two targets from one file so the frontend build is not repeated:
#   --target api  -> uvicorn on :8000
#   --target web  -> nginx serving the built SPA on :80
#
# The SPA is built with VITE_API_BASE="" (same origin), so /api and /ws go
# through the nginx proxy in the web image. The API is still published on 8000
# for direct/CLI access. Never bake VITE_API_TOKEN into this build: every
# VITE_* variable is inlined into public JavaScript.

FROM node:20-slim AS frontend
ARG VITE_API_BASE=""
ENV VITE_API_BASE=${VITE_API_BASE}
WORKDIR /app/web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build


FROM python:3.12-slim AS api

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # No proxy inside the container by default; override to reach an exchange
    # from a restricted network.
    FACTORMINER_NO_PROXY=1

WORKDIR /app

# git: requirements.txt pulls ml4t-backtest from a pinned commit.
# libgomp1: lightgbm's OpenMP runtime. curl: healthcheck convenience.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY core ./core
COPY api ./api
COPY config ./config
COPY skills ./skills
COPY scripts ./scripts

# Mount points for state that must survive a container rebuild.
RUN mkdir -p data factor_db logs user_workspace/reports \
    && useradd --create-home --uid 10001 miner \
    && chown -R miner:miner /app

USER miner

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

# One worker on purpose: task state and the websocket broadcaster live in
# process memory, so a second worker would answer with a different task list.
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]


FROM nginx:1.27-alpine AS web
COPY --from=frontend /app/web/dist /usr/share/nginx/html
COPY web/nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
