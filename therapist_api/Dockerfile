# Genex Therapist API — independent container (does not share the parent image).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

# Install runtime deps first for layer caching.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# App source.
COPY app ./app

# Cloud Run provides $PORT. The app is built via the fail-closed factory, so the
# container will refuse to serve if the environment is missing/incoherent.
EXPOSE 8080
CMD ["sh", "-c", "uvicorn app.main:create_asgi_app --factory --host 0.0.0.0 --port ${PORT:-8080}"]
