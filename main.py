"""
Точка входа. Инициализирует все модули и запускает боты.

Порядок запуска:
1. Загрузить секреты (и упасть если чего-то нет)
2. Подключить Redis
3. Инициализировать биржи
4. Запустить Emergency Stop
5. Запустить Telegram бот
6. Запустить News Sentinel
7. Запустить Health Server (для watchdog на Senko)
8. Запустить торговые боты
"""

import asyncio
import signal
import sys
from pathlib import Path
from loguru import logger
import yaml

from core.security import get_secrets
from core.exchange import ExchangeManager
from core.emergency_stop import init_emergency_stop
from core.state import StateStore
from core.portfolio import PortfolioManager
from core.watchdog import HealthServer
from notifications.telegram import TelegramNotifier
from sentinel.news_sentinel import NewsSentinel


async def _price_crash_monitor(exchange, emergency, symbol: str, interval: float) -> None:
    """Уровень 1 защиты: кормит EmergencyStop тиками цены для детектора краша."""
    while True:
        try:
            price = await exchange.get_ticker(symbol)
            await emergency.on_price_tick(symbol, price)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"Price monitor error: {e}")
        await asyncio.sleep(interval)


async def _drawdown_monitor(exchanges, emergency, interval: float = 300.0) -> None:
    """Уровень 2 защиты: периодически проверяет просадку портфеля."""
    while True:
        try:
            balance = await exchanges.get_total_balance_usdt()
            await emergency.on_balance_update(balance)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"Drawdown monitor error: {e}")
        await asyncio.sleep(interval)


def load_config() -> dict:
    path = Path(__file__).parent / "config.yaml"
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def setup_logging(log_level: str) -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level=log_level,
        format="<green>{time:HH:mm:ss}</green> | <level>{level}</level> | {message}",
    )
    logger.add(
        "logs/crypto-trader.log",
        level="DEBUG",
        rotation="50 MB",
        retention="14 days",
        compression="gz",
    )


async def main() -> None:
    # ------------------------------------------------------------------
    # 1. Конфигурация и секреты
    # ------------------------------------------------------------------
    config = load_config()
    setup_logging(config["system"].get("log_level", "INFO"))

    logger.info("=" * 60)
    logger.info("Crypto-Trader starting...")
    logger.info("=" * 60)

    secrets = get_secrets()

    if secrets.is_paper_mode():
        logger.warning("⚠️  PAPER MODE — реальные деньги не задействованы!")
    else:
        logger.warning("💰 LIVE MODE — торговля реальными деньгами!")

    # ------------------------------------------------------------------
    # 2. Инфраструктура
    # ------------------------------------------------------------------
    redis_url = secrets.require("REDIS_URL")
    if "changeme" in redis_url and not secrets.is_paper_mode():
        logger.critical(
            "REDIS_URL содержит дефолтный пароль 'changeme'. "
            "Смените пароль в .env и docker-compose.yml перед live-торговлей!"
        )
        sys.exit(1)
    state = StateStore(redis_url)
    await state.connect()

    exchanges = ExchangeManager(secrets)

    # ------------------------------------------------------------------
    # 3. Emergency Stop
    # ------------------------------------------------------------------
    emergency = init_emergency_stop(exchanges)
    risk_cfg = config.get("risk", {})
    emergency.CRASH_DROP_PCT = risk_cfg.get("crash_drop_pct", 5.0)
    emergency.MAX_DRAWDOWN_PCT = risk_cfg.get("max_portfolio_drawdown_pct", 12.0)

    # ------------------------------------------------------------------
    # 4. Telegram
    # ------------------------------------------------------------------
    telegram = TelegramNotifier(secrets)
    emergency.set_notifier(telegram.send)
    telegram.set_emergency_stop(emergency)
    telegram.set_exchange_manager(exchanges)

    # ------------------------------------------------------------------
    # 5. Portfolio
    # ------------------------------------------------------------------
    initial_balance = await exchanges.get_total_balance_usdt()
    portfolio = PortfolioManager(exchanges, initial_balance)
    telegram.set_portfolio(portfolio)

    # ------------------------------------------------------------------
    # 6. News Sentinel
    # ------------------------------------------------------------------
    sentinel_cfg = config.get("sentinel", {})
    sentinel: NewsSentinel | None = None
    if sentinel_cfg.get("enabled", True):
        sentinel = NewsSentinel(
            cryptopanic_key=secrets.get("CRYPTOPANIC_API_KEY"),
        )
        sentinel.set_notifier(telegram.send)
        sentinel.set_emergency_stop(emergency)
        sentinel.set_exchange_manager(exchanges)
        telegram.set_sentinel(sentinel)

    # ------------------------------------------------------------------
    # 7. Запускаем все задачи
    # ------------------------------------------------------------------
    tasks       = []
    bot_registry: dict = {}  # name → bot instance, для AnalyzerBot

    # Telegram bot
    tasks.append(asyncio.create_task(telegram.start(), name="telegram"))

    # Health server для watchdog
    health = HealthServer(port=config["system"].get("health_port", 8080))
    tasks.append(asyncio.create_task(health.start(), name="health"))

    # News Sentinel
    if sentinel:
        tasks.append(asyncio.create_task(sentinel.run(), name="sentinel"))

    # Price crash monitor (Уровень 1 EmergencyStop — без этого crash detector мёртв)
    bybit_adapter = exchanges.get("bybit")
    tasks.append(asyncio.create_task(
        _price_crash_monitor(
            bybit_adapter, emergency,
            symbol="BTCUSDT",
            interval=risk_cfg.get("price_monitor_interval_sec", 30),
        ),
        name="price_monitor",
    ))

    # Drawdown monitor (Уровень 2 EmergencyStop — проверка просадки портфеля)
    tasks.append(asyncio.create_task(
        _drawdown_monitor(
            exchanges, emergency,
            interval=risk_cfg.get("drawdown_check_interval_sec", 300),
        ),
        name="drawdown_monitor",
    ))

    # Grid Bot
    grid_cfg = config.get("grid_bot", {})
    if grid_cfg.get("enabled", True):
        from bots.grid.bot import GridBot
        grid_exchange = exchanges.get(grid_cfg.get("exchange", "bybit"))
        grid_bot = GridBot(
            exchange=grid_exchange,
            config=grid_cfg,
            state_store=state,
            emergency_stop=emergency,
            paper_mode=secrets.is_paper_mode(),
        )
        tasks.append(asyncio.create_task(grid_bot.start(), name="grid"))
        telegram.register_bot("grid", grid_bot)
        bot_registry["grid_bot"] = grid_bot
        logger.info("Grid Bot scheduled.")

    # Funding Arb Bot
    arb_cfg = config.get("funding_arb", {})
    if arb_cfg.get("enabled", False):
        from bots.funding_arb.bot import FundingArbBot
        arb_exchange = exchanges.get(arb_cfg.get("exchange", "bybit"))
        arb_bot = FundingArbBot(
            exchange=arb_exchange,
            config=arb_cfg,
            state_store=state,
            emergency_stop=emergency,
            paper_mode=secrets.is_paper_mode(),
            notifier=telegram.send,
        )
        tasks.append(asyncio.create_task(arb_bot.start(), name="funding_arb"))
        telegram.register_bot("funding_arb", arb_bot)
        bot_registry["funding_arb"] = arb_bot
        logger.info("Funding Arb Bot scheduled.")

    # Scalper Bot
    scalper_cfg = config.get("scalper", {})
    if scalper_cfg.get("enabled", False):
        from bots.scalper.bot import ScalperBot
        scalper_exchange = exchanges.get(scalper_cfg.get("exchange", "bybit"))
        scalper_bot = ScalperBot(
            exchange=scalper_exchange,
            config=scalper_cfg,
            state_store=state,
            emergency_stop=emergency,
            paper_mode=secrets.is_paper_mode(),
            notifier=telegram.send,
        )
        tasks.append(asyncio.create_task(scalper_bot.start(), name="scalper"))
        telegram.register_bot("scalper", scalper_bot)
        scalper_bot.set_pnl_reporter(portfolio.report_scalper_pnl)
        bot_registry["scalper"] = scalper_bot
        logger.info("Scalper Bot scheduled.")

    # Redistributor
    redis_cfg = config.get("redistributor", {})
    if redis_cfg.get("enabled", False):
        from bots.redistributor.bot import RedistributorBot
        redistributor = RedistributorBot(
            config=redis_cfg,
            state_store=state,
            emergency_stop=emergency,
            portfolio=portfolio,
            notifier=telegram.send,
        )
        tasks.append(asyncio.create_task(redistributor.start(), name="redistributor"))
        logger.info("Redistributor scheduled.")

    # Breakout Momentum Bot
    breakout_cfg = config.get("breakout_bot", {})
    if breakout_cfg.get("enabled", False):
        from bots.breakout.bot import BreakoutBot
        breakout_exchange = exchanges.get(breakout_cfg.get("exchange", "bybit"))
        breakout_bot = BreakoutBot(
            exchange=breakout_exchange,
            config=breakout_cfg,
            state_store=state,
            emergency_stop=emergency,
            paper_mode=secrets.is_paper_mode(),
            notifier=telegram.send,
        )
        tasks.append(asyncio.create_task(breakout_bot.start(), name="breakout"))
        telegram.register_bot("breakout", breakout_bot)
        bot_registry["breakout"] = breakout_bot
        logger.info("Breakout Bot scheduled.")

    # Statistical Arbitrage Bot
    stat_arb_cfg = config.get("stat_arb", {})
    if stat_arb_cfg.get("enabled", False):
        from bots.stat_arb.bot import StatArbBot
        stat_arb_exchange = exchanges.get(stat_arb_cfg.get("exchange", "bybit"))
        stat_arb_bot = StatArbBot(
            exchange=stat_arb_exchange,
            config=stat_arb_cfg,
            state_store=state,
            emergency_stop=emergency,
            paper_mode=secrets.is_paper_mode(),
            notifier=telegram.send,
        )
        tasks.append(asyncio.create_task(stat_arb_bot.start(), name="stat_arb"))
        telegram.register_bot("stat_arb", stat_arb_bot)
        bot_registry["stat_arb"] = stat_arb_bot
        logger.info("StatArb Bot scheduled.")

    # TSM Trend-Following Bot (Donchian 55/20, дневки)
    tsm_cfg = config.get("tsm_bot", {})
    if tsm_cfg.get("enabled", False):
        from bots.tsm.bot import TsmBot
        tsm_exchange = exchanges.get(tsm_cfg.get("exchange", "bybit"))
        tsm_bot = TsmBot(
            exchange=tsm_exchange,
            config=tsm_cfg,
            state_store=state,
            emergency_stop=emergency,
            paper_mode=secrets.is_paper_mode(),
            notifier=telegram.send,
        )
        tasks.append(asyncio.create_task(tsm_bot.start(), name="tsm"))
        telegram.register_bot("tsm", tsm_bot)
        bot_registry["tsm"] = tsm_bot
        logger.info("TSM Bot scheduled.")

    # Analyzer Bot (ежедневный отчёт + советник)
    analyzer_cfg = config.get("analyzer", {})
    if analyzer_cfg.get("enabled", True):
        from bots.analyzer.bot import AnalyzerBot
        analyzer = AnalyzerBot(
            state_store=state,
            emergency_stop=emergency,
            portfolio=portfolio,
            config=analyzer_cfg,
            full_config=config,
            notifier=telegram.send,
        )
        for name, bot in bot_registry.items():
            analyzer.register_bot(name, bot)
        tasks.append(asyncio.create_task(analyzer.start(), name="analyzer"))
        logger.info("Analyzer Bot scheduled.")

    logger.info(f"Running {len(tasks)} tasks. System ready.")

    # ------------------------------------------------------------------
    # 8. Graceful shutdown по Ctrl+C или SIGTERM
    # ------------------------------------------------------------------
    loop = asyncio.get_running_loop()

    def shutdown(sig_name: str) -> None:
        logger.warning(f"Received {sig_name}. Shutting down gracefully...")
        for task in tasks:
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda s=sig.name: shutdown(s))  # type: ignore[misc]
        except NotImplementedError:
            # Windows не поддерживает add_signal_handler
            pass

    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await telegram.stop()
        await state.close()
        logger.info("Crypto-Trader stopped.")


if __name__ == "__main__":
    asyncio.run(main())
