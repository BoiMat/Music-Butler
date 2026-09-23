FROM node:24-bookworm-slim AS node
FROM python:3.12-slim-bookworm
COPY --from=node /usr/local/bin/node /usr/local/bin/node
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends bash ca-certificates ffmpeg \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/music-butler
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY scripts/ ./scripts/
RUN chmod 0555 scripts/download_music.sh scripts/music_butler.py
USER 1000:1000
ENV HOME=/tmp LIBRARY_ROOT=/library
ENTRYPOINT ["/opt/music-butler/scripts/download_music.sh"]
