"""Тесты regime-фильтра Grid: гистерезис ON/OFF, flatten-учёт инвентаря."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from bots.grid.bot import GridBot


def make_bot(rf_enabled=True):
    cfg = {"symbol": "BTCUSDT", "grid_count": 30, "range_pct": 20.0,
           "capital_usdt": 500.0,
           "regime_filter": {"enabled": rf_enabled, "donchian_days": 3}}
    state = MagicMock()
    state.set_bot_state = AsyncMock()
    return GridBot(exchange=MagicMock(), config=cfg, state_store=state,
                   emergency_stop=MagicMock(), paper_mode=True)


def bars(prices, spread=1.0, ts0=1_700_000_000_000):
    return [{"timestamp": ts0 + i * 86_400_000, "open": p, "high": p + spread,
             "low": p - spread, "close": p, "volume": 1.0}
            for i, p in enumerate(prices)]


def run_regime(bot, closes):
    """Прогоняет _refresh_regime на синтетических дневках (+1 формирующийся бар)."""
    async def _run():
        bot._exchange.get_klines = AsyncMock(return_value=bars(closes + [closes[-1]]))
        bot._rf_checked_at = 0.0
        await bot._refresh_regime()
        return bot._regime_allowed
    return asyncio.run(_run())


class TestRegimeHysteresis:
    def test_off_on_breakdown(self):
        bot = make_bot()
        # последний закрытый close (92) < min(low 3 предыдущих)=99-1=98 → OFF
        assert run_regime(bot, [100, 100, 100, 100, 92]) is False

    def test_on_after_breakout(self):
        bot = make_bot()
        bot._regime_allowed = False
        # close (107) > max(high 3 предыдущих)=101 → ON
        assert run_regime(bot, [100, 100, 100, 100, 107]) is True

    def test_holds_state_inside_range(self):
        bot = make_bot()
        bot._regime_allowed = False
        # внутри диапазона — состояние держится
        assert run_regime(bot, [100, 100, 100, 100, 100.2]) is False
        bot._regime_allowed = True
        assert run_regime(bot, [100, 100, 100, 100, 100.2]) is True


class TestFlatten:
    def test_flatten_realizes_inventory_pnl(self):
        async def _run():
            bot = make_bot()
            bot._initialized = True
            bot._btc_inventory = 0.01
            bot._inventory_cost = 0.01 * 60_000.0   # куплено по 60k
            bot._total_profit = 0.0
            await bot._flatten_and_pause(price=54_000.0)  # рынок упал на 10%
            return bot

        bot = asyncio.run(_run())
        assert bot._initialized is False
        assert bot._btc_inventory == 0.0
        assert bot._inventory_cost == 0.0
        # реализованный убыток: (54000-60000)×0.01 − fee(0.001×0.01×54000)
        expected = -60.0 - 0.54
        assert bot._total_profit == pytest.approx(expected, abs=0.01)

    def test_flatten_without_inventory(self):
        async def _run():
            bot = make_bot()
            bot._initialized = True
            bot._active_orders[100.0] = MagicMock()
            await bot._flatten_and_pause(price=50_000.0)
            return bot

        bot = asyncio.run(_run())
        assert bot._initialized is False
        assert bot._active_orders == {}
        assert bot._total_profit == 0.0

    def test_regime_state_roundtrip(self):
        async def _run():
            b1 = make_bot()
            b1._regime_allowed = False
            snap = await b1.get_state_snapshot()
            b2 = make_bot()
            await b2.restore_state(snap)
            return b2
        b2 = asyncio.run(_run())
        assert b2._regime_allowed is False
