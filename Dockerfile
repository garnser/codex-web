# syntax=docker/dockerfile:1.7

ARG BASE_IMAGE=garnser/codex-web:base
FROM ${BASE_IMAGE} AS runtime

ARG CODEX_VERSION=0.154.0
ARG CODEX_UID=1000
ARG CODEX_GID=1000

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CODEX_WEB_HOST=0.0.0.0 \
    CODEX_WEB_PORT=8765 \
    HOME=/home/codex

# The expensive OS/Node/Codex/Python runtime is supplied by Dockerfile.base and
# published from trusted main builds. Keep user creation here so local UID/GID
# overrides continue to work for mounted workspaces. Fail explicitly if the
# requested Codex version does not match the selected base image.
RUN actual="$(codex --version)" \
    && printf '%s' "$actual" | grep -F "${CODEX_VERSION}" \
    && groupadd --gid "${CODEX_GID}" codex \
    && useradd --uid "${CODEX_UID}" --gid "${CODEX_GID}" --create-home --shell /bin/bash codex

WORKDIR /app

COPY . .

RUN mkdir -p /app/data /workspace /home/codex/.codex \
    && chown -R codex:codex /app/data /workspace /home/codex

USER codex

EXPOSE 8765

HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=4 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/livez', timeout=2).read()"

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "server.py"]
