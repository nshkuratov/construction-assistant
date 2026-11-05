# Бот: Python 3.12 + Node.js 22 (для codex).
FROM node:22-slim AS node

FROM python:3.12-slim
ARG CODEX_VERSION=0.166.0

# Node.js из официального образа: бинарник и глобальные модули.
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && npm install -g --silent "@openai/codex@${CODEX_VERSION}" \
 && npm cache clean --force

RUN useradd --create-home --uid 1000 app
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -q -r requirements.txt

COPY --chown=app:app . .
RUN mkdir -p data logs && chown app:app data logs

USER app
ENV HOME=/home/app \
    PYTHONUNBUFFERED=1 \
    CA_DATA_DIR=data
VOLUME ["/app/data", "/app/logs"]

# Бот обновляет data/heartbeat каждые 30 с; файл старше 120 с — контейнер нездоров.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import os,sys,time; sys.exit(0 if time.time()-os.path.getmtime('data/heartbeat')<120 else 1)"

CMD ["python", "-m", "src.bot"]
