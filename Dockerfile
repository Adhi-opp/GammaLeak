FROM python:3.12-slim

WORKDIR /app

# System deps for aiohttp SSL + protobuf
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libffi-dev libssl-dev && \
    rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY GammaLeak.py .
COPY web_server.py .
COPY fii_dii_scraper.py .
COPY oauth_token_exchange.py .
COPY fetch_historical.py .
COPY analytics/ analytics/
COPY core/ core/
COPY gammaleak_runtime/ gammaleak_runtime/
COPY orderflow/ orderflow/
COPY signals/ signals/
COPY ui/ ui/
COPY static/ static/

# Logs and research dirs
RUN mkdir -p logs research historical

EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/')" || exit 1

CMD ["python", "web_server.py", "--port", "8080"]
