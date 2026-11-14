# The complete local workbench; keys and databases are generated on first start.
FROM node:24-bookworm-slim AS node
WORKDIR /build
COPY web/package.json web/package-lock.json ./web/
RUN cd web && npm ci --no-audit --no-fund
COPY web ./web
RUN cd web && npm run build
COPY deploy/vps/dsh/package.json deploy/vps/dsh/package-lock.json ./dsh/
RUN cd dsh && npm ci --omit=dev --no-audit --no-fund

FROM python:3.12-slim-bookworm
LABEL org.opencontainers.image.source="https://github.com/KevinXia69/ForgeFlow" \
      org.opencontainers.image.licenses="Apache-2.0"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    HOME=/data/dsh/home \
    FORGEFLOW_LOCAL_CONFIG=/config/runtime.json \
    FORGEFLOW_LOCAL_ROOT=/app \
    FORGEFLOW_DSH_CLI=/app/.runtime/dsh/node_modules/@deepseek-ai/dsh/lib/bin.js \
    FORGEFLOW_DSH_NODE=/usr/local/bin/node
RUN apt-get -o Acquire::Retries=3 update \
    && apt-get -o Acquire::Retries=3 install -y --no-install-recommends \
       libreoffice-writer libreoffice-calc libreoffice-impress pandoc \
       fonts-dejavu-core fonts-noto-cjk ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=node /usr/local/bin/node /usr/local/bin/node
WORKDIR /app
COPY deploy/workbench/requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src ./src
COPY migrations ./migrations
COPY scripts ./scripts
COPY contracts ./contracts
COPY knowledge ./knowledge
COPY docs/en ./docs/en
COPY deploy/pandoc-reference.docx /app/pandoc-reference.docx
COPY --from=node /build/web/dist ./src/forgeflow/api/static/app
COPY --from=node /build/dsh /app/.runtime/dsh
RUN pip install --no-cache-dir --no-deps . \
    && groupadd --gid 10001 forgeflow \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /bin/false forgeflow \
    && mkdir -p /config /control-secret /odoo-secret /odoo-config /qdrant-config \
       /data/approvals /data/dsh/home /data/dsh/workspace /data/attachments \
       /data/knowledge /data/runs \
    && chown -R 10001:10001 /config /control-secret /odoo-secret /odoo-config /qdrant-config /data
USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "forgeflow.local_workbench.runtime", "api"]
