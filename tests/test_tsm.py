"""Тесты TSM-бота: сигналы Donchian, risk-based сайзинг, state roundtrip.
Pure-logic, без API/Redis."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from bots.tsm.bot import TsmBot, PERP_MIN_QTY


def make_bot(**cfg_over):
    cfg = {"symbols": ["BTCUSDT"], "capital_usdt": 200.0,
           "risk_per_trade_usd": 5.0, "entry_period": 5, "exit_period": 3,
           "atr_period": 3, "atr_stop_mult": 2.0, "fee_pct": 0.0011}
    cfg.update(cfg_over)
    state = MagicMock()
    state.set_bot_state = AsyncMock()
    return TsmBot(exchange=MagicMock(), config=cfg, state_store=state,
                  emergency_stop=MagicMock(), paper_mode=True,
                  notifier=AsyncMock())


def bars(prices, spread=1.0, ts0=1_700_000_000_000):
    """Синтетические дневные бары вокруг списка close-цен."""
    out = []
    for i, p in enumerate(prices):
        out.append({"timestamp": ts0 + i * 86_400_000, "open": p,
                    "high": p + spread, "low": p - spread,
                    "close": p, "volume": 1.0})
    return out


class TestSignals:
    def test_long_entry_on_breakout(self):
        bot = make_bot()
        # 6 плоских баров (high=101), последний закрылся выше их максимума
        closed = bars([100] * 6 + [105])
        sig = bot._signals(closed)
        assert sig["long_in"] is True
        assert sig["short_in"] is False

    def test_short_entry_on_breakdown(self):
        bot = make_bot()
        closed = bars([100] * 6 + [94])
        sig = bot._signals(closed)
        assert sig["short_in"] is True
        assert sig["long_in"] is False

    def test_no_signal_inside_range(self):
        bot = make_bot()
        closed = bars([100] * 6 + [100.5])
        sig = bot._signals(closed)
        assert not sig["long_in"] and not sig["short_in"]

    def test_long_exit_on_low_break(self):
        bot = make_bot()
        # последний close ниже min(low 3 предыдущих) → выход из лонга
        closed = bars([100, 100, 100, 100, 100, 100] + [97])
        sig = bot._signals(closed)
        assert sig["long_out"] is True


class TestSizing:
    def test_risk_based_qty(self):
        bot = make_bot()
        # ATR=500, стоп 2×500=1000 → qty = 5/1000 = 0.005 BTC (кап 200/50000=0.004!)
        qty = bot._size(price=50_000.0, atr=500.0, symbol="BTCUSDT")
        assert qty == pytest.approx(0.004, abs=1e-9), "кап 1× слота должен сработать"

    def test_cap_not_binding_with_wide_stop(self):
        bot = make_bot()
        # ATR=2500 → стоп 5000 → qty_risk = 0.001 < кап 0.004 → берём risk-based
        qty = bot._size(price=50_000.0, atr=2_500.0, symbol="BTCUSDT")
        assert qty == pytest.approx(0.001, abs=1e-9)

    def test_below_min_qty_rejected(self):
        bot = make_bot()
        # слишком широкий стоп → qty < биржевого минимума → 0
        qty = bot._size(price=50_000.0, atr=10_000.0, symbol="BTCUSDT")
        assert qty == 0.0
        assert PERP_MIN_QTY["BTCUSDT"] == 0.001

    def test_zero_atr_rejected(self):
        bot = make_bot()
        assert bot._size(price=50_000.0, atr=0.0, symbol="BTCUSDT") == 0.0


class TestStateRoundtrip:
    def test_snapshot_restore(self):
        async def _run():
            b1 = make_bot()
            b1._total_trades, b1._win_trades = 7, 3
            b1._total_pnl, b1._total_fees_usdt = 42.5, 1.25
            b1._last_entry_bar = {"BTCUSDT": 1_700_000_000_000}
            from bots.tsm.bot import TsmTrade
            b1._trades["BTCUSDT"] = TsmTrade(
                symbol="BTCUSDT", direction="short", entry_price=60_000.0,
                qty=0.002, stop_price=62_400.0, entry_time=123.0,
                entry_bar_ts=1_700_000_000_000)
            snap = await b1.get_state_snapshot()
            b2 = make_bot()
            await b2.restore_state(snap)
            return b1, b2

        b1, b2 = asyncio.run(_run())
        assert b2._total_trades == 7 and b2._win_trades == 3
        assert b2._total_pnl == pytest.approx(42.5)
        assert b2._total_fees_usdt == pytest.approx(1.25)
        assert b2._last_entry_bar == {"BTCUSDT": 1_700_000_000_000}
        t = b2._trades["BTCUSDT"]
        assert t.direction == "short" and t.stop_price == pytest.approx(62_400.0)

    def test_dedup_same_bar(self):
        """Повторный вход на том же дневном баре запрещён."""
        async def _run():
            bot = make_bot()
            closed = bars([100] * 6 + [105])
            sig_ts = bot._signals(closed)["bar_ts"]
            bot._last_entry_bar["BTCUSDT"] = sig_ts
            await bot._check_entry("BTCUSDT", closed, 105.0)
            return bot

        bot = asyncio.run(_run())
        assert "BTCUSDT" not in bot._trades, "dedup по бару должен блокировать вход"
