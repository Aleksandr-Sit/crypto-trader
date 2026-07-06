"""
Integration-тесты с mock exchange.

Тестируют критические сценарии с подделанными зависимостями:
- State persistence (save → restart → restore)
- Комиссии: fee вычитается из gross PnL, накапливается, переживает рестарт
- Withdraw в NewsSentinel (Level 2 и Level 3)

Запуск: python -m pytest tests/ -v
"""

import asyncio
import sys
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# Патч pandas/ta ДО импорта ботов — pandas недоступен локально (только в Docker)
# ---------------------------------------------------------------------------
_PANDAS_MOCK = MagicMock()
_PANDAS_MOCK.DataFrame = MagicMock(return_value=MagicMock(
    __getitem__=MagicMock(return_value=MagicMock()),
))
sys.modules.setdefault("pandas", _PANDAS_MOCK)
sys.modules.setdefault("ta", MagicMock())
sys.modules.setdefault("ta.volatility", MagicMock())
sys.modules.setdefault("ta.momentum", MagicMock())

SENTINEL_KEYWORDS_PATH = Path(__file__).parent.parent / "sentinel" / "keywords.yaml"


# ---------------------------------------------------------------------------
# Фикстуры — mock-адаптер биржи и StateStore
# ---------------------------------------------------------------------------

def make_exchange(ticker_price: float = 50_000.0) -> MagicMock:
    ex = MagicMock()
    ex.get_ticker              = AsyncMock(return_value=ticker_price)
    ex.get_balance             = AsyncMock(return_value=MagicMock(available=1000.0, total=1000.0))
    ex.place_market_order      = AsyncMock(return_value=MagicMock(order_id="mock-1"))
    ex.place_limit_order       = AsyncMock(return_value=MagicMock(order_id="mock-limit-1"))
    ex.place_perp_market_order = AsyncMock(return_value=MagicMock(order_id="mock-perp-1"))
    ex.close_perp_position     = AsyncMock(return_value=MagicMock(order_id="mock-close-1"))
    ex.cancel_all_orders       = AsyncMock(return_value=0)
    ex.get_open_orders         = AsyncMock(return_value=[])
    ex.get_open_positions      = AsyncMock(return_value=[])
    ex.get_funding_rates       = AsyncMock(return_value={"BTCUSDT": 0.0005})
    ex.get_klines              = AsyncMock(return_value=[
        {"timestamp": i * 3600000, "open": 50000.0, "high": 50500.0,
         "low": 49500.0, "close": 50000.0, "volume": 100.0}
        for i in range(30)
    ])
    ex.set_leverage            = AsyncMock(return_value=None)
    ex.get_adl_rank            = AsyncMock(return_value=0)
    return ex


def make_state_store() -> MagicMock:
    store: dict = {}
    ss = MagicMock()

    async def set_bot_state(name, data):
        store[name] = data

    async def get_bot_state(name):
        return store.get(name)

    ss.set_bot_state = set_bot_state
    ss.get_bot_state = get_bot_state
    ss._store = store
    return ss


def make_emergency() -> MagicMock:
    em = MagicMock()
    em.is_running = MagicMock(return_value=True)
    em.trigger    = AsyncMock()
    return em


# ---------------------------------------------------------------------------
# 1. Grid Bot: state persistence roundtrip
# ---------------------------------------------------------------------------

class TestGridBotStatePersistence:

    @staticmethod
    def make_bot(exchange, state_store, emergency, paper=True):
        from bots.grid.bot import GridBot
        config = {
            "symbol": "BTCUSDT", "grid_count": 10, "range_pct": 10.0,
            "hard_stop_below_range": True, "rebalance_interval_h": 24.0,
            "capital_usdt": 1000.0, "fee_pct": -0.0002,
        }
        return GridBot(
            exchange=exchange, config=config,
            state_store=state_store, emergency_stop=emergency, paper_mode=paper,
        )

    def test_snapshot_contains_all_fields(self):
        bot = self.make_bot(make_exchange(), make_state_store(), make_emergency())
        bot._grid_low = 45000.0
        bot._grid_high = 55000.0
        bot._grid_step = 1000.0
        bot._capital_usdt = 1000.0
        bot._qty_per_level = 0.01
        bot._initialized = True
        bot._total_trades = 5
        bot._total_profit = 50.0

        snap = asyncio.run(bot.get_state_snapshot())
        assert snap is not None
        for key in ("grid_low", "grid_high", "grid_step", "capital_usdt",
                    "qty_per_level", "initialized", "total_trades",
                    "total_profit", "total_fees_usdt", "active_orders"):
            assert key in snap, f"Missing key in snapshot: {key}"

    def test_restore_state_roundtrip(self):
        ex = make_exchange()
        ss = make_state_store()
        em = make_emergency()
        bot1 = self.make_bot(ex, ss, em)
        bot1._grid_low = 45000.0
        bot1._grid_high = 55000.0
        bot1._grid_step = 1000.0
        bot1._capital_usdt = 1000.0
        bot1._qty_per_level = 0.01
        bot1._initialized = True
        bot1._total_trades = 7
        bot1._total_profit = 77.0
        bot1._total_fees_usdt = -3.5

        async def _run():
            snap = await bot1.get_state_snapshot()
            await ss.set_bot_state("grid_bot", snap)
            bot2 = self.make_bot(ex, ss, em)
            saved = await ss.get_bot_state("grid_bot")
            await bot2.restore_state(saved)
            return bot2

        bot2 = asyncio.run(_run())
        assert bot2._grid_low          == 45000.0
        assert bot2._initialized       is True
        assert bot2._total_trades      == 7
        assert bot2._total_profit      == pytest.approx(77.0)
        assert bot2._total_fees_usdt   == pytest.approx(-3.5)


# ---------------------------------------------------------------------------
# 2. FundingArb: fee tracking и state roundtrip
# ---------------------------------------------------------------------------

class TestFundingArbFees:

    @staticmethod
    def make_bot(exchange, state_store, emergency, paper=True):
        from bots.funding_arb.bot import FundingArbBot
        config = {
            "symbols": ["BTCUSDT"], "capital_usdt": 300.0, "max_positions": 2,
            "entry_rate_threshold": 0.0003, "exit_rate_threshold": 0.0001,
            "negative_rate_exit_periods": 2, "stop_loss_pct": 2.0,
            "adl_reduce_threshold": 4, "adl_close_threshold": 5, "fee_pct": 0.0040,
        }
        return FundingArbBot(
            exchange=exchange, config=config,
            state_store=state_store, emergency_stop=emergency, paper_mode=paper,
        )

    def test_fee_pct_loaded_from_config(self):
        bot = self.make_bot(make_exchange(), make_state_store(), make_emergency())
        assert bot._fee_pct == pytest.approx(0.0040)

    def test_total_fees_usdt_in_snapshot(self):
        bot = self.make_bot(make_exchange(), make_state_store(), make_emergency())
        bot._total_fees_usdt = 1.23
        snap = asyncio.run(bot.get_state_snapshot())
        assert "total_fees_usdt" in snap
        assert snap["total_fees_usdt"] == pytest.approx(1.23)

    def test_fees_restored_after_restart(self):
        ss = make_state_store()

        async def _run():
            bot1 = self.make_bot(make_exchange(), ss, make_emergency())
            bot1._total_fees_usdt = 5.55
            snap = await bot1.get_state_snapshot()
            await ss.set_bot_state("funding_arb", snap)
            bot2 = self.make_bot(make_exchange(), ss, make_emergency())
            saved = await ss.get_bot_state("funding_arb")
            await bot2.restore_state(saved)
            return bot2

        bot2 = asyncio.run(_run())
        assert bot2._total_fees_usdt == pytest.approx(5.55)


# ---------------------------------------------------------------------------
# 3. Scalper: fee deduction в _close() + state roundtrip
# ---------------------------------------------------------------------------

class TestScalperFees:

    @staticmethod
    def make_bot(exchange, state_store, emergency, paper=True):
        from bots.scalper.bot import ScalperBot
        config = {
            "symbols": ["BTCUSDT"], "capital_usdt": 200.0, "bb_period": 20,
            "bb_std": 2.0, "squeeze_threshold": 0.05, "vwap_deviation_pct": 0.5,
            "rsi_period": 7, "rsi_oversold": 35, "take_profit_pct": 0.40,
            "stop_loss_pct": 0.20, "max_trade_duration_min": 15,
            "quiet_hours_start": 1, "quiet_hours_end": 5, "fee_pct": 0.0009,
        }
        return ScalperBot(
            exchange=exchange, config=config,
            state_store=state_store, emergency_stop=emergency, paper_mode=paper,
        )

    def test_fee_pct_loaded_from_config(self):
        bot = self.make_bot(make_exchange(), make_state_store(), make_emergency())
        assert bot._fee_pct == pytest.approx(0.0009)

    def test_close_deducts_fee_from_pnl(self):
        from bots.scalper.bot import ScalpTrade

        async def _run():
            bot = self.make_bot(make_exchange(), make_state_store(), make_emergency(), paper=True)
            trade = ScalpTrade(
                symbol="BTCUSDT", entry_price=50_000.0, qty=0.004,
                usdt_size=200.0, entry_time=0.0, tp_price=50_200.0,
                sl_price=49_900.0, entry_filled=True,
            )
            bot._trades["BTCUSDT"] = trade
            await bot._close("BTCUSDT", trade, 50_200.0, "TP")
            return bot

        bot = asyncio.run(_run())
        gross = (50_200.0 - 50_000.0) * 0.004   # 0.80
        fee   = 0.0009 * 0.004 * 50_000.0        # 0.18
        assert bot._total_pnl       == pytest.approx(gross - fee, abs=1e-6)
        assert bot._total_fees_usdt == pytest.approx(fee, abs=1e-6)
        assert bot._total_trades    == 1
        assert bot._winning_trades  == 1

    def test_fees_in_snapshot_and_restore(self):
        ss = make_state_store()

        async def _run():
            bot1 = self.make_bot(make_exchange(), ss, make_emergency())
            bot1._total_fees_usdt = 2.22
            snap = await bot1.get_state_snapshot()
            assert "total_fees_usdt" in snap
            await ss.set_bot_state("scalper", snap)
            bot2 = self.make_bot(make_exchange(), ss, make_emergency())
            saved = await ss.get_bot_state("scalper")
            await bot2.restore_state(saved)
            return bot2

        bot2 = asyncio.run(_run())
        assert bot2._total_fees_usdt == pytest.approx(2.22)


# ---------------------------------------------------------------------------
# 4. StatArb: fee deduction
# ---------------------------------------------------------------------------

class TestStatArbFees:

    @staticmethod
    def make_bot(exchange, state_store, emergency, paper=True):
        from bots.stat_arb.bot import StatArbBot
        config = {
            "pair_a": "ETHUSDT", "pair_b": "BTCUSDT", "lookback_periods": 20,
            "entry_zscore": 2.0, "exit_zscore": 0.3, "stop_zscore": 3.5,
            "capital_per_leg": 400.0, "leverage": 3, "max_hold_hours": 48,
            "fee_pct": 0.0040,
        }
        return StatArbBot(
            exchange=exchange, config=config,
            state_store=state_store, emergency_stop=emergency, paper_mode=paper,
        )

    def test_fee_pct_loaded_from_config(self):
        bot = self.make_bot(make_exchange(), make_state_store(), make_emergency())
        assert bot._fee_pct == pytest.approx(0.0040)

    def test_exit_deducts_fee(self):
        from bots.stat_arb.bot import StatArbPosition

        async def _run():
            bot = self.make_bot(make_exchange(), make_state_store(), make_emergency(), paper=True)
            bot._position = StatArbPosition(
                direction="short_a_long_b", entry_zscore=2.1,
                qty_a=0.024, qty_b=0.0012,
                entry_price_a=2000.0, entry_price_b=50000.0, entry_time=0.0,
            )
            # Цены не изменились → gross PnL = 0, только комиссия
            await bot._exit("zero PnL test", 2000.0, 50000.0)
            return bot

        bot = asyncio.run(_run())
        expected_fee = 0.0040 * 400.0 * 3 * 2   # fee_pct * capital * leverage * 2 legs
        assert bot._total_fees_usdt == pytest.approx(expected_fee, abs=0.01)
        assert bot._total_pnl       == pytest.approx(-expected_fee, abs=0.01)

    def test_fees_in_snapshot_and_restore(self):
        ss = make_state_store()

        async def _run():
            bot1 = self.make_bot(make_exchange(), ss, make_emergency())
            bot1._total_fees_usdt = 9.60
            snap = await bot1.get_state_snapshot()
            assert "total_fees_usdt" in snap
            await ss.set_bot_state("stat_arb", snap)
            bot2 = self.make_bot(make_exchange(), ss, make_emergency())
            saved = await ss.get_bot_state("stat_arb")
            await bot2.restore_state(saved)
            return bot2

        bot2 = asyncio.run(_run())
        assert bot2._total_fees_usdt == pytest.approx(9.60)


# ---------------------------------------------------------------------------
# 5. Breakout: fee deduction в _close()
# ---------------------------------------------------------------------------

class TestBreakoutFees:

    @staticmethod
    def make_bot(exchange, state_store, emergency, paper=True):
        from bots.breakout.bot import BreakoutBot
        config = {
            "symbols": ["BTCUSDT"], "capital_usdt": 500.0, "leverage": 5,
            "risk_per_trade_pct": 1.5, "rr_min": 2.5, "max_concurrent": 3,
            "volume_multiplier": 2.5, "bb_period": 20, "squeeze_threshold": 0.05,
            "atr_sl_mult": 1.0, "atr_tp_mult": 2.5, "max_trade_duration_h": 4,
            "cooldown_after_exit_h": 2, "quiet_hours_start": 1, "quiet_hours_end": 5,
            "fee_pct": 0.0022,
        }
        return BreakoutBot(
            exchange=exchange, config=config,
            state_store=state_store, emergency_stop=emergency, paper_mode=paper,
        )

    def test_fee_pct_loaded_from_config(self):
        bot = self.make_bot(make_exchange(), make_state_store(), make_emergency())
        assert bot._fee_pct == pytest.approx(0.0022)

    def test_close_long_tp_deducts_fee(self):
        from bots.breakout.bot import BreakoutTrade

        async def _run():
            bot = self.make_bot(make_exchange(), make_state_store(), make_emergency(), paper=True)
            trade = BreakoutTrade(
                symbol="BTCUSDT", direction="long", entry_price=50_000.0,
                qty=0.001, tp_price=51_250.0, sl_price=49_500.0, entry_time=0.0,
            )
            bot._trades["BTCUSDT"] = trade
            await bot._close("BTCUSDT", trade, 51_250.0, "TP")
            return bot

        bot = asyncio.run(_run())
        gross = (51_250.0 - 50_000.0) * 0.001   # 1.25
        fee   = 0.0022 * 0.001 * 50_000.0        # 0.11
        assert bot._total_pnl       == pytest.approx(gross - fee, abs=1e-6)
        assert bot._total_fees_usdt == pytest.approx(fee, abs=1e-6)

    def test_fees_in_snapshot_and_restore(self):
        ss = make_state_store()

        async def _run():
            bot1 = self.make_bot(make_exchange(), ss, make_emergency())
            bot1._total_fees_usdt = 3.14
            snap = await bot1.get_state_snapshot()
            assert "total_fees_usdt" in snap
            await ss.set_bot_state("breakout", snap)
            bot2 = self.make_bot(make_exchange(), ss, make_emergency())
            saved = await ss.get_bot_state("breakout")
            await bot2.restore_state(saved)
            return bot2

        bot2 = asyncio.run(_run())
        assert bot2._total_fees_usdt == pytest.approx(3.14)


# ---------------------------------------------------------------------------
# 6. NewsSentinel: withdraw вызывается при Level 2 и Level 3
# ---------------------------------------------------------------------------

class TestSentinelWithdraw:

    @staticmethod
    def make_net(ok: bool = True):
        from core.withdrawal_router import NetworkOption
        net = NetworkOption(chain="TRC20", address="TXXXXXXXXXXXXXXXXXXXXXXXXXXwithdraw", fee=1.0)
        return (ok, net)

    @staticmethod
    def make_sentinel(exchange_manager):
        from sentinel.news_sentinel import NewsSentinel
        s = NewsSentinel(
            cryptopanic_key=None,
            keywords_path=SENTINEL_KEYWORDS_PATH,
        )
        s.set_exchange_manager(exchange_manager)
        s.set_notifier(AsyncMock())
        return s

    def test_level2_calls_withdraw(self):
        em = MagicMock()
        em.get_total_balance_usdt = AsyncMock(return_value=1000.0)
        em.withdraw_smart         = AsyncMock(return_value=self.make_net(True))
        sentinel = self.make_sentinel(em)

        asyncio.run(sentinel._execute_level2())

        em.withdraw_smart.assert_awaited_once()
        coin, amount = em.withdraw_smart.call_args[0][:2]
        assert coin   == "USDT"
        assert amount == pytest.approx(300.0)   # 30% от 1000

    def test_level3_triggers_emergency_and_withdraw(self):
        em = MagicMock()
        em.get_total_balance_usdt = AsyncMock(return_value=500.0)
        em.withdraw_smart         = AsyncMock(return_value=self.make_net(True))
        emergency_mock            = MagicMock()
        emergency_mock.trigger    = AsyncMock()
        sentinel = self.make_sentinel(em)
        sentinel.set_emergency_stop(emergency_mock)

        asyncio.run(sentinel._execute_level3())

        emergency_mock.trigger.assert_awaited_once()
        em.withdraw_smart.assert_awaited_once()
        _, amount = em.withdraw_smart.call_args[0][:2]
        assert amount == pytest.approx(490.0)   # 500 - 10 = 490

    def test_level2_sends_notification_on_success(self):
        em = MagicMock()
        em.get_total_balance_usdt = AsyncMock(return_value=200.0)
        em.withdraw_smart         = AsyncMock(return_value=self.make_net(True))
        notify_mock               = AsyncMock()
        sentinel = self.make_sentinel(em)
        sentinel.set_notifier(notify_mock)

        asyncio.run(sentinel._execute_level2())
        notify_mock.assert_awaited()

    def test_level2_error_notification_on_failed_withdraw(self):
        em = MagicMock()
        em.get_total_balance_usdt = AsyncMock(return_value=200.0)
        em.withdraw_smart         = AsyncMock(return_value=self.make_net(False))
        notify_mock               = AsyncMock()
        sentinel = self.make_sentinel(em)
        sentinel.set_notifier(notify_mock)

        asyncio.run(sentinel._execute_level2())
        notify_mock.assert_awaited()
        text = notify_mock.call_args[0][0].lower()
        assert "ошибка" in text or "error" in text or "не выполнен" in text


# ---------------------------------------------------------------------------
# 7. ExchangeManager.withdraw — delegation к primary adapter
# ---------------------------------------------------------------------------

class TestExchangeManagerWithdraw:

    def test_withdraw_delegates_to_primary_adapter(self):
        from core.exchange import ExchangeManager
        with patch.object(ExchangeManager, "__init__", lambda self, *a, **kw: None):
            manager = ExchangeManager.__new__(ExchangeManager)
            mock_adapter = MagicMock()
            mock_adapter.withdraw = AsyncMock(return_value=True)
            manager.adapters = {"bybit": mock_adapter}
            manager.primary  = "bybit"

            result = asyncio.run(manager.withdraw("USDT", 100.0, "TXXXXaddr", "TRC20"))
            mock_adapter.withdraw.assert_awaited_once_with("USDT", 100.0, "TXXXXaddr", "TRC20")
            assert result is True
