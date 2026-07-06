"""Тесты FundingArbBot._reconcile: фантомные позиции после рестарта
(04_ARCHITECTURE §3.3). Не требуют API/Redis."""

import asyncio
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

from bots.funding_arb.bot import FundingArbBot, ArbPosition


@dataclass
class _FakePosition:
    symbol: str


def _make_bot(paper: bool = False):
    exchange = MagicMock()
    state = MagicMock()
    state.set_bot_state = AsyncMock()
    emergency = MagicMock()
    notify = AsyncMock()
    bot = FundingArbBot(
        exchange=exchange,
        config={"symbols": ["BTCUSDT", "ETHUSDT"]},
        state_store=state,
        emergency_stop=emergency,
        paper_mode=paper,
        notifier=notify,
    )
    return bot, exchange, notify


def _pos(symbol: str) -> dict:
    return {
        "symbol": symbol, "spot_qty": 0.003, "usdt_size": 200.0,
        "spot_entry_price": 62000.0, "perp_entry_price": 62000.0,
        "entry_rate": 0.0004, "entry_time": 0.0, "last_funding_check": 0.0,
    }


class TestFundingReconcile:
    def test_phantom_removed_and_alerted(self):
        """Перп-ноги нет на бирже → фантом удалён, владелец оповещён."""
        async def _run():
            bot, exchange, notify = _make_bot(paper=False)
            exchange.get_open_positions = AsyncMock(return_value=[])  # биржа пуста
            await bot.restore_state({"positions": {"BTCUSDT": _pos("BTCUSDT")}})
            return bot, notify

        bot, notify = asyncio.run(_run())
        assert bot._positions == {}, "фантом должен быть удалён"
        notify.assert_awaited()
        assert "BTCUSDT" in notify.await_args.args[0]

    def test_real_position_kept(self):
        """Перп-нога есть на бирже → позиция сохраняется, алерта нет."""
        async def _run():
            bot, exchange, notify = _make_bot(paper=False)
            exchange.get_open_positions = AsyncMock(
                return_value=[_FakePosition("BTCUSDT")]
            )
            await bot.restore_state({"positions": {"BTCUSDT": _pos("BTCUSDT")}})
            return bot, notify

        bot, notify = asyncio.run(_run())
        assert "BTCUSDT" in bot._positions
        notify.assert_not_awaited()

    def test_partial_phantom(self):
        """Из двух позиций на бирже осталась одна → удалена только фантомная."""
        async def _run():
            bot, exchange, notify = _make_bot(paper=False)
            exchange.get_open_positions = AsyncMock(
                return_value=[_FakePosition("ETHUSDT")]
            )
            await bot.restore_state({"positions": {
                "BTCUSDT": _pos("BTCUSDT"),
                "ETHUSDT": _pos("ETHUSDT"),
            }})
            return bot, notify

        bot, notify = asyncio.run(_run())
        assert set(bot._positions) == {"ETHUSDT"}
        notify.assert_awaited_once()

    def test_paper_mode_skips_reconcile(self):
        """Paper mode: реконсиляция не вызывается (нет реальной биржи)."""
        async def _run():
            bot, exchange, notify = _make_bot(paper=True)
            exchange.get_open_positions = AsyncMock(return_value=[])
            await bot.restore_state({"positions": {"BTCUSDT": _pos("BTCUSDT")}})
            return bot, exchange

        bot, exchange = asyncio.run(_run())
        assert "BTCUSDT" in bot._positions, "в paper фантомы не чистим"
        exchange.get_open_positions.assert_not_awaited()

    def test_api_error_keeps_positions(self):
        """Ошибка API при сверке → позиции не трогаем (консервативно)."""
        async def _run():
            bot, exchange, notify = _make_bot(paper=False)
            exchange.get_open_positions = AsyncMock(side_effect=RuntimeError("api down"))
            await bot.restore_state({"positions": {"BTCUSDT": _pos("BTCUSDT")}})
            return bot

        bot = asyncio.run(_run())
        assert "BTCUSDT" in bot._positions
