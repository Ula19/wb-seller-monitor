# сайт (React): собираем отдельно, в итоговый образ попадает только web/dist
FROM node:22-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# chromium для keeper (веб-сессия WB) + Xvfb: браузер гоняем headful под виртуальным
# дисплеем (headless антибот WB палит). --with-deps ставит системные либы chromium.
RUN apt-get update \
    && apt-get install -y --no-install-recommends xvfb postgresql-client age \
    && rm -rf /var/lib/apt/lists/* \
    && patchright install --with-deps chromium

COPY . .
COPY --from=web /web/dist /app/web/dist

# виртуальный дисплей для headful-браузера keeper'а. Xvfb запускаем сами в фоне:
# xvfb-run в Docker виснет навсегда, ожидая от Xvfb сигнал готовности.
# lock от прошлого запуска: при рестарте контейнера /tmp сохраняется, и Xvfb не стартует
ENV DISPLAY=:99
CMD ["sh", "-c", "rm -f /tmp/.X99-lock; Xvfb :99 -screen 0 1280x1024x24 -nolisten tcp & alembic upgrade head && exec python -m app.main"]
