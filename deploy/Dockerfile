# Remote mode on a Docker host: runs the Bots' computer (service, browser, workspace, routines) headless.
#   docker compose -f deploy/docker-compose.yml up -d --build
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 OPENGROKBOT_HOME=/data PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements-service.txt .
RUN pip install -r requirements-service.txt && playwright install --with-deps chromium

COPY core ./core
COPY service ./service
COPY web ./web
COPY skills ./skills
COPY plugins ./plugins
COPY assets ./assets
COPY main.py .

RUN useradd -m bots && mkdir -p /data && chown -R bots /data /ms-playwright
USER bots
VOLUME /data
EXPOSE 8765

# The access token MUST be supplied (OPENGROKBOT_TOKEN). Provider keys come from environment variables
# (ANTHROPIC_API_KEY, OPENAI_API_KEY, ...) because a headless container has no credential manager.
CMD ["python", "main.py", "--service", "--host", "0.0.0.0", "--port", "8765"]
