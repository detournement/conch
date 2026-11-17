# syntax=docker/dockerfile:1.7

FROM python:3.13-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
COPY pyproject.toml README.md ./
COPY conch ./conch
RUN python -m pip wheel --no-cache-dir --wheel-dir /wheels .


FROM python:3.13-slim-bookworm AS base

ARG CONCH_UID=10001
ARG CONCH_GID=10001

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/home/conch \
    XDG_CONFIG_HOME=/home/conch/.config \
    XDG_CACHE_HOME=/tmp/conch-cache \
    XDG_STATE_HOME=/home/conch/.local/state \
    GIT_CONFIG_GLOBAL=/home/conch/.config/conch/gitconfig \
    PATH=/opt/venv/bin:${PATH}

RUN if ! getent group "${CONCH_GID}" >/dev/null; then \
        groupadd --gid "${CONCH_GID}" conch; \
    fi \
    && useradd --uid "${CONCH_UID}" --gid "${CONCH_GID}" \
        --create-home --shell /bin/sh conch \
    && python -m venv /opt/venv


FROM base AS dev-base

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        less \
        openssh-client \
        procps \
        ripgrep \
        tini \
    && rm -rf /var/lib/apt/lists/*


FROM base AS runtime

COPY --from=builder /wheels /wheels
RUN /opt/venv/bin/pip install --no-cache-dir /wheels/*.whl \
    && rm -rf /wheels

COPY --chmod=755 docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN mkdir -p \
        /workspace \
        /home/conch/.config/conch \
        /home/conch/.local/state/conch \
    && chown -R "${CONCH_UID}:${CONCH_GID}" /workspace /home/conch

WORKDIR /workspace
USER conch

ENTRYPOINT ["docker-entrypoint.sh"]
CMD []


FROM runtime AS minimal


# Optional strict-isolation fleet worker profile (Swarm Phase 2): the same
# verified wheel, but the container runs the constrained worker supervisor,
# never an interactive shell. Deploy digest-pinned (image@sha256:...);
# hostctl runs it non-root/read-only with all capabilities dropped and no
# Docker socket. Delivery/verification/rollback semantics are identical to
# the signed .pyz profile.
FROM runtime AS worker

ENV CONCH_FLEET_WORKER_HOME=/worker
VOLUME ["/worker"]

ENTRYPOINT ["conch-worker"]
CMD []


FROM dev-base AS dev

COPY --from=runtime /opt/venv /opt/venv
COPY --from=runtime /usr/local/bin/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN mkdir -p \
        /workspace \
        /home/conch/.config/conch \
        /home/conch/.local/state/conch \
    && chown -R conch /workspace /home/conch

WORKDIR /workspace
USER conch

ENTRYPOINT ["/usr/bin/tini", "--", "docker-entrypoint.sh"]
CMD []

# The default target is intentionally useful for agent work. Build
# --target minimal when image size matters more than developer tooling.
