# Slim rather than alpine: the httpx/h2 wheels build cleanly on glibc, and
# alpine's musl would force a source build for no size win worth having.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first, so a source-only change does not reinstall them.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# Runs unprivileged. Nothing here needs to write to the filesystem -- the
# response cache and the session both live in memory.
RUN useradd --create-home --shell /usr/sbin/nologin appuser
USER appuser

EXPOSE 8000

# Hosts inject $PORT; the default keeps `docker run -p 8000:8000` working locally.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
