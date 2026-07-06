"""
Breakout Momentum Bot — Стратегия 5: пробой консолидации с объёмным подтверждением.

Логика входа (все условия одновременно):
1. BB Squeeze на 1H:  BandWidth < squeeze_threshold (консолидация)
2. Пробой на 15m:     последняя закрытая свеча вышла за max/min последних N 1H-свечей
3. Объём на 15m:      объём пробойной свечи > volume_multiplier × средний объём
4. R:R ≥ rr_min:     соотношение TP/SL рассчитывается через ATR(1H)

Логика выхода (первое из):
- TP:      цена достигла entry ± atr_tp_mult × ATR
- SL:      цена достигла entry ± atr_sl_mult × ATR
- Таймаут: позиция открыта > max_trade_duration_h часов

Математика:
  Win rate 45% × R:R 2.5:1 = ожидаемое значение +0.575 на сделку → стабильный плюс.

Исполнение через Bybit Linear (perpetual) с фиксированным плечом.
Paper mode: симулируем заполнение и выход по цене.
"""

import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
from ta.volatility import BollingerBands
from loguru import logger

from bots.base import BaseBot
from core.exchange import ExchangeAdapter
from core.state import StateStore
from core.emergency_stop import EmergencyStop

QTY_PRECISION = 6
PERP_MIN_QTY: dict[str, float] = {"BTCUSDT": 0.001, "ETHUSDT": 0.01}


@dataclass
class BreakoutTrade:
    symbol:      str
    direction:   str    # "long" or "short"
    entry_price: float
    qty:         float
    tp_price:    float
    sl_price:    float
    entry_time:  float
    pnl_usdt:    float = 0.0


class BreakoutBot(BaseBot):
    TICK_INTERVAL = 60.0  # 1 минута

    def __init__(
        self,
        exchange: ExchangeAdapter,
        config: dict,
        state_store: StateStore,
        emergency_stop: EmergencyStop,
        paper_mode: bool = False,
        notifier=None,
    ):
        super().__init__("breakout", state_store, emergency_stop)
        self._exchange = exchange
        self._cfg      = config
        self._paper    = paper_mode
        self._notify   = notifier

        self._symbols         = config.get("symbols", ["BTCUSDT", "ETHUSDT", "SOLUSDT"])
        self._capital         = config.get("capital_usdt", 500.0)
        self._leverage        = config.get("leverage", 5)
        self._risk_pct        = config.get("risk_per_trade_pct", 1.5) / 100.0
        self._rr_min          = config.get("rr_min", 2.5)
        self._max_concurrent  = config.get("max_concurrent", 3)
        self._volume_mult     = config.get("volume_multiplier", 2.5)
        self._bb_period       = config.get("bb_period", 20)
        self._squeeze_thr     = config.get("squeeze_threshold", 0.05)
        self._atr_sl_mult     = config.get("atr_sl_mult", 1.0)
        self._atr_tp_mult     = config.get("atr_tp_mult", 2.5)
        self._max_duration_h  = config.get("max_trade_duration_h", 4)
        self._cooldown_h      = config.get("cooldown_after_exit_h", 2)
        self._quiet_start     = config.get("quiet_hours_start", 1)
        self._quiet_end       = config.get("quiet_hours_end", 5)

        self._trades: dict[str, BreakoutTrade] = {}
        self._cooldown: dict[str, float] = {}       # symbol → can_enter_after
        self._leverage_set: set[str] = set()
        self._squeeze_cache: dict[str, tuple[bool, float]] = {}  # (is_squeeze, expires)

        # Circuit breaker: не открывать позиции если BTC упал > 3% за последний час
        self._btc_current_price: float = 0.0
        self._btc_price_1h_ago: float = 0.0
        self._btc_snapshot_time: float = 0.0

        self._fee_pct: float = config.get("fee_pct", 0.0020)

        self._total_trades    = 0
        self._win_trades      = 0
        self._total_pnl       = 0.0
        self._total_fees_usdt = 0.0

    # ------------------------------------------------------------------
    # Основной тик
    # ------------------------------------------------------------------

    async def tick(self) -> None:
        if self._is_quiet_hours():
            return

        now = time.time()
        for sym in self._symbols:
            try:
                price = await self._exchange.get_ticker(sym)
                if sym == "BTCUSDT":
                    self._update_btc_snapshot(price, now)
                if sym in self._trades:
                    await self._manage_trade(sym, self._trades[sym], price)
                elif (
                    len(self._trades) < self._max_concurrent
                    and not self._in_cooldown(sym)
                ):
                    await self._check_entry(sym, price)
            except Exception as e:
                logger.warning(f"[breakout] {sym} tick error: {e}")

    # ------------------------------------------------------------------
    # Сигнал входа
    # ------------------------------------------------------------------

    def _update_btc_snapshot(self, price: float, now: float) -> None:
        self._btc_current_price = price
        if self._btc_snapshot_time == 0.0:
            self._btc_price_1h_ago = price
            self._btc_snapshot_time = now
        elif now - self._btc_snapshot_time >= 3600:
            self._btc_price_1h_ago = self._btc_current_price
            self._btc_snapshot_time = now

    def _btc_crashed(self) -> bool:
        """True если BTC упал >3% с момента последнего часового снимка цены."""
        if self._btc_price_1h_ago <= 0 or self._btc_current_price <= 0:
            return False
        drop = (self._btc_price_1h_ago - self._btc_current_price) / self._btc_price_1h_ago
        return drop > 0.03

    async def _check_entry(self, symbol: str, price: float) -> None:
        # Circuit breaker: BTC упал >3% за последний час → не открываем новые позиции
        if self._btc_crashed():
            drop = (self._btc_price_1h_ago - self._btc_current_price) / self._btc_price_1h_ago * 100
            logger.warning(f"[breakout] Circuit breaker: BTC -{drop:.1f}% за час — пропуск {symbol}")
            return

        now = time.time()

        # 1H BB Squeeze (кешируем на 1 час)
        cached = self._squeeze_cache.get(symbol)
        if cached is None or now >= cached[1]:
            klines_1h = await self._exchange.get_klines(symbol, "60", self._bb_period + 15)
            if len(klines_1h) < self._bb_period:
                return
            is_sq = self._is_squeeze(klines_1h)
            self._squeeze_cache[symbol] = (is_sq, now + 3600.0)
            logger.debug(f"[breakout] {symbol}: 1H squeeze cache → {is_sq}")
        else:
            is_sq = cached[0]

        if not is_sq:
            return

        # 15m пробой с объёмом
        klines_15m = await self._exchange.get_klines(symbol, "15", 25)
        if len(klines_15m) < 20:
            return

        last = klines_15m[-2]        # последняя ЗАКРЫТАЯ свеча
        prev = klines_15m[-17:-2]    # 15 свечей для среднего объёма

        avg_vol = sum(c["volume"] for c in prev) / len(prev) if prev else 0
        if avg_vol == 0 or last["volume"] < avg_vol * self._volume_mult:
            logger.debug(
                f"[breakout] {symbol}: объём {last['volume']:.1f} "
                f"< {avg_vol * self._volume_mult:.1f} — нет подтверждения"
            )
            return

        # Диапазон консолидации (max/min последних N 1H-свечей, исключая текущую)
        klines_1h_fresh = await self._exchange.get_klines(symbol, "60", self._bb_period + 5)
        window = klines_1h_fresh[-(self._bb_period + 1):-1]  # exclude last forming
        if not window:
            return
        range_high = max(c["high"]  for c in window)
        range_low  = min(c["low"]   for c in window)

        # Направление пробоя
        if last["close"] > range_high and last["close"] > last["open"]:
            direction = "long"
        elif last["close"] < range_low and last["close"] < last["open"]:
            direction = "short"
        else:
            logger.debug(f"[breakout] {symbol}: нет чёткого пробоя")
            return

        # ATR(14) на 1H для размера TP/SL
        atr = self._calc_atr(klines_1h_fresh, period=14)
        if atr == 0:
            return

        if direction == "long":
            sl_price = price - atr * self._atr_sl_mult
            tp_price = price + atr * self._atr_tp_mult
        else:
            sl_price = price + atr * self._atr_sl_mult
            tp_price = price - atr * self._atr_tp_mult

        sl_distance = abs(price - sl_price)
        tp_distance = abs(price - tp_price)

        # Проверка R:R
        actual_rr = tp_distance / sl_distance if sl_distance > 0 else 0
        if actual_rr < self._rr_min:
            logger.debug(
                f"[breakout] {symbol}: R:R {actual_rr:.2f} < {self._rr_min} — пропуск"
            )
            return

        # Размер позиции: рискуем risk_pct от капитала
        risk_usdt = self._capital * self._risk_pct
        qty = math.floor((risk_usdt / sl_distance) * 10 ** QTY_PRECISION) / 10 ** QTY_PRECISION

        min_qty = PERP_MIN_QTY.get(symbol, 0.001)
        if qty < min_qty:
            logger.warning(
                f"[breakout] {symbol}: qty {qty:.6f} < биржевой min {min_qty}"
            )
            return

        await self._enter(symbol, direction, price, qty, tp_price, sl_price)

    async def _enter(
        self,
        symbol: str,
        direction: str,
        price: float,
        qty: float,
        tp_price: float,
        sl_price: float,
    ) -> None:
        logger.info(
            f"[breakout] ВХОД {direction.upper()} {symbol} @ {price:.2f} "
            f"TP={tp_price:.2f} SL={sl_price:.2f} qty={qty:.6f}"
        )

        if not self._paper:
            # Установить плечо один раз
            if symbol not in self._leverage_set:
                await self._exchange.set_leverage(symbol, self._leverage)
                self._leverage_set.add(symbol)

            side = "Buy" if direction == "long" else "Sell"
            try:
                # stop_loss выставляется на бирже — защита при краше процесса
                await self._exchange.place_perp_market_order(
                    symbol, side, qty, stop_loss=sl_price
                )
            except Exception as e:
                logger.error(f"[breakout] Entry failed {symbol}: {e}")
                return

        self._trades[symbol] = BreakoutTrade(
            symbol=symbol,
            direction=direction,
            entry_price=price,
            qty=qty,
            tp_price=tp_price,
            sl_price=sl_price,
            entry_time=time.time(),
        )

        await self._save_state()

        if self._notify:
            rr = abs(tp_price - price) / abs(sl_price - price)
            await self._notify(
                f"Breakout {'📈 LONG' if direction == 'long' else '📉 SHORT'} [{symbol}]\n"
                f"@ {price:.2f} | TP {tp_price:.2f} | SL {sl_price:.2f}\n"
                f"R:R {rr:.1f}:1 | qty {qty:.6f} | {'paper' if self._paper else 'live'}"
            )

    # ------------------------------------------------------------------
    # Управление открытой сделкой
    # ------------------------------------------------------------------

    async def _manage_trade(
        self, symbol: str, trade: BreakoutTrade, price: float
    ) -> None:
        elapsed_h = (time.time() - trade.entry_time) / 3600

        # TP
        tp_hit = (
            (trade.direction == "long"  and price >= trade.tp_price) or
            (trade.direction == "short" and price <= trade.tp_price)
        )
        if tp_hit:
            await self._close(symbol, trade, trade.tp_price, "TP")
            return

        # SL
        sl_hit = (
            (trade.direction == "long"  and price <= trade.sl_price) or
            (trade.direction == "short" and price >= trade.sl_price)
        )
        if sl_hit:
            await self._close(symbol, trade, price, "SL")
            return

        # Таймаут
        if elapsed_h >= self._max_duration_h:
            await self._close(symbol, trade, price, "TIMEOUT")

    async def _close(
        self,
        symbol: str,
        trade: BreakoutTrade,
        close_price: float,
        reason: str,
    ) -> None:
        if not self._paper:
            close_side = "Sell" if trade.direction == "long" else "Buy"
            try:
                await self._exchange.close_perp_position(symbol, close_side, trade.qty)
            except Exception as e:
                logger.error(f"[breakout] Close {symbol} failed: {e}")
                return

        mult = 1.0 if trade.direction == "long" else -1.0
        gross_pnl = (close_price - trade.entry_price) * trade.qty * mult
        fee_usdt  = self._fee_pct * trade.qty * trade.entry_price
        pnl       = gross_pnl - fee_usdt
        pnl_pct   = pnl / (trade.qty * trade.entry_price) * 100.0
        self._total_fees_usdt += fee_usdt

        del self._trades[symbol]
        self._total_trades += 1
        self._total_pnl    += pnl
        if pnl > 0:
            self._win_trades += 1

        # Кулдаун после выхода
        self._cooldown[symbol] = time.time() + self._cooldown_h * 3600
        await self._save_state()

        logger.info(
            f"[breakout] ЗАКРЫТ {trade.direction.upper()} {symbol} [{reason}] "
            f"@ {close_price:.2f} pnl={pnl:+.4f} USDT ({pnl_pct:+.2f}%)"
        )

        if self._notify:
            icon = "✅" if pnl > 0 else "❌"
            await self._notify(
                f"Breakout {icon} {reason} [{symbol}]\n"
                f"{trade.direction.upper()} {trade.entry_price:.2f} → {close_price:.2f}\n"
                f"PnL: {pnl:+.4f} USDT ({pnl_pct:+.2f}%)\n"
                f"Сделок: {self._total_trades} | Win: {self._win_rate:.0f}% | "
                f"Итого: {self._total_pnl:+.2f}"
            )

    # ------------------------------------------------------------------
    # Индикаторы
    # ------------------------------------------------------------------

    def _is_squeeze(self, klines: list[dict]) -> bool:
        df = pd.DataFrame(klines)
        bb = BollingerBands(
            close=df["close"], window=self._bb_period, window_dev=2.0
        )
        # iloc[-2] = последняя ЗАКРЫТАЯ 1H свеча; iloc[-1] формируется прямо сейчас
        bw = bb.bollinger_wband()
        return float(bw.iloc[-2]) < self._squeeze_thr

    @staticmethod
    def _calc_atr(klines: list[dict], period: int = 14) -> float:
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

    # ------------------------------------------------------------------
    # Вспомогательные
    # ------------------------------------------------------------------

    def _is_quiet_hours(self) -> bool:
        hour = datetime.now(timezone.utc).hour
        if self._quiet_start < self._quiet_end:
            return self._quiet_start <= hour < self._quiet_end
        return hour >= self._quiet_start or hour < self._quiet_end

    def _in_cooldown(self, symbol: str) -> bool:
        return time.time() < self._cooldown.get(symbol, 0)

    @property
    def _win_rate(self) -> float:
        return self._win_trades / self._total_trades * 100.0 if self._total_trades else 0.0

    async def get_sleep_interval(self) -> float:
        return self.TICK_INTERVAL

    # ------------------------------------------------------------------
    # Персистентность (Redis)
    # ------------------------------------------------------------------

    async def _save_state(self) -> None:
        snap = await self.get_state_snapshot()
        if snap:
            await self._state.set_bot_state(self.name, snap)

    async def get_state_snapshot(self) -> Optional[dict]:
        return {
            "trades": {
                sym: {
                    "symbol":      t.symbol,
                    "direction":   t.direction,
                    "entry_price": t.entry_price,
                    "qty":         t.qty,
                    "tp_price":    t.tp_price,
                    "sl_price":    t.sl_price,
                    "entry_time":  t.entry_time,
                    "pnl_usdt":    t.pnl_usdt,
                }
                for sym, t in self._trades.items()
            },
            "cooldown":           dict(self._cooldown.items()),
            "total_trades":       self._total_trades,
            "win_trades":         self._win_trades,
            "total_pnl":          self._total_pnl,
            "total_fees_usdt":    self._total_fees_usdt,
            "btc_price_1h_ago":   self._btc_price_1h_ago,
            "btc_snapshot_time":  self._btc_snapshot_time,
        }

    async def restore_state(self, saved: dict) -> None:
        for sym, d in saved.get("trades", {}).items():
            self._trades[sym] = BreakoutTrade(**d)
        self._cooldown           = saved.get("cooldown", {})
        self._total_trades       = saved.get("total_trades", 0)
        self._win_trades         = saved.get("win_trades", 0)
        self._total_pnl          = saved.get("total_pnl", 0.0)
        self._total_fees_usdt    = saved.get("total_fees_usdt", 0.0)
        self._btc_price_1h_ago   = saved.get("btc_price_1h_ago", 0.0)
        self._btc_snapshot_time  = saved.get("btc_snapshot_time", 0.0)
        logger.info(f"[breakout] Restored {len(self._trades)} trades from Redis.")
        if not self._paper and self._trades:
            await self._reconcile()

    async def _reconcile(self) -> None:
        """Сверка Redis-состояния с реальными позициями на бирже после рестарта."""
        try:
            real_positions = await self._exchange.get_open_positions()
            real_symbols = {p.symbol for p in real_positions}
            stale = [sym for sym in self._trades if sym not in real_symbols]
            for sym in stale:
                logger.warning(
                    f"[breakout] {sym}: позиция в Redis, но не найдена на бирже — очищаем"
                )
                del self._trades[sym]
            if stale:
                await self._save_state()
                logger.info(f"[breakout] Reconcile: удалено {len(stale)} фантомных позиций")
        except Exception as e:
            logger.warning(f"[breakout] Reconcile error: {e}")

    def get_stats(self) -> dict:
        return {
            "active_trades":   len(self._trades),
            "total_trades":    self._total_trades,
            "win_rate_pct":    round(self._win_rate, 1),
            "total_pnl_usdt":  round(self._total_pnl, 4),
            "total_fees_usdt": round(self._total_fees_usdt, 4),
            "open": {
                sym: {
                    "direction": t.direction,
                    "entry":     t.entry_price,
                    "tp":        t.tp_price,
                    "sl":        t.sl_price,
                    "h_ago":     round((time.time() - t.entry_time) / 3600, 1),
                }
                for sym, t in self._trades.items()
            },
        }
