# Genex Alpha — Dockerfile
# Local test:  docker build -t genex-alpha . && docker run -p 8080:8080 genex-alpha
# Cloud Run:   gcloud builds submit --tag gcr.io/genex-mvp-2026/genex-alpha
#              then: gcloud run deploy genex-alpha --image gcr.io/genex-mvp-2026/genex-alpha ...

FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Streamlit — headless, no telemetry
ENV STREAMLIT_SERVER_HEADLESS=true
ENV STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

# Session/feedback local fallback (ephemeral — GCS is the persistent store on Cloud Run)
ENV SESSION_DIR=/tmp/sessions

# Cloud Run injects $PORT at runtime; default to 8080 for local Docker testing
EXPOSE 8080

CMD ["sh", "-c", "streamlit run app.py --server.port=${PORT:-8080} --server.address=0.0.0.0 --server.headless=true"]
