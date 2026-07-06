#!/bin/bash
# Первичная установка NFI X7 стратегии для Freqtrade
# Запуск: bash /opt/crypto-trader/tools/setup_freqtrade.sh
set -e

BASE=/opt/crypto-trader
STRATEGY_DIR=$BASE/bots/freqtrade/user_data/strategies
NFI_REPO="https://raw.githubusercontent.com/iterativv/NostalgiaForInfinity/main"

echo "=== Freqtrade + NFI X7 Setup ==="

# Создать директории
mkdir -p $STRATEGY_DIR
mkdir -p $BASE/bots/freqtrade/user_data/logs
mkdir -p $BASE/bots/freqtrade/user_data/data
mkdir -p $BASE/bots/freqtrade/user_data/backtest_results

echo "[1/3] Скачиваю NostalgiaForInfinityX7.py..."
curl -sSL "$NFI_REPO/NostalgiaForInfinityX7.py" -o "$STRATEGY_DIR/NostalgiaForInfinityX7.py"
echo "  OK: $(wc -l < $STRATEGY_DIR/NostalgiaForInfinityX7.py) строк"

echo "[2/3] Скачиваю вспомогательные файлы..."
# NI X7 требует файл с торговыми парами
curl -sSL "$NFI_REPO/configs/pairlist-volume-bybit-usdt.json" \
  -o "$BASE/bots/freqtrade/user_data/pairlist-bybit.json" 2>/dev/null || \
  echo "  (pairlist не найден — используется VolumePairList из config.json)"

echo "[3/3] Проверяю config.json..."
if [ -f "$BASE/bots/freqtrade/config.json" ]; then
  echo "  OK: config.json существует"
else
  echo "  ОШИБКА: config.json не найден!"
  exit 1
fi

echo ""
echo "=== Готово ==="
echo "Запуск Freqtrade:"
echo "  cd $BASE && docker compose --profile freqtrade up -d"
echo ""
echo "Логи:"
echo "  docker compose logs -f freqtrade"
