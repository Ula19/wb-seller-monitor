#!/usr/bin/env python3
"""Выводит custom_emoji_id всех эмодзи из набора Telegram.

Использование:
    python scripts/get_emoji_ids.py [имя_набора]

Имя набора берётся из ссылки t.me/addemoji/<ИМЯ>. По умолчанию — tgmacicons.
Токен бота читается из переменной окружения BOT_TOKEN или из файла .env.
Зависимостей нет — только стандартная библиотека.
"""

import json
import os
import sys
import urllib.request
from pathlib import Path


def load_token() -> str:
    token = os.environ.get("BOT_TOKEN")
    if token:
        return token
    env = Path(__file__).resolve().parent.parent / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("BOT_TOKEN=") and not line.startswith("#"):
                return line.split("=", 1)[1].strip().strip("'\"")
    sys.exit("Не найден BOT_TOKEN (в окружении или .env)")


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "tgmacicons"
    token = load_token()
    url = f"https://api.telegram.org/bot{token}/getStickerSet?name={name}"

    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"Ошибка запроса: {e} — {e.read().decode(errors='ignore')[:200]}")

    if not data.get("ok"):
        sys.exit(f"Telegram вернул ошибку: {data.get('description')}")

    result = data["result"]
    stickers = result.get("stickers", [])
    print(f"Набор: {result.get('title')} ({result.get('name')})")
    print(f"Тип: {result.get('sticker_type')} | эмодзи: {len(stickers)}\n")
    print(f"{'#':>3}  {'эмодзи':<6}  custom_emoji_id")
    print("-" * 45)

    mapping = {}
    for i, st in enumerate(stickers, 1):
        emoji = st.get("emoji", "")
        ce_id = st.get("custom_emoji_id", "—")
        print(f"{i:>3}  {emoji:<6}  {ce_id}")
        mapping[emoji] = ce_id

    # готовый словарь для копирования в код
    print("\n# Готовый словарь {обычный_эмодзи: custom_emoji_id}:")
    print(json.dumps(mapping, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
