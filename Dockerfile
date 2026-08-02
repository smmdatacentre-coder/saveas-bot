FROM python:3.11-slim

ARG BUILD_DATE=2026-08-02-v3

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl xz-utils \
    libnss3 libatk-bridge2.0-0 libdrm2 libxcomposite1 libxdamage1 libxrandr2 \
    libgbm1 libasound2 libpango-1.0-0 libcairo2 libatspi2.0-0 libcups2 \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /tmp/ffmpeg-src \
    && curl -sL https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz | tar -xJf - -C /tmp/ffmpeg-src \
    && cp /tmp/ffmpeg-src/ffmpeg-*-amd64-static/ffmpeg /usr/local/bin/ \
    && cp /tmp/ffmpeg-src/ffmpeg-*-amd64-static/ffprobe /usr/local/bin/ \
    && chmod +x /usr/local/bin/ffmpeg /usr/local/bin/ffprobe \
    && ffmpeg -version \
    && rm -rf /tmp/ffmpeg-src

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && playwright install chromium

COPY . .
RUN python -m py_compile bot.py && echo "Syntax OK"

CMD ["python", "bot.py"]
