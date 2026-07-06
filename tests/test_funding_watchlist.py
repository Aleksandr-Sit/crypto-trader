"""Тесты расширения watchlist FundingArbBot (12_CARRY_WATCHLIST_SPEC):
per-symbol лоты, tickSize, funding-интервал, 8h-нормализация порогов.
Не требуют API/Redis."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from bots.funding_arb.bot import FundingArbBot, FUNDING_INTERVAL_SEC

SUI_META = {
    "spot_qty_step": 0.01, "spot_min_qty": 1.0,
    "perp_qty_step": 10.0, "perp_min_qty": 10.0,
    "perp_tick_size": 0.0001,
    "funding_interval_sec": 8 * 3600.0,
}
ONDO_META = {
    "spot_qty_step": 0.01, "spot_min_qty": 1.0,
    "perp_qty_step": 1.0, "perp_min_qty": 1.0,
    "perp_tick_size": 0.0001,
    "funding_interval_sec": 4 * 3600.0,   # 4h!
}
BTC_META = {
    "spot_qty_step": 0.000001, "spot_min_qty": 0.000048,
    "perp_qty_step": 0.001, "perp_min_qty": 0.001,
    "perp_tick_size": 0.1,
    "funding_interval_sec": 8 * 3600.0,
}


def _make_bot(meta: dict, capital: float = 200.0, price: float = 2.5):
    exchange = MagicMock()
    exchange.get_instrument_meta = AsyncMock(return_value=meta)
    exchange.get_ticker = AsyncMock(return_value=price)
    order = MagicMock()
    order.order_id = "oid-1"
    exchange.place_market_order = AsyncMock(return_value=order)
    exchange.place_perp_market_order = AsyncMock(return_value=order)
    state = MagicMock()
    state.set_bot_state = AsyncMock()
    bot = FundingArbBot(
        exchange=exchange,
        config={"symbols": ["SUIUSDT"], "capital_usdt": capital},
        state_store=state,
        emergency_stop=MagicMock(),
        paper_mode=True,
        notifier=None,
    )
    return bot, exchange


class TestQtySizing:
    def test_qty_floors_to_coarser_step(self):
        """SUI: перп-шаг 10 грубее спотового 0.01 → qty кратно 10."""
        bot, exchange = _make_bot(SUI_META, capital=200.0, price=2.5)
        asyncio.run(bot._enter("SUIUSDT", 0.0004, 2.5, 0.0))
        assert "SUIUSDT" in bot._positions
        qty = bot._positions["SUIUSDT"].spot_qty
        assert qty == 80.0                      # floor(200/2.5=80 → кратно 10)
        assert qty % 10 == 0

    def test_entry_skipped_below_min_qty(self):
        """Дорогая пара с грубым шагом: qty=0 → входа нет, ордера не отправлены."""
        bot, exchange = _make_bot(SUI_META, capital=200.0, price=100.0)
        asyncio.run(bot._enter("SUIUSDT", 0.0004, 100.0, 0.0))  # 200/100=2 < step 10
        assert bot._positions == {}
        exchange.place_market_order.assert_not_awaited()

    def test_entry_skipped_without_meta(self):
        """Метаданные недоступны → вслепую не входим."""
        bot, exchange = _make_bot({})
        asyncio.run(bot._enter("SUIUSDT", 0.0004, 2.5, 0.0))
        assert bot._positions == {}
        exchange.place_market_order.assert_not_awaited()

    def test_btc_legacy_path(self):
        """BTC: прежний сайзинг сохраняется (шаг 0.001 перпа грубее спота)."""
        bot, exchange = _make_bot(BTC_META, capital=200.0, price=63000.0)
        asyncio.run(bot._enter("BTCUSDT", 0.0004, 63000.0, 0.0))
        qty = bot._positions["BTCUSDT"].spot_qty
        assert qty == 0.003                     # floor(0.003174/0.001)

    def test_sl_price_on_tick_grid(self):
        """SL-цена кратна tickSize перпа (вниз = чуть жёстче)."""
        bot, exchange = _make_bot(BTC_META, capital=200.0, price=63001.7)
        asyncio.run(bot._enter("BTCUSDT", 0.0004, 63001.7, 0.0))
        sl = bot._positions["BTCUSDT"].perp_sl_price
        assert sl == pytest.approx(66151.7, abs=0.11)  # ~= 63001.7*1.05
        assert (sl / 0.1) == pytest.approx(round(sl / 0.1))
        assert sl <= 63001.7 * 1.05


class TestFundingIntervals:
    def test_count_periods_8h(self):
        """Регресс: сутки при 8h = 3 расчёта."""
        n = FundingArbBot._count_funding_periods(0.0, 86400.0)
        assert n == 3

    def test_count_periods_4h(self):
        """ONDO-кейс: сутки при 4h = 6 расчётов (старый код давал 3)."""
        n = FundingArbBot._count_funding_periods(0.0, 86400.0, 4 * 3600.0)
        assert n == 6

    def test_interval_stored_in_position(self):
        bot, exchange = _make_bot(ONDO_META, capital=200.0, price=0.5)
        asyncio.run(bot._enter("ONDOUSDT", 0.0002, 0.5, 0.0))
        assert bot._positions["ONDOUSDT"].funding_interval_sec == 4 * 3600.0


class TestThresholdNormalization:
    def test_4h_rate_normalized_for_entry(self):
        """Сырая ставка 0.02%/4h = 0.04%/8h-экв ≥ порога 0.03% → вход есть."""
        bot, exchange = _make_bot(ONDO_META, capital=200.0, price=0.5)
        bot._symbols = ["ONDOUSDT"]
        exchange.get_funding_rates = AsyncMock(return_value={"ONDOUSDT": 0.0002})
        asyncio.run(bot.tick())
        assert "ONDOUSDT" in bot._positions

    def test_8h_rate_below_threshold_no_entry(self):
        """Та же сырая ставка на 8h-паре = 0.02%/8h < 0.03% → входа нет."""
        bot, exchange = _make_bot(SUI_META, capital=200.0, price=2.5)
        exchange.get_funding_rates = AsyncMock(return_value={"SUIUSDT": 0.0002})
        asyncio.run(bot.tick())
        assert bot._positions == {}


class TestStateRoundtrip:
    def test_new_fields_survive_roundtrip(self):
        async def _run():
            bot, exchange = _make_bot(ONDO_META, capital=200.0, price=0.5)
            await bot._enter("ONDOUSDT", 0.0002, 0.5, 0.0)
            snap = await bot.get_state_snapshot()
            bot2, _ = _make_bot(ONDO_META)
            await bot2.restore_state(snap)
            return bot2
        bot2 = asyncio.run(_run())
        pos = bot2._positions["ONDOUSDT"]
        assert pos.funding_interval_sec == 4 * 3600.0
        assert pos.qty_step == 1.0
        assert pos.min_qty == 1.0

    def test_old_snapshot_gets_legacy_defaults(self):
        """Снапшот BTC-эпохи без новых полей → дефолты 8h / BTC-лоты."""
        old = {"positions": {"BTCUSDT": {
            "symbol": "BTCUSDT", "spot_qty": 0.003, "usdt_size": 200.0,
            "spot_entry_price": 62000.0, "perp_entry_price": 62000.0,
            "entry_rate": 0.0004, "entry_time": 0.0, "last_funding_check": 0.0,
        }}}
        bot, _ = _make_bot(BTC_META)
        asyncio.run(bot.restore_state(old))
        pos = bot._positions["BTCUSDT"]
        assert pos.funding_interval_sec == FUNDING_INTERVAL_SEC
        assert pos.min_qty == 0.000048
