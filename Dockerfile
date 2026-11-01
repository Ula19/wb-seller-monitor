FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# chromium для keeper (веб-сессия WB) + Xvfb: браузер гоняем headful под виртуальным
# дисплеем (headless антибот WB палит). --with-deps ставит системные либы chromium.
RUN apt-get update \
    && apt-get install -y --no-install-recommends xvfb \
    && rm -rf /var/lib/apt/lists/* \
    && patchright install --with-deps chromium

COPY . .

# xvfb-run — виртуальный дисплей для headful-браузера keeper'а
CMD ["xvfb-run", "-a", "sh", "-c", "alembic upgrade head && python -m app.main"]
