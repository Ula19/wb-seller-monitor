#!/bin/sh
# Виртуальный экран + VNC (один раз залогиниться руками) + Chrome + наблюдатель.
set -e
mkdir -p /data/profile
rm -f /data/profile/Singleton*  # хвосты прошлого запуска, иначе Chrome не стартует
Xvfb :99 -screen 0 1366x768x24 -nolisten tcp &
export DISPLAY=:99
sleep 1
x11vnc -display :99 -forever -shared -rfbport 5900 -passwd "${VNC_PASSWORD:?}" -quiet -bg -o /data/vnc.log
# Chrome упал — поднимаем заново (профиль на volume, сессия не теряется).
# --no-sandbox: в контейнере под root без него не стартует; странице это не видно.
(while true; do
  chromium --no-sandbox --user-data-dir=/data/profile \
    --proxy-server="$BROWSER_PROXY" --remote-debugging-port=9222 \
    --no-first-run --no-default-browser-check --password-store=basic \
    --lang=ru-RU --window-size=1366,768 "https://www.wildberries.ru/" >/data/chrome.log 2>&1 || true
  sleep 5
done) &
exec python /app/keeper.py
