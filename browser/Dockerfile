FROM python:3.12-slim

# Chromium из Debian (Google Chrome под Linux ARM не существует, а сервер — aarch64) —
# обычный, НЕ headless, на виртуальном экране + VNC для разового входа
RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium xvfb x11vnc tzdata fonts-liberation fonts-dejavu-core fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir curl_cffi==0.15.0

WORKDIR /app
COPY keeper.py start.sh ./
CMD ["sh", "/app/start.sh"]
