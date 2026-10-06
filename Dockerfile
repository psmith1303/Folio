# Folio — FastAPI music-score viewer
# Build context is the repo root. Two targets: `runtime` (the image compose
# runs) and `test`, which runs the suite and fails the build on a failure --
# the deploy hook's gate (Src/Docker/docker-folio.yml's folio-test).
FROM python:3.12-slim AS runtime

# HOME drives where the app keeps its runtime config (~/.folio/web_config.json).
# Pointing it at /config lets us persist that via a single volume.
ENV HOME=/config \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Only the application package is needed at runtime (scripts/ are dev tooling).
COPY web ./web

EXPOSE 8989

# Mirror folio.sh.
CMD ["python", "-m", "uvicorn", "web.server:app", "--host", "0.0.0.0", "--port", "8989"]

# The suite, on top of the runtime image, so it tests what ships. deno runs
# the JS module tests (they skip without it).
FROM runtime AS test
COPY --from=denoland/deno:bin-2.9.5 /deno /usr/local/bin/deno
COPY requirements-dev.txt pytest.ini ./
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY scripts ./scripts
COPY tests ./tests
RUN python -m pytest -q -p no:cacheprovider
