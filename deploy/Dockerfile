# SMS agent receiver + inline worker.
#
# Deliberately NOT the main SiftStack image: no Playwright, no OpenCV, no
# Tesseract. The cloud box uses the smrtPhone API transport (verified live), so
# the browser session fallback is a local-only path and the image stays small
# and fast to redeploy.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY deploy/requirements-sms-agent.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Only the agent package. Everything it needs is env-driven, and the reisift
# client falls back to the standalone Api-Key path when there is no Deal Room
# checkout on disk, which there never is here.
COPY src/sms_agent ./sms_agent

# The SQLite event log lives on the mounted volume so it survives a redeploy.
ENV SMS_AGENT_DATA_DIR=/data \
    SMS_AGENT_DB=/data/sms_agent.db \
    SMS_AGENT_INLINE_WORKER=1 \
    SMS_AGENT_TRANSPORT=api \
    SMS_AGENT_HOST=0.0.0.0 \
    SMS_AGENT_PORT=8080 \
    PYTHONPATH=/app

EXPOSE 8080

# The container has no .env; every secret arrives as a Fly secret.
CMD ["uvicorn", "sms_agent.receiver:app", "--host", "0.0.0.0", "--port", "8080"]
