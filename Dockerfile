# Folio — FastAPI music-score viewer
# Build context is the repo root.
FROM python:3.12-slim

# HOME drives where the app keeps its runtime config (~/.folio/web_config.json).
# Pointing it at /config lets us persist that via a single volume.
ENV HOME=/config \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# pymupdf ships manylinux wheels, so no compiler/toolchain needed.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Only the application package is needed at runtime (scripts/ are dev tooling).
COPY web ./web

EXPOSE 8989

# Mirror folio.sh.
CMD ["python", "-m", "uvicorn", "web.server:app", "--host", "0.0.0.0", "--port", "8989"]
