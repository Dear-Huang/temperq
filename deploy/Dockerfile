FROM node:22-bookworm-slim AS website
WORKDIR /build
COPY website/package.json website/package-lock.json ./
RUN npm ci
COPY website/ ./
RUN npm run build

FROM python:3.11-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 QWB_SITE_DB=/var/lib/qwb/site.db
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 qwb
WORKDIR /app
COPY requirements-site.lock ./
RUN pip install --no-cache-dir -r requirements-site.lock
COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/
COPY third_party/ashare-tcost/ ./third_party/ashare-tcost/
RUN pip install --no-cache-dir --no-deps . ./third_party/ashare-tcost
COPY share_site/ ./share_site/
COPY examples/ ./examples/
COPY website/public/ ./website/public/
COPY --from=website /build/dist/ ./website/dist/
COPY scripts/backup_site_db.py ./scripts/backup_site_db.py
RUN mkdir -p /var/lib/qwb && chown qwb:qwb /var/lib/qwb
USER qwb
EXPOSE 8800
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8800/healthz', timeout=4)"
CMD ["python", "-m", "uvicorn", "share_site.app:app", "--host", "0.0.0.0", "--port", "8800", "--workers", "1", "--no-proxy-headers"]
