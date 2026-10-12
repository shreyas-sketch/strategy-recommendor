FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    APP_ENV=production

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY config ./config
COPY static ./static

# One worker on purpose: the 6 AM scheduler, the login rate limiter and SQLite all live in
# this process. 100 participants is far below what one async worker handles.
# --proxy-headers: Railway terminates TLS at its edge; this gives us the real client IP.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --proxy-headers --forwarded-allow-ips='*' --no-server-header --timeout-keep-alive 30"]
