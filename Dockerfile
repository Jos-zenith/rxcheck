# rxcheck counter app - container for a cloud host (Render, Fly.io, Railway, a VPS).
# The image holds code and default data files only. Everything that changes - ledger, holds, audit trail,
# uploads, reports, data imports, backups - lives under RXCHECK_HOME, which must be a persistent disk.
# Demo mode is on by default: a public deployment of this research prototype must not receive real
# prescriptions (see README, "Public demo deployment").
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    RXCHECK_HOME=/var/rxcheck \
    RXCHECK_BACKUP_DIR=/var/rxcheck/backups \
    RXCHECK_DEMO=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && groupadd --system --gid 10001 rxcheck \
 && useradd --system --uid 10001 --gid rxcheck --no-create-home --shell /usr/sbin/nologin rxcheck

COPY app/rxcheck.py app/counter_app.py app/sharedindex.py app/doctorping.py ./app/
COPY docker-entrypoint.sh ./
COPY data/*.json ./data/
# the Vue bundle, built from web/ and committed: the image needs no Node
COPY static/app.js static/app.css ./static/
RUN chmod 0755 docker-entrypoint.sh

EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', '8765'), timeout=4)"

ENTRYPOINT ["./docker-entrypoint.sh"]
# HTTPS is terminated by the host's proxy; the app listens on $PORT (default 8765)
CMD ["python", "app/rxcheck.py", "serve", "--host", "0.0.0.0", "--behind-proxy"]
