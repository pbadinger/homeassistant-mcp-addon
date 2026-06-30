ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.12-alpine3.20
FROM ${BUILD_FROM}

WORKDIR /app

RUN python -m pip install --no-cache-dir \
    fastapi==0.115.6 \
    pydantic==2.10.4 \
    uvicorn==0.34.0

COPY rootfs /
COPY app /app/mcp_companion

RUN chmod +x /run.sh

CMD ["/run.sh"]

