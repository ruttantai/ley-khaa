FROM python:3.12-slim
WORKDIR /app

# Docker CLI ONLY — never the daemon. docker-compose.yml mounts the host's
# /var/run/docker.sock into this container so the backend can spawn sibling
# sandbox containers on the host daemon (spec §5.10); without a client binary
# that socket is unreachable and DockerSandbox.available() is always False,
# silently downgrading every "auto" run to SubprocessSandbox. Pinned, not
# floated — this phase's stance on every library the image ships. Fetched
# with python3 (already in the base image) rather than adding curl/wget, and
# the architecture is read from `uname -m` on the build platform itself
# rather than the buildx-only $TARGETARCH arg, since this project's Dockerfiles
# are built with the classic (non-BuildKit) builder, which never sets it.
ARG DOCKER_CLI_VERSION=27.5.1
RUN set -eux; \
    arch="$(uname -m)"; \
    case "${arch}" in \
        x86_64) docker_arch=x86_64 ;; \
        aarch64|arm64) docker_arch=aarch64 ;; \
        *) echo "unsupported arch: ${arch}" >&2; exit 1 ;; \
    esac; \
    python3 -c "import urllib.request; urllib.request.urlretrieve('https://download.docker.com/linux/static/stable/${docker_arch}/docker-${DOCKER_CLI_VERSION}.tgz', '/tmp/docker-cli.tgz')"; \
    tar -xzf /tmp/docker-cli.tgz -C /tmp; \
    mv /tmp/docker/docker /usr/local/bin/docker; \
    rm -rf /tmp/docker-cli.tgz /tmp/docker; \
    chmod +x /usr/local/bin/docker; \
    docker --version

COPY . .
RUN pip install --no-cache-dir .
CMD ["uvicorn", "ley_khaa.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
