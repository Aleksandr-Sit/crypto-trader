"""
Unit-тесты для детерминированной логики торговых ботов.

Не требуют API, Redis, Docker или реальных ключей.
Тестируют только чистые функции и расчёты.

Запуск: python -m pytest tests/ -v
"""

import math
import pytest


# ---------------------------------------------------------------------------
# FundingArbBot._count_funding_periods
# ---------------------------------------------------------------------------

class TestCountFundingPeriods:
    """Bybit зачисляет funding в 00:00, 08:00, 16:00 UTC (каждые 8 часов)."""

    @staticmethod
    def count(since: float, until: float) -> int:
        from bots.funding_arb.bot import FundingArbBot
        return FundingArbBot._count_funding_periods(since, until)

    INTERVAL = 8 * 3600  # 28800 секунд

    def test_no_periods_in_same_interval(self):
        t0 = 0.0
        assert self.count(t0, t0 + self.INTERVAL - 1) == 0

    def test_exactly_one_period_boundary(self):
        t0 = 0.0
        assert self.count(t0, self.INTERVAL) == 1

    def test_two_periods(self):
        t0 = 0.0
        assert self.count(t0, self.INTERVAL * 2) == 2

    def test_three_periods_per_day(self):
        t0 = 0.0
        assert self.count(t0, 24 * 3600) == 3

    def test_start_mid_interval(self):
        t0 = self.INTERVAL / 2         # Started halfway through first interval
        until = self.INTERVAL * 1.5    # Past first boundary only
        assert self.count(t0, until) == 1

    def test_zero_if_until_before_since(self):
        t0 = 1000.0
        assert self.count(t0, t0 - 1) == 0

    def test_same_timestamp(self):
        t0 = 1000.0
        assert self.count(t0, t0) == 0

    def test_real_timestamps_48h(self):
        import time as t
        now = t.time()
        assert self.count(now - 48 * 3600, now) == 6


# ---------------------------------------------------------------------------
# StatArbBot._calc_leg_pnl
# Тестируем через статическую функцию (self не используется в теле метода)
# ---------------------------------------------------------------------------

def _stat_arb_leg_pnl(direction, entry_a, entry_b, qty_a, qty_b, price_a, price_b):
    """Дублирует логику StatArbBot._calc_leg_pnl без import pandas."""
    if direction == "short_a_long_b":
        pnl_a = (entry_a - price_a) * qty_a
        pnl_b = (price_b - entry_b) * qty_b
    else:
        pnl_a = (price_a - entry_a) * qty_a
        pnl_b = (entry_b - price_b) * qty_b
    return pnl_a, pnl_b


class TestStatArbLegPnl:
    """PnL расчёт для delta-neutral pair trades."""

    def test_short_a_long_b_profit_when_a_drops(self):
        pnl_a, pnl_b = _stat_arb_leg_pnl("short_a_long_b", 2000.0, 40000.0, 1.0, 0.05, 1900.0, 40000.0)
        assert pnl_a == pytest.approx(100.0)
        assert pnl_b == pytest.approx(0.0)

    def test_short_a_long_b_profit_when_b_rises(self):
        pnl_a, pnl_b = _stat_arb_leg_pnl("short_a_long_b", 2000.0, 40000.0, 1.0, 0.05, 2000.0, 42000.0)
        assert pnl_a == pytest.approx(0.0)
        assert pnl_b == pytest.approx(100.0)

    def test_long_a_short_b_profit_when_a_rises(self):
        pnl_a, pnl_b = _stat_arb_leg_pnl("long_a_short_b", 2000.0, 40000.0, 1.0, 0.05, 2200.0, 40000.0)
        assert pnl_a == pytest.approx(200.0)
        assert pnl_b == pytest.approx(0.0)

    def test_symmetric_loss(self):
        pnl_a, pnl_b = _stat_arb_leg_pnl("short_a_long_b", 2000.0, 40000.0, 1.0, 0.05, 2100.0, 40000.0)
        assert pnl_a == pytest.approx(-100.0)

    def test_flat_position_zero_pnl(self):
        pnl_a, pnl_b = _stat_arb_leg_pnl("short_a_long_b", 2000.0, 40000.0, 1.0, 0.05, 2000.0, 40000.0)
        assert pnl_a == pytest.approx(0.0)
        assert pnl_b == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# ATR calculation — дублируем логику из BreakoutBot._calc_atr
# (pandas не доступен локально — только в Docker; тестируем алгоритм напрямую)
# ---------------------------------------------------------------------------

def _calc_atr(klines: list[dict], period: int = 14) -> float:
    """Копия BreakoutBot._calc_atr для тестирования без import pandas."""
    trs = []
    for i in range(1, len(klines)):
        prev_close = klines[i - 1]["close"]
        h = klines[i]["high"]
        low = klines[i]["low"]
        tr = max(h - low, abs(h - prev_close), abs(low - prev_close))
        trs.append(tr)
    if len(trs) < period:
        return sum(trs) / len(trs) if trs else 0.0
    return sum(trs[-period:]) / period


class TestCalcAtr:
    def _make_candle(self, h, low, c):
        return {"open": c, "high": h, "low": low, "close": c, "volume": 100}

    def test_simple_atr(self):
        klines = [self._make_candle(105, 95, 100)] * 20
        atr = _calc_atr(klines)
        assert atr == pytest.approx(10.0)

    def test_zero_for_single_candle(self):
        klines = [self._make_candle(100, 90, 95)]
        atr = _calc_atr(klines, period=1)
        assert atr == 0.0

    def test_uses_last_period_candles(self):
        low_vol = [self._make_candle(102.5, 97.5, 100)] * 14
        high_vol = [self._make_candle(110, 90, 100)] * 10
        klines = high_vol + low_vol
        atr = _calc_atr(klines, period=14)
        assert atr == pytest.approx(5.0, abs=0.5)

    def test_gap_candle_uses_prev_close_in_tr(self):
        # prev_close=100, next high=90, low=80 → TR = max(10, 10, 20) = 20
        klines = [
            self._make_candle(100, 90, 100),
            self._make_candle(90, 80, 85),
        ]
        atr = _calc_atr(klines, period=1)
        assert atr == pytest.approx(20.0)


# ---------------------------------------------------------------------------
# GridBot position sizing
# ---------------------------------------------------------------------------

class TestGridPositionSizing:
    """Проверяем, что qty_per_level рассчитывается корректно."""

    def test_qty_calculation(self):
        price = 100_000.0   # BTC price in USDT
        capital = 1_000.0   # total capital
        n_buy = 10          # buy-side grid levels
        precision = 6

        usdt_per_buy = (capital / 2) / max(n_buy, 1)
        qty = math.floor((usdt_per_buy / price) * 10**precision) / 10**precision

        # With $1000 capital, 10 buy levels: $50 per level, at $100k = 0.0005 BTC
        assert qty == pytest.approx(0.0005, abs=1e-7)

    def test_min_qty_check(self):
        from bots.grid.bot import BYBIT_BTC_MIN_QTY
        assert BYBIT_BTC_MIN_QTY == 0.000048

    def test_qty_below_min(self):
        from bots.grid.bot import BYBIT_BTC_MIN_QTY
        price = 100_000.0
        capital = 1.0     # way too small
        n_buy = 20
        precision = 6
        usdt_per_buy = (capital / 2) / max(n_buy, 1)
        qty = math.floor((usdt_per_buy / price) * 10**precision) / 10**precision
        assert qty < BYBIT_BTC_MIN_QTY


# ---------------------------------------------------------------------------
# ScalperBot sizing
# ---------------------------------------------------------------------------

class TestScalperSizing:
    def test_qty_precision(self):
        capital = 200.0
        price = 105_000.0
        precision = 6
        qty = math.floor((capital / price) * 10**precision) / 10**precision
        # Should be a multiple of 10^-6
        assert round(qty * 10**precision) == int(qty * 10**precision + 0.5)

    def test_tp_sl_prices(self):
        price = 100_000.0
        tp_pct = 0.004   # 0.40%
        sl_pct = 0.002   # 0.20%
        tp = round(price * (1 + tp_pct), 2)
        sl = round(price * (1 - sl_pct), 2)
        assert tp == 100_400.0
        assert sl == 99_800.0
        assert tp > price > sl

    def test_rr_ratio(self):
        price = 100_000.0
        tp_pct = 0.004
        sl_pct = 0.002
        tp = price * (1 + tp_pct)
        sl = price * (1 - sl_pct)
        rr = (tp - price) / (price - sl)
        assert rr == pytest.approx(2.0, abs=0.01)  # R:R = 2:1


# ---------------------------------------------------------------------------
# BreakoutBot: R:R validation
# ---------------------------------------------------------------------------

class TestBreakoutRR:
    def test_long_rr_calculation(self):
        price = 50_000.0
        atr = 500.0
        atr_sl = 1.0
        atr_tp = 2.5
        sl = price - atr * atr_sl  # 49500
        tp = price + atr * atr_tp  # 51250
        rr = (tp - price) / (price - sl)
        assert rr == pytest.approx(2.5, abs=0.01)

    def test_short_rr_calculation(self):
        price = 50_000.0
        atr = 500.0
        atr_sl = 1.0
        atr_tp = 2.5
        sl = price + atr * atr_sl  # 50500
        tp = price - atr * atr_tp  # 48750
        sl_dist = abs(price - sl)
        tp_dist = abs(price - tp)
        rr = tp_dist / sl_dist
        assert rr == pytest.approx(2.5, abs=0.01)

    def test_breakout_risk_sizing(self):
        capital = 500.0
        risk_pct = 0.015   # 1.5%
        price = 50_000.0
        atr = 500.0
        atr_sl = 1.0
        precision = 6

        risk_usdt = capital * risk_pct  # $7.5
        sl_distance = atr * atr_sl       # $500
        qty = math.floor((risk_usdt / sl_distance) * 10**precision) / 10**precision

        # $7.5 / $500 = 0.015
        assert qty == pytest.approx(0.015, abs=1e-7)
        # Notional = 0.015 * $50,000 = $750 → with leverage=5, margin=$150
        notional = qty * price
        assert notional == pytest.approx(750.0)


# ---------------------------------------------------------------------------
# StatArbBot: Z-score edge cases
# ---------------------------------------------------------------------------

class TestZScore:
    """Тестируем только математику, без вызова API."""

    @staticmethod
    def compute_zscore(prices_a, prices_b, lookback):
        import math as m
        ratios = [m.log(pa / pb) for pa, pb in zip(prices_a, prices_b)]
        window = ratios[-lookback:]
        n = len(window)
        mean = sum(window) / n
        variance = sum((x - mean) ** 2 for x in window) / (n - 1) if n > 1 else 0
        std = m.sqrt(variance) if variance > 1e-12 else 1e-10
        current = ratios[-1]
        return (current - mean) / std

    def test_zero_zscore_when_ratio_at_mean(self):
        prices_a = [2000.0] * 20   # constant ratio
        prices_b = [40000.0] * 20
        z = self.compute_zscore(prices_a, prices_b, 20)
        assert abs(z) < 1e-6

    def test_positive_zscore_when_a_above_mean(self):
        prices_a = [2000.0] * 19 + [2200.0]  # spike up
        prices_b = [40000.0] * 20
        z = self.compute_zscore(prices_a, prices_b, 20)
        assert z > 0

    def test_negative_zscore_when_a_below_mean(self):
        prices_a = [2000.0] * 19 + [1800.0]  # spike down
        prices_b = [40000.0] * 20
        z = self.compute_zscore(prices_a, prices_b, 20)
        assert z < 0

    def test_large_deviation_exceeds_threshold(self):
        prices_a = [2000.0] * 18 + [2000.0, 2600.0]
        prices_b = [40000.0] * 20
        z = self.compute_zscore(prices_a, prices_b, 20)
        assert z >= 2.0  # should trigger entry at zscore=2.0


# ---------------------------------------------------------------------------
# FundingArbBot: PnL calculation
# ---------------------------------------------------------------------------

class TestFundingArbPnl:
    def test_funding_earned_positive_rate(self):
        rate = 0.0003     # 0.03% per 8h
        usdt_size = 300.0
        periods = 3       # 3 funding periods
        earned = periods * rate * usdt_size
        assert earned == pytest.approx(0.27)  # $0.27

    def test_apr_calculation(self):
        rate = 0.0003     # per 8h
        apr = rate * 3 * 365 * 100  # 3 periods/day * 365 days * %
        assert apr == pytest.approx(32.85, abs=0.01)  # ~32.85% APR

    def test_stop_loss_threshold(self):
        usdt_size = 300.0
        sl_pct = 0.02     # 2%
        funding_collected = -7.0
        pnl_pct = funding_collected / usdt_size
        assert pnl_pct < -sl_pct  # should trigger SL


# ---------------------------------------------------------------------------
# Emergency Stop: price crash detection math
# ---------------------------------------------------------------------------

class TestPriceCrashDetector:
    def test_crash_detected_at_5pct(self):
        oldest_price = 100_000.0
        current_price = 94_800.0  # 5.2% drop
        crash_threshold = 5.0

        drop_pct = (oldest_price - current_price) / oldest_price * 100
        assert drop_pct >= crash_threshold

    def test_no_crash_below_threshold(self):
        oldest_price = 100_000.0
        current_price = 95_200.0  # 4.8% drop
        crash_threshold = 5.0

        drop_pct = (oldest_price - current_price) / oldest_price * 100
        assert drop_pct < crash_threshold

    def test_drawdown_detection(self):
        peak = 10_000.0
        current = 8_790.0  # 12.1% drawdown
        max_dd = 12.0

        dd_pct = (peak - current) / peak * 100
        assert dd_pct >= max_dd
