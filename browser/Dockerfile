FROM python:3.12-slim

# Настоящий Google Chrome (не chromium/headless) + виртуальный экран и VNC
RUN apt-get update && apt-get install -y --no-install-recommends \
        wget ca-certificates xvfb x11vnc tzdata fonts-noto-color-emoji \
    && wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && apt-get install -y --no-install-recommends ./google-chrome-stable_current_amd64.deb \
    && rm google-chrome-stable_current_amd64.deb && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir curl_cffi==0.15.0

WORKDIR /app
COPY keeper.py start.sh ./
CMD ["sh", "/app/start.sh"]
