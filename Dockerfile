# giliran web image: Django + gunicorn (threads, for the live streams) + WhiteNoise for static files.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Collect static files at build time (needs no database; the key is a throw-away build value).
RUN DJANGO_DEBUG=0 DJANGO_SECRET_KEY=build-only-not-a-secret python manage.py collectstatic --noinput \
    && useradd --system --no-create-home --uid 10001 giliran \
    && chown -R giliran /app/staticfiles
USER giliran

ENV DJANGO_DEBUG=0 \
    GUNICORN_THREADS=32

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz/', timeout=4).status == 200 else 1)"]

ENTRYPOINT ["/app/docker/entrypoint.sh"]
# One process with many threads: every open live stream holds a thread (see README, Live updates),
# and the in-memory rate limit counters live in that one process.
CMD ["sh", "-c", "exec gunicorn config.wsgi --bind 0.0.0.0:8000 --worker-class gthread --workers 1 --threads ${GUNICORN_THREADS} --access-logfile -"]
