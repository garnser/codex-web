# syntax=docker/dockerfile:1.7

FROM python:3.14-slim-bookworm AS runtime

ARG CODEX_VERSION=latest
ARG CODEX_UID=1000
ARG CODEX_GID=1000

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CODEX_WEB_HOST=0.0.0.0 \
    CODEX_WEB_PORT=8765 \
    HOME=/home/codex

# Install the runtime toolchain directly into the image. Codex is installed
# from the official npm package during docker build; no host Codex binary or
# host Node installation is copied into the image or required to build it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        gnupg \
        openssh-client \
        ripgrep \
        tini \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_24.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install --global --omit=dev "@openai/codex@${CODEX_VERSION}" \
    && codex --version \
    && node --version \
    && npm --version \
    && rm -rf /var/lib/apt/lists/* /root/.npm \
    && groupadd --gid "${CODEX_GID}" codex \
    && useradd --uid "${CODEX_UID}" --gid "${CODEX_GID}" --create-home --shell /bin/bash codex

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt

COPY . .

RUN mkdir -p /app/data /workspace /home/codex/.codex \
    && chown -R codex:codex /app/data /workspace /home/codex

USER codex

EXPOSE 8765

HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=4 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/livez', timeout=2).read()"

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "server.py"]
