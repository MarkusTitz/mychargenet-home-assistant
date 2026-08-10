FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

ARG BUILD_VERSION=0.2.4-pilot
ARG BUILD_ARCH=local

LABEL io.hass.version="${BUILD_VERSION}" \
      io.hass.type="app" \
      io.hass.arch="${BUILD_ARCH}"

WORKDIR /opt/mychargenet-edge
COPY edge/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY edge/app ./app
COPY addon_entrypoint.py ./addon_entrypoint.py
RUN mkdir -p /data \
    && useradd --system --uid 10001 --home-dir /data/mychargenet-edge --shell /usr/sbin/nologin edge

EXPOSE 8100
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8100/health', timeout=2)"]

ENTRYPOINT ["python", "/opt/mychargenet-edge/addon_entrypoint.py"]
