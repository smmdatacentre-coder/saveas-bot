FROM python:3.11-slim

ARG BUILD_DATE=2026-08-02

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /tmp/ffmpeg-src \
    && curl -sL https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz | tar -xJf - -C /tmp/ffmpeg-src \
    && cp /tmp/ffmpeg-src/ffmpeg-*-amd64-static/ffmpeg /usr/local/bin/ \
    && cp /tmp/ffmpeg-src/ffmpeg-*-amd64-static/ffprobe /usr/local/bin/ \
    && chmod +x /usr/local/bin/ffmpeg /usr/local/bin/ffprobe \
    && ffmpeg -version \
    && rm -rf /tmp/ffmpeg-src

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot.py"]
