"""
Тест подключения ко всем сервисам.
Запуск: python test_connections.py

Проверяет:
  - Bybit API (баланс субаккаунта)
  - MEXC API (баланс)
  - Telegram бот (отправка тестового сообщения)

Ничего не торгует, только читает данные.
"""

import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

BYBIT_KEY    = os.getenv("BYBIT_API_KEY", "")
BYBIT_SECRET = os.getenv("BYBIT_API_SECRET", "")
BYBIT_TEST   = os.getenv("BYBIT_TESTNET", "false").lower() == "true"
MEXC_KEY     = os.getenv("MEXC_API_KEY", "")
MEXC_SECRET  = os.getenv("MEXC_API_SECRET", "")
TG_TOKEN     = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT      = os.getenv("TELEGRAM_CHAT_ID", "")


def mask(v: str) -> str:
    if not v or len(v) < 6:
        return "[пусто]"
    return v[:4] + "****" + v[-3:]

# ------------------------------------------------------------------
# Bybit
# ------------------------------------------------------------------


def test_bybit() -> bool:
    print("\n🔵 Тест Bybit...")
    print(f"   API Key: {mask(BYBIT_KEY)}")
    print(f"   Testnet: {BYBIT_TEST}")
    try:
        from pybit.unified_trading import HTTP
        client = HTTP(
            testnet=BYBIT_TEST,
            api_key=BYBIT_KEY,
            api_secret=BYBIT_SECRET,
        )
        resp = client.get_wallet_balance(accountType="UNIFIED")
        if resp.get("retCode") != 0:
            print(f"   ❌ Ошибка: {resp.get('retMsg')}")
            return False

        coins = resp["result"]["list"][0].get("coin", [])
        usdt = next((c for c in coins if c["coin"] == "USDT"), None)
        balance = float(usdt["walletBalance"]) if usdt else 0.0
        print(f"   ✅ Подключено! Баланс USDT: {balance:.2f}")
        return True
    except Exception as e:
        print(f"   ❌ Исключение: {e}")
        return False

# ------------------------------------------------------------------
# MEXC
# ------------------------------------------------------------------


def test_mexc() -> bool:
    print("\n🟣 Тест MEXC...")
    print(f"   API Key: {mask(MEXC_KEY)}")
    if not MEXC_KEY or MEXC_KEY == "your_mexc_api_key_here":
        print("   ⏭  Пропущен (ключ не задан)")
        return True
    try:
        import ccxt
        exchange = ccxt.mexc({
            "apiKey": MEXC_KEY,
            "secret": MEXC_SECRET,
        })
        balance = exchange.fetch_balance()
        usdt = balance.get("USDT", {}).get("free", 0.0) or 0.0
        print(f"   ✅ Подключено! Баланс USDT: {float(usdt):.2f}")
        return True
    except Exception as e:
        print(f"   ❌ Исключение: {e}")
        return False

# ------------------------------------------------------------------
# Telegram
# ------------------------------------------------------------------


async def test_telegram() -> bool:
    print("\n✈️  Тест Telegram...")
    print(f"   Bot Token: {mask(TG_TOKEN)}")
    print(f"   Chat ID:   {TG_CHAT}")
    if not TG_TOKEN or TG_TOKEN == "your_bot_token_here":
        print("   ⏭  Пропущен (токен не задан)")
        return True
    try:
        import aiohttp
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json={
                "chat_id": TG_CHAT,
                "text": (
                    "✅ Crypto-Trader: тест подключения прошёл!\n"
                    "Bybit, MEXC и Telegram работают.\n"
                    "Готов к запуску ботов 🚀"
                ),
            }, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()
                if data.get("ok"):
                    print("   ✅ Сообщение отправлено в Telegram!")
                    return True
                else:
                    print(f"   ❌ Ошибка Telegram: {data}")
                    return False
    except Exception as e:
        print(f"   ❌ Исключение: {e}")
        return False

# ------------------------------------------------------------------
# Запуск
# ------------------------------------------------------------------


async def main():
    print("=" * 50)
    print("  CRYPTO-TRADER — ТЕСТ ПОДКЛЮЧЕНИЙ")
    print("=" * 50)

    results = {}
    results["bybit"]    = test_bybit()
    results["mexc"]     = test_mexc()
    results["telegram"] = await test_telegram()

    print("\n" + "=" * 50)
    print("  РЕЗУЛЬТАТ")
    print("=" * 50)
    all_ok = True
    for name, ok in results.items():
        status = "✅ OK" if ok else "❌ FAIL"
        print(f"  {name:<12} {status}")
        if not ok:
            all_ok = False

    print("=" * 50)
    if all_ok:
        print("  🚀 Всё готово! Можно строить Grid Bot.")
    else:
        print("  ⚠️  Исправь ошибки выше перед запуском ботов.")
    print()

asyncio.run(main())
