# Production image. Build: docker build -t aal-scheduler .
# Run:  docker run -p 8000:8000 -e ENV=prod -e SECRET_KEY=... -e DATABASE_URL=postgres://... -e BASE_URL=https://... aal-scheduler
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY seed_prod.py seed.py ./

RUN useradd --create-home --uid 10001 appuser && chown -R appuser /srv
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT','8000'), timeout=4)"

# Schema sync is idempotent; then serve behind the platform's TLS proxy.
CMD ["sh", "-c", "python seed_prod.py && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips '*'"]
