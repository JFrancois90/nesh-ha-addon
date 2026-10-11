# Nesh add-on image.
# Supervisor 2026.04.0 and later no longer passes BUILD_FROM and build.yaml is
# deprecated, so the base image is named here. The Home Assistant base images
# are multi-platform manifests, so one FROM serves both aarch64 and amd64.
ARG BUILD_FROM=ghcr.io/home-assistant/base-python:3.12-alpine3.23
FROM ${BUILD_FROM}

ARG BUILD_VERSION=0.1.0
ARG BUILD_ARCH

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip3 install -r /app/requirements.txt

COPY nesh_agent /app/nesh_agent
COPY run.sh /run.sh
RUN chmod a+x /run.sh

LABEL \
    io.hass.name="Nesh" \
    io.hass.description="Logs heating behaviour for a learned thermal model" \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.type="app" \
    io.hass.arch="${BUILD_ARCH}"

CMD [ "/run.sh" ]
