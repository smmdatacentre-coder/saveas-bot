FROM python:3.11-slim

ARG BUILD_DATE=2026-08-02-v6

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl xz-utils wget gnupg \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /tmp/ffmpeg-src \
    && curl -sL https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz | tar -xJf - -C /tmp/ffmpeg-src \
    && cp /tmp/ffmpeg-src/ffmpeg-*-amd64-static/ffmpeg /usr/local/bin/ \
    && cp /tmp/ffmpeg-src/ffmpeg-*-amd64-static/ffprobe /usr/local/bin/ \
    && chmod +x /usr/local/bin/ffmpeg /usr/local/bin/ffprobe \
    && rm -rf /tmp/ffmpeg-src

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install chromium \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
        libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 \
        libgbm1 libpango-1.0-0 libcairo2 libasound2 libatspi2.0-0 \
        libxshmfence1 libxfixes3 fonts-liberation libexpat1 \
        libx11-xcb1 libxcb-dri3-0 libxss1 libxtst6 \
    && rm -rf /var/lib/apt/lists/*

COPY . .

CMD ["python", "bot.py"]
