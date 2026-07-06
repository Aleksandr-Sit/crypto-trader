#!/bin/bash
# Еженедельное обновление NFI X7 (запускается cron каждый понедельник 03:00 UTC)
# Cron: 0 3 * * 1 /opt/crypto-trader/tools/update_nfi.sh >> /var/log/update_nfi.log 2>&1
set -e

BASE=/opt/crypto-trader
STRATEGY_FILE=$BASE/bots/freqtrade/user_data/strategies/NostalgiaForInfinityX7.py
NFI_URL="https://raw.githubusercontent.com/iterativv/NostalgiaForInfinity/main/NostalgiaForInfinityX7.py"
TIMESTAMP=$(date '+%Y-%m-%d %H:%M UTC')

echo "[$TIMESTAMP] Проверяю обновления NFI X7..."

OLD_HASH=$(md5sum "$STRATEGY_FILE" 2>/dev/null | cut -d' ' -f1 || echo "none")

curl -sSL "$NFI_URL" -o "$STRATEGY_FILE.new" || {
  echo "[$TIMESTAMP] ОШИБКА: не удалось скачать стратегию"
  exit 1
}

NEW_HASH=$(md5sum "$STRATEGY_FILE.new" | cut -d' ' -f1)

if [ "$OLD_HASH" = "$NEW_HASH" ]; then
  echo "[$TIMESTAMP] Стратегия не изменилась, рестарт не нужен."
  rm -f "$STRATEGY_FILE.new"
  exit 0
fi

echo "[$TIMESTAMP] Стратегия обновлена ($OLD_HASH → $NEW_HASH), рестартую freqtrade..."
mv "$STRATEGY_FILE.new" "$STRATEGY_FILE"

cd $BASE && docker compose --profile freqtrade restart freqtrade 2>&1 || true

echo "[$TIMESTAMP] Готово. NFI X7 обновлён и freqtrade перезапущен."
