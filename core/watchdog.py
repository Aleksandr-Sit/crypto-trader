"""
Dead Man's Switch — запускается на РЕЗЕРВНОМ сервере (Senko VPS).

Логика:
  Каждые 30 секунд пингует /health эндпоинт основного сервера.
  Если нет ответа 90 секунд → закрывает ВСЕ позиции через Bybit API напрямую.

Запуск на Senko:
  python -m core.watchdog --primary-url http://CONTABO_IP:8080

Основной сервер (main.py) поднимает /health эндпоинт автоматически.
"""

import asyncio
import argparse
import time
import os
import sys
from loguru import logger
import aiohttp
from dotenv import load_dotenv


PING_INTERVAL = 30       # секунд между пингами
DEAD_THRESHOLD = 90      # секунд молчания → считать мёртвым
HEALTH_TIMEOUT = 10      # секунд ожидания ответа /health


class WatchdogClient:
    """Запускается на Senko. Следит за основным сервером."""

    def __init__(self, primary_url: str, api_key: str, api_secret: str,
                 telegram_token: str, telegram_chat_id: str):
        self._url = primary_url.rstrip("/") + "/health"
        self._api_key = api_key
        self._api_secret = api_secret
        self._tg_token = telegram_token
        self._tg_chat_id = telegram_chat_id
        self._last_seen: float = time.time()
        self._triggered = False

    async def run(self) -> None:
        logger.info(f"Watchdog started. Monitoring: {self._url}")
        await self._telegram(
            "👁 Watchdog запущен на резервном сервере.\n"
            f"Мониторю: {self._url}"
        )
        async with aiohttp.ClientSession() as session:
            while True:
                await self._ping(session)
                await self._check_dead()
                await asyncio.sleep(PING_INTERVAL)

    async def _ping(self, session: aiohttp.ClientSession) -> None:
        try:
            async with session.get(
                self._url, timeout=aiohttp.ClientTimeout(total=HEALTH_TIMEOUT)
            ) as resp:
                if resp.status == 200:
                    self._last_seen = time.time()
                    self._triggered = False  # Сервер живой — сбросить флаг
                else:
                    logger.warning(f"Health check returned {resp.status}")
        except Exception as e:
            elapsed = time.time() - self._last_seen
            logger.warning(f"Primary unreachable ({elapsed:.0f}s): {e}")

    async def _check_dead(self) -> None:
        elapsed = time.time() - self._last_seen
        if elapsed >= DEAD_THRESHOLD and not self._triggered:
            self._triggered = True
            logger.critical(
                f"PRIMARY SERVER DEAD for {elapsed:.0f}s! Triggering emergency close."
            )
            await self._emergency_close()

    async def _emergency_close(self) -> None:
        await self._telegram(
            f"💀 ОСНОВНОЙ СЕРВЕР НЕ ОТВЕЧАЕТ {DEAD_THRESHOLD} СЕК!\n"
            "Закрываю все позиции через резервный канал..."
        )
        try:
            from pybit.unified_trading import HTTP
            client = HTTP(api_key=self._api_key, api_secret=self._api_secret)

            # Отменить все ордера (spot)
            try:
                client.cancel_all_orders(category="spot")
                logger.info("Watchdog: cancelled all spot orders")
            except Exception as e:
                logger.error(f"Cancel orders failed: {e}")

            # Отменить все ордера (linear/futures)
            try:
                client.cancel_all_orders(category="linear", settleCoin="USDT")
                logger.info("Watchdog: cancelled all linear orders")
            except Exception as e:
                logger.error(f"Cancel linear orders failed: {e}")

            # Закрыть фьючерсные позиции
            try:
                positions = client.get_positions(
                    category="linear", settleCoin="USDT"
                )["result"]["list"]
                for pos in positions:
                    if float(pos.get("size", 0)) > 0:
                        close_side = "Sell" if pos["side"] == "Buy" else "Buy"
                        client.place_order(
                            category="linear",
                            symbol=pos["symbol"],
                            side=close_side,
                            orderType="Market",
                            qty=pos["size"],
                            reduceOnly=True,
                        )
                        logger.info(
                            f"Watchdog: closed {pos['symbol']} {pos['side']}"
                        )
            except Exception as e:
                logger.error(f"Close positions failed: {e}")

            await self._telegram(
                "✅ Watchdog: все позиции закрыты.\n"
                "Основной сервер требует проверки!\n"
                "После восстановления запусти /resume"
            )
        except Exception as e:
            logger.critical(f"WATCHDOG EMERGENCY CLOSE FAILED: {e}")
            await self._telegram(
                f"🚨 Watchdog не смог закрыть позиции: {e}\n"
                "Немедленно закрой позиции вручную на Bybit!"
            )

    async def _telegram(self, text: str) -> None:
        url = f"https://api.telegram.org/bot{self._tg_token}/sendMessage"
        try:
            async with aiohttp.ClientSession() as session:
                await session.post(
                    url,
                    json={"chat_id": self._tg_chat_id, "text": text},
                    timeout=aiohttp.ClientTimeout(total=10),
                )
        except Exception as e:
            logger.error(f"Watchdog telegram failed: {e}")


# ---------------------------------------------------------------------------
# Health-сервер — запускается на основном сервере в main.py
# ---------------------------------------------------------------------------

class HealthServer:
    """Простой HTTP-сервер, отвечает на /health. Запускается на Primary VPS."""

    def __init__(self, port: int = 8080):
        self._port = port
        self._start_time = time.time()

    async def start(self) -> None:
        from aiohttp import web

        async def health(request: web.Request) -> web.Response:
            return web.json_response({
                "status": "ok",
                "uptime": int(time.time() - self._start_time),
                "timestamp": int(time.time()),
            })

        app = web.Application()
        app.router.add_get("/health", health)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self._port)
        await site.start()
        logger.info(f"Health server started on port {self._port}")
        # Держим задачу живой до отмены; при CancelledError — корректная очистка.
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            await runner.cleanup()
            raise


# ---------------------------------------------------------------------------
# CLI точка входа для Senko VPS
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Crypto Bot Watchdog")
    parser.add_argument(
        "--primary-url",
        required=True,
        help="URL основного сервера, например http://1.2.3.4:8080",
    )
    parser.add_argument(
        "--env",
        default=".env",
        help="Путь к .env файлу (default: .env)",
    )
    args = parser.parse_args()

    load_dotenv(args.env)

    required = [
        "BYBIT_API_KEY", "BYBIT_API_SECRET",
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    ]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        print(f"ERROR: Missing in .env: {missing}")
        sys.exit(1)

    watchdog = WatchdogClient(
        primary_url=args.primary_url,
        api_key=os.environ["BYBIT_API_KEY"],
        api_secret=os.environ["BYBIT_API_SECRET"],
        telegram_token=os.environ["TELEGRAM_BOT_TOKEN"],
        telegram_chat_id=os.environ["TELEGRAM_CHAT_ID"],
    )

    asyncio.run(watchdog.run())
