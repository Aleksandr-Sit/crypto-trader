"""
Тесты BreakoutBot: PERP_MIN_QTY guard, state roundtrip, PnL net-of-fees.
"""
import sys
import asyncio
import math
import time
from unittest.mock import AsyncMock, MagicMock

sys.modules.setdefault("pandas", MagicMock())
sys.modules.setdefault("ta", MagicMock())
sys.modules.setdefault("ta.volatility", MagicMock())

import pytest
from bots.breakout.bot import BreakoutBot, BreakoutTrade, PERP_MIN_QTY
from core.state import StateStore
from core.emergency_stop import EmergencyStop


_CFG = {
    "symbols": ["BTCUSDT", "ETHUSDT"],
    "capital_usdt": 300.0,
    "leverage": 3,
    "risk_per_trade_pct": 1.5,
    "rr_min": 2.5,
    "max_concurrent": 2,
    "volume_multiplier": 2.5,
    "bb_period": 20,
    "squeeze_threshold": 0.05,
    "atr_sl_mult": 1.0,
    "atr_tp_mult": 2.5,
    "max_trade_duration_h": 4,
    "cooldown_after_exit_h": 2,
    "quiet_hours_start": 1,
    "quiet_hours_end": 5,
    "fee_pct": 0.0011,
}


def _make_em() -> EmergencyStop:
    mgr = MagicMock()
    mgr.cancel_all_everywhere = AsyncMock(return_value=0)
    mgr.close_all_everywhere = AsyncMock(return_value=0)
    mgr.get_total_balance_usdt = AsyncMock(return_value=1000.0)
    em = EmergencyStop(mgr)
    em._notify = AsyncMock()
    return em


def _make_bot(cfg_override: dict | None = None) -> BreakoutBot:
    cfg = dict(_CFG)
    if cfg_override:
        cfg.update(cfg_override)
    ex = MagicMock()
    ss = StateStore("redis://localhost:6379/0")
    em = _make_em()
    return BreakoutBot(ex, cfg, ss, em, paper_mode=True)


def _make_trade(direction: str = "long", entry: float = 59600.0) -> BreakoutTrade:
    atr = entry * 0.008
    if direction == "long":
        tp = round(entry + atr * 2.5, 2)
        sl = round(entry - atr * 1.0, 2)
    else:
        tp = round(entry - atr * 2.5, 2)
        sl = round(entry + atr * 1.0, 2)
    risk_usdt = 300.0 * 0.015
    qty = math.floor((risk_usdt / (atr * 1.0)) * 10**6) / 10**6
    return BreakoutTrade(
        symbol="BTCUSDT",
        direction=direction,
        entry_price=entry,
        qty=qty,
        tp_price=tp,
        sl_price=sl,
        entry_time=time.time(),
    )


class TestPerpMinQty:
    def test_constants_correct(self):
        assert PERP_MIN_QTY["BTCUSDT"] == 0.001
        assert PERP_MIN_QTY["ETHUSDT"] == 0.01

    def test_ok_at_normal_atr(self):
        """ATR 0.8%, BTC $59600, capital $300: qty >> 0.001"""
        risk_usdt = 300.0 * 0.015
        price = 59600.0
        atr = price * 0.008
        qty = math.floor((risk_usdt / atr) * 10**6) / 10**6
        assert qty > PERP_MIN_QTY["BTCUSDT"]

    def test_ok_at_3pct_atr_current_price(self):
        """ATR 3%, BTC $59600: qty=0.001678 > 0.001 → баг не срабатывает"""
        risk_usdt = 300.0 * 0.015
        price = 59600.0
        atr = price * 0.03
        qty = math.floor((risk_usdt / atr) * 10**6) / 10**6
        assert qty > PERP_MIN_QTY["BTCUSDT"], \
            f"При ATR 3%, BTC $59600: qty={qty:.6f} должно быть > 0.001"

    def test_fails_at_extreme_atr(self):
        """Граница срабатывания: risk / (ATR_pct × price) < min_qty.
        При BTC $59600, risk=$4.50: qty < 0.001 когда ATR > 7.55%.
        Тест использует ATR=8% — явно за порогом."""
        risk_usdt = 300.0 * 0.015   # $4.50
        price = 59600.0
        atr = price * 0.08          # 8% ATR — flash crash сценарий
        qty = math.floor((risk_usdt / atr) * 10**6) / 10**6
        assert qty < PERP_MIN_QTY["BTCUSDT"], \
            f"При ATR 8%, BTC $59600: qty={qty:.6f} должно быть < 0.001"

    def test_eth_min_qty_not_triggered_at_realistic_atr(self):
        """ETH perp min = 0.01 ETH. Граница: ATR > 28.7% — практически недостижима.
        При ATR 5% qty >> 0.01 → ETH никогда не блокируется при текущих ценах."""
        risk_usdt = 300.0 * 0.015   # $4.50
        price = 1570.0
        atr = price * 0.05          # 5% ATR
        qty = math.floor((risk_usdt / atr) * 10**6) / 10**6
        assert qty > PERP_MIN_QTY["ETHUSDT"], \
            f"При ATR 5%, ETH $1570: qty={qty:.6f} должно быть > 0.01 (ETH проблемы нет)"


class TestBreakoutPnL:
    def test_long_tp_pnl_net(self):
        async def _run():
            bot = _make_bot()
            trade = _make_trade("long")
            bot._trades["BTCUSDT"] = trade

            await bot._close("BTCUSDT", trade, trade.tp_price, "TP")

            gross = (trade.tp_price - trade.entry_price) * trade.qty
            fee   = 0.0011 * trade.qty * trade.entry_price
            expected = gross - fee

            assert bot._total_pnl == pytest.approx(expected, abs=1e-6)
            assert bot._total_fees_usdt == pytest.approx(fee, abs=1e-8)
            assert bot._win_trades == 1

        asyncio.run(_run())

    def test_short_sl_pnl_negative(self):
        async def _run():
            bot = _make_bot()
            trade = _make_trade("short")
            bot._trades["BTCUSDT"] = trade

            # SL для шорта: цена поднялась выше sl_price
            await bot._close("BTCUSDT", trade, trade.sl_price, "SL")

            assert bot._total_pnl < 0
            assert bot._win_trades == 0

        asyncio.run(_run())

    def test_rr_at_least_25(self):
        """ATR-based TP/SL при atr_tp_mult=2.5, atr_sl_mult=1.0 → R:R ≥ 2.0 net"""
        entry = 59600.0
        atr = entry * 0.008
        fee_pct = 0.0011
        risk_usdt = 300.0 * 0.015
        sl_dist = atr * 1.0
        qty = math.floor((risk_usdt / sl_dist) * 10**6) / 10**6

        gross_win  = atr * 2.5 * qty
        gross_loss = sl_dist * qty
        fee = fee_pct * qty * entry

        rr = (gross_win - fee) / (gross_loss + fee)
        assert rr >= 2.0, f"Net R:R = {rr:.2f}"


class TestBreakoutStateRoundtrip:
    def test_trade_and_counters_survive_roundtrip(self):
        async def _run():
            ss = StateStore("redis://localhost:6379/0")
            bot = _make_bot()
            bot._state = ss

            trade = _make_trade("long")
            bot._trades["BTCUSDT"] = trade
            bot._total_trades = 4
            bot._win_trades = 3
            bot._total_pnl = 18.77
            bot._total_fees_usdt = 1.98

            snap = await bot.get_state_snapshot()
            await ss.set_bot_state("breakout", snap)

            bot2 = _make_bot()
            bot2._state = ss
            saved = await ss.get_bot_state("breakout")
            await bot2.restore_state(saved)   # paper=True → reconcile skipped

            assert len(bot2._trades) == 1
            t2 = bot2._trades["BTCUSDT"]
            assert t2.direction == "long"
            assert t2.entry_price == pytest.approx(59600.0)
            assert t2.qty == pytest.approx(trade.qty)
            assert bot2._total_trades == 4
            assert bot2._win_trades == 3
            assert bot2._total_pnl == pytest.approx(18.77)
            assert bot2._total_fees_usdt == pytest.approx(1.98)

        asyncio.run(_run())

    def test_cooldown_restored(self):
        async def _run():
            ss = StateStore("redis://localhost:6379/0")
            bot = _make_bot()
            bot._state = ss
            future = time.time() + 7200
            bot._cooldown["BTCUSDT"] = future

            await bot._save_state()

            bot2 = _make_bot()
            bot2._state = ss
            saved = await ss.get_bot_state("breakout")
            await bot2.restore_state(saved)

            assert "BTCUSDT" in bot2._cooldown
            assert bot2._cooldown["BTCUSDT"] == pytest.approx(future, abs=1.0)

        asyncio.run(_run())

    def test_capital_config_300(self):
        """Конфиг breakout: capital_usdt=300 после фикса."""
        bot = _make_bot()
        assert bot._capital == 300.0
