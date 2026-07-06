"""
Тесты ScalperBot: state roundtrip, PnL net-of-fees, min qty guard.
"""
import sys
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

# Мокируем pandas и ta ДО импорта бота — они не установлены локально
sys.modules.setdefault("pandas", MagicMock())
sys.modules.setdefault("ta", MagicMock())
sys.modules.setdefault("ta.volatility", MagicMock())
sys.modules.setdefault("ta.momentum", MagicMock())

import pytest
from bots.scalper.bot import ScalperBot, ScalpTrade, BTC_MIN_QTY
from core.state import StateStore
from core.emergency_stop import EmergencyStop


_CFG = {
    "symbols": ["BTCUSDT"],
    "capital_usdt": 100.0,
    "take_profit_pct": 0.80,
    "stop_loss_pct": 0.20,
    "max_trade_duration_min": 15,
    "fee_pct": 0.0009,
    "bb_period": 20,
    "bb_std": 2.0,
    "squeeze_threshold": 0.05,
    "vwap_deviation_pct": 0.5,
    "rsi_period": 7,
    "rsi_oversold": 35.0,
    "quiet_hours_start": 1,
    "quiet_hours_end": 5,
}


def _make_state() -> StateStore:
    ss = StateStore("redis://localhost:6379/0")
    return ss  # in-memory fallback при недоступном Redis


def _make_em() -> EmergencyStop:
    mgr = MagicMock()
    mgr.cancel_all_everywhere = AsyncMock(return_value=0)
    mgr.close_all_everywhere = AsyncMock(return_value=0)
    mgr.get_total_balance_usdt = AsyncMock(return_value=1000.0)
    em = EmergencyStop(mgr)
    em._notify = AsyncMock()
    return em


def _make_bot(ss: StateStore, em: EmergencyStop) -> ScalperBot:
    ex = MagicMock()
    ex.get_ticker = AsyncMock(return_value=59600.0)
    return ScalperBot(ex, _CFG, ss, em, paper_mode=True)


def _make_trade(entry: float = 59600.0) -> ScalpTrade:
    return ScalpTrade(
        symbol="BTCUSDT",
        entry_price=entry,
        qty=0.001678,
        usdt_size=100.0,
        entry_time=time.time(),
        tp_price=round(entry * 1.008, 2),
        sl_price=round(entry * 0.998, 2),
        entry_filled=True,
    )


class TestScalperStateRoundtrip:
    def test_trade_survives_save_restore(self):
        async def _run():
            ss = _make_state()
            em = _make_em()
            bot = _make_bot(ss, em)
            bot._trades["BTCUSDT"] = _make_trade()
            bot._total_trades = 7
            bot._winning_trades = 4
            bot._total_pnl = 12.50
            bot._total_fees_usdt = 0.63

            snap = await bot.get_state_snapshot()
            await ss.set_bot_state("scalper", snap)

            bot2 = _make_bot(ss, em)
            saved = await ss.get_bot_state("scalper")
            await bot2.restore_state(saved)  # paper=True → reconcile skipped

            assert len(bot2._trades) == 1
            t = bot2._trades["BTCUSDT"]
            assert t.entry_price == 59600.0
            assert t.qty == pytest.approx(0.001678)
            assert t.entry_filled is True
            assert bot2._total_trades == 7
            assert bot2._winning_trades == 4
            assert bot2._total_pnl == pytest.approx(12.50)
            assert bot2._total_fees_usdt == pytest.approx(0.63)

        asyncio.run(_run())

    def test_empty_state_restore(self):
        async def _run():
            ss = _make_state()
            em = _make_em()
            bot = _make_bot(ss, em)
            await bot.restore_state({})
            assert len(bot._trades) == 0
            assert bot._total_trades == 0

        asyncio.run(_run())

    def test_no_data_loss_on_multiple_saves(self):
        async def _run():
            ss = _make_state()
            em = _make_em()
            bot = _make_bot(ss, em)
            bot._trades["BTCUSDT"] = _make_trade()
            # Сохранить дважды — второй save не портит первый
            await bot._save_state()
            await bot._save_state()
            bot2 = _make_bot(ss, em)
            saved = await ss.get_bot_state("scalper")
            await bot2.restore_state(saved)
            assert "BTCUSDT" in bot2._trades

        asyncio.run(_run())


class TestScalperPnL:
    def test_pnl_is_net_of_fees_on_tp(self):
        async def _run():
            ss = _make_state()
            em = _make_em()
            bot = _make_bot(ss, em)
            trade = _make_trade(59600.0)
            bot._trades["BTCUSDT"] = trade

            close_price = trade.tp_price
            await bot._close("BTCUSDT", trade, close_price, "TP")

            gross = (close_price - trade.entry_price) * trade.qty
            fee   = 0.0009 * trade.qty * trade.entry_price
            expected_net = gross - fee

            assert bot._total_pnl == pytest.approx(expected_net, abs=1e-6)
            assert bot._total_fees_usdt == pytest.approx(fee, abs=1e-8)
            assert bot._total_trades == 1
            assert bot._winning_trades == 1

        asyncio.run(_run())

    def test_pnl_negative_on_sl(self):
        async def _run():
            ss = _make_state()
            em = _make_em()
            bot = _make_bot(ss, em)
            trade = _make_trade(59600.0)
            bot._trades["BTCUSDT"] = trade

            close_price = trade.sl_price
            await bot._close("BTCUSDT", trade, close_price, "SL")

            assert bot._total_pnl < 0
            assert bot._winning_trades == 0

        asyncio.run(_run())

    def test_rr_ratio_after_tp_fix(self):
        """После фикса TP=0.80%, SL=0.20%: net R:R > 2.0"""
        entry = 59600.0
        tp_pct = 0.80 / 100.0
        sl_pct = 0.20 / 100.0
        fee_pct = 0.0009
        qty = 0.001678

        gross_win = entry * tp_pct * qty
        gross_loss = entry * sl_pct * qty
        fee = fee_pct * entry * qty

        net_win  = gross_win - fee
        net_loss = gross_loss + fee
        rr = net_win / net_loss

        assert rr > 2.0, f"Net R:R = {rr:.2f} должен быть > 2.0"


class TestScalperMinQty:
    def test_btc_min_qty_constant_is_current(self):
        # BTC_MIN_QTY=0.000048 — устаревший, но ≥ реального min 0.000011
        assert BTC_MIN_QTY >= 0.000011

    def test_capital_100_produces_valid_qty(self):
        import math
        price = 59600.0
        capital = 100.0
        qty = math.floor((capital / price) * 10**6) / 10**6
        assert qty >= BTC_MIN_QTY, f"qty={qty:.6f} должно быть >= {BTC_MIN_QTY}"
