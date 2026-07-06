"""
Scalper Bot — Стратегия 4: BB Squeeze + VWAP mean-reversion.

Логика входа (все условия одновременно):
1. BB Squeeze на 4H: ширина полос < squeeze_threshold (сжатие = накопление)
2. Цена ниже VWAP на 1m на vwap_deviation_pct% (локальная перепроданность)
3. RSI(7) на 1m < rsi_oversold (подтверждение)

Логика выхода (первое из):
- TP:      цена >= entry * (1 + take_profit_pct)
- SL:      цена <= entry * (1 - stop_loss_pct)
- Таймаут: позиция открыта > max_trade_duration_min минут

Тип ордеров: PostOnly Limit (maker rebate -0.01% на Bybit).
Нерабочее время: quiet_hours_start — quiet_hours_end UTC (низкий объём).

Paper mode: симулируем заполнение по пересечению цены.
Live mode:  limit buy → ждём fill → limit sell TP + мониторим SL.
"""

import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
from ta.volatility import BollingerBands
from ta.momentum import RSIIndicator
from loguru import logger

from bots.base import BaseBot
from core.exchange import ExchangeAdapter
from core.state import StateStore
from core.emergency_stop import EmergencyStop

QTY_PRECISION = 6
BTC_MIN_QTY   = 0.000048


@dataclass
class ScalpTrade:
    symbol:          str
    entry_price:     float
    qty:             float
    usdt_size:       float
    entry_time:      float
    tp_price:        float
    sl_price:        float
    entry_filled:    bool  = False
    entry_order_id:  str   = ""
    tp_order_id:     str   = ""
    sl_order_id:     str   = ""   # exchange-level stop order id
    pnl_usdt:        float = 0.0


class ScalperBot(BaseBot):
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
        super().__init__("scalper", state_store, emergency_stop)
        self._exchange = exchange
        self._cfg      = config
        self._paper    = paper_mode
        self._notify   = notifier

        self._symbols         = config.get("symbols", ["BTCUSDT"])
        self._capital         = config.get("capital_usdt", 200.0)
        self._bb_period       = config.get("bb_period", 20)
        self._bb_std          = config.get("bb_std", 2.0)
        self._squeeze_thr     = config.get("squeeze_threshold", 0.05)
        self._vwap_dev        = config.get("vwap_deviation_pct", 0.5) / 100.0
        self._rsi_period      = config.get("rsi_period", 7)
        self._rsi_oversold    = config.get("rsi_oversold", 35.0)
        self._tp_pct          = config.get("take_profit_pct", 0.40) / 100.0
        self._sl_pct          = config.get("stop_loss_pct", 0.20) / 100.0
        self._max_duration    = config.get("max_trade_duration_min", 15) * 60
        self._quiet_start     = config.get("quiet_hours_start", 1)
        self._quiet_end       = config.get("quiet_hours_end", 5)

        self._fee_pct = config.get("fee_pct", 0.0009)

        self._trades: dict[str, ScalpTrade] = {}
        self._total_trades    = 0
        self._winning_trades  = 0
        self._total_pnl       = 0.0
        self._total_fees_usdt = 0.0

        # Кеш BB-squeeze на 4H: symbol → (is_squeeze, expires_at)
        # Обновляется раз в 4 часа, а не каждую минуту.
        self._squeeze_cache: dict[str, tuple[bool, float]] = {}
        self._squeeze_ttl = 4 * 3600.0

        # Опциональный callback для репортинга PnL в PortfolioManager
        self._pnl_reporter = None

    def set_pnl_reporter(self, callback) -> None:
        """Подключить PortfolioManager.report_scalper_pnl для реинвестирования."""
        self._pnl_reporter = callback

    # ------------------------------------------------------------------
    # Основной тик
    # ------------------------------------------------------------------

    async def tick(self) -> None:
        if self._is_quiet_hours():
            return

        for sym in self._symbols:
            try:
                price = await self._exchange.get_ticker(sym)
                if sym in self._trades:
                    await self._manage_trade(sym, self._trades[sym], price)
                else:
                    await self._check_entry(sym, price)
            except Exception as e:
                logger.warning(f"[scalper] {sym} tick error: {e}")

    def _is_quiet_hours(self) -> bool:
        hour = datetime.now(timezone.utc).hour
        if self._quiet_start < self._quiet_end:
            return self._quiet_start <= hour < self._quiet_end
        return hour >= self._quiet_start or hour < self._quiet_end

    # ------------------------------------------------------------------
    # Сигнал входа
    # ------------------------------------------------------------------

    async def _check_entry(self, symbol: str, price: float) -> None:
        # 4H BB squeeze — читаем из кеша, обновляем только раз в 4 часа
        now = time.time()
        cached = self._squeeze_cache.get(symbol)
        if cached is None or now >= cached[1]:
            klines_4h = await self._exchange.get_klines(symbol, "240", self._bb_period + 10)
            if len(klines_4h) < self._bb_period:
                return
            is_sq = self._is_squeeze(klines_4h)
            self._squeeze_cache[symbol] = (is_sq, now + self._squeeze_ttl)
            logger.debug(f"[scalper] {symbol}: 4H BB squeeze cache updated → {is_sq}")
        else:
            is_sq = cached[0]

        if not is_sq:
            logger.debug(f"[scalper] {symbol}: нет BB сжатия (cached)")
            return

        # 1m свечи → VWAP + RSI
        klines_1m = await self._exchange.get_klines(symbol, "1", 60)
        if len(klines_1m) < self._rsi_period + 5:
            return

        vwap = self._calc_vwap(klines_1m)
        rsi  = self._calc_rsi(klines_1m)

        entry_signal = (
            price < vwap * (1.0 - self._vwap_dev) and
            rsi < self._rsi_oversold
        )

        logger.debug(
            f"[scalper] {symbol}: price={price:.2f} vwap={vwap:.2f} "
            f"rsi={rsi:.1f} signal={entry_signal}"
        )

        if entry_signal:
            await self._enter(symbol, price)

    async def _enter(self, symbol: str, price: float) -> None:
        qty = math.floor(
            (self._capital / price) * 10 ** QTY_PRECISION
        ) / 10 ** QTY_PRECISION

        if qty < BTC_MIN_QTY:
            logger.warning(
                f"[scalper] {symbol}: qty {qty:.6f} < min. "
                f"Нужно >= ${math.ceil(BTC_MIN_QTY * price)}"
            )
            return

        tp_price = round(price * (1.0 + self._tp_pct), 2)
        sl_price = round(price * (1.0 - self._sl_pct), 2)

        logger.info(
            f"[scalper] ВХОД {symbol} @ {price:.2f} "
            f"TP {tp_price:.2f} SL {sl_price:.2f} qty={qty:.6f}"
        )

        entry_id = ""
        if not self._paper:
            try:
                order = await self._exchange.place_limit_order(
                    symbol, "Buy", qty, price
                )
                entry_id = order.order_id
            except Exception as e:
                logger.error(f"[scalper] Entry failed {symbol}: {e}")
                return

        trade = ScalpTrade(
            symbol=symbol,
            entry_price=price,
            qty=qty,
            usdt_size=self._capital,
            entry_time=time.time(),
            tp_price=tp_price,
            sl_price=sl_price,
            entry_filled=self._paper,   # paper: fill сразу
            entry_order_id=entry_id,
        )
        self._trades[symbol] = trade
        await self._save_state()

        if self._notify:
            await self._notify(
                f"Scalper ВХОД [{symbol}]\n"
                f"@ {price:.2f} | TP {tp_price:.2f} | SL {sl_price:.2f}\n"
                f"${self._capital:.0f} | {qty:.6f} BTC"
            )

    # ------------------------------------------------------------------
    # Управление открытой позицией
    # ------------------------------------------------------------------

    async def _manage_trade(
        self, symbol: str, trade: ScalpTrade, price: float
    ) -> None:
        elapsed = time.time() - trade.entry_time

        # Live: ждать fill entry ордера
        if not self._paper and not trade.entry_filled:
            open_ids = {o.order_id for o in await self._exchange.get_open_orders(symbol)}
            if trade.entry_order_id not in open_ids:
                # Ордер пропал из списка = исполнен
                trade.entry_filled = True
                logger.info(f"[scalper] {symbol}: entry filled @ {trade.entry_price:.2f}")
                try:
                    tp_order = await self._exchange.place_limit_order(
                        symbol, "Sell", trade.qty, trade.tp_price
                    )
                    trade.tp_order_id = tp_order.order_id
                except Exception as e:
                    logger.error(f"[scalper] TP order failed {symbol}: {e}")
                # Exchange-level SL: защита при краше процесса
                try:
                    sl_id = await self._exchange.place_spot_stop_order(
                        symbol, "Sell", trade.qty, trade.sl_price
                    )
                    trade.sl_order_id = sl_id
                    logger.info(
                        f"[scalper] {symbol}: exchange SL placed @ {trade.sl_price:.2f}"
                    )
                except Exception as e:
                    logger.warning(f"[scalper] exchange SL order failed {symbol}: {e}")
                await self._save_state()
            elif elapsed > self._max_duration:
                # Entry так и не заполнился — отменяем
                await self._cancel_entry(symbol, trade)
            return

        if not trade.entry_filled:
            return

        # Live: проверить fill TP ордера
        if not self._paper and trade.tp_order_id:
            open_ids = {o.order_id for o in await self._exchange.get_open_orders(symbol)}
            if trade.tp_order_id not in open_ids:
                await self._close(symbol, trade, trade.tp_price, "TP")
                return

        # Paper: проверить пересечение TP
        if self._paper and price >= trade.tp_price:
            await self._close(symbol, trade, trade.tp_price, "TP")
            return

        # Проверить SL
        if price <= trade.sl_price:
            await self._close(symbol, trade, price, "SL")
            return

        # Таймаут
        if elapsed >= self._max_duration:
            await self._close(symbol, trade, price, "TIMEOUT")

    async def _cancel_entry(self, symbol: str, trade: ScalpTrade) -> None:
        try:
            await self._exchange.cancel_all_orders(symbol)
        except Exception:
            pass
        del self._trades[symbol]
        await self._save_state()
        logger.info(f"[scalper] {symbol}: entry отменён (не заполнился)")

    async def _close(
        self, symbol: str, trade: ScalpTrade, close_price: float, reason: str
    ) -> None:
        gross_pnl = (close_price - trade.entry_price) * trade.qty
        fee_usdt  = self._fee_pct * trade.qty * trade.entry_price
        pnl       = gross_pnl - fee_usdt
        self._total_fees_usdt += fee_usdt
        pnl_pct   = pnl / trade.usdt_size * 100.0

        if not self._paper and reason in ("SL", "TIMEOUT"):
            try:
                if trade.tp_order_id:
                    await self._exchange.cancel_all_orders(symbol)
                await self._exchange.place_market_order(symbol, "Sell", trade.qty)
            except Exception as e:
                logger.error(f"[scalper] Close {symbol} failed: {e}")
                return

        del self._trades[symbol]
        self._total_trades  += 1
        self._total_pnl     += pnl
        if pnl > 0:
            self._winning_trades += 1

        await self._save_state()

        if self._pnl_reporter is not None:
            self._pnl_reporter(pnl)

        logger.info(
            f"[scalper] ЗАКРЫТ {symbol} [{reason}] "
            f"@ {close_price:.2f} pnl={pnl:+.4f} USDT ({pnl_pct:+.2f}%)"
        )

        if self._notify:
            icon = "✅" if pnl > 0 else "❌"
            await self._notify(
                f"Scalper {icon} {reason} [{symbol}]\n"
                f"PnL: {pnl:+.4f} USDT ({pnl_pct:+.2f}%)\n"
                f"Сделок: {self._total_trades} | "
                f"Win: {self._win_rate:.0f}% | "
                f"Итого: {self._total_pnl:+.2f} USDT"
            )

    # ------------------------------------------------------------------
    # Индикаторы
    # ------------------------------------------------------------------

    def _is_squeeze(self, klines: list[dict]) -> bool:
        df = pd.DataFrame(klines)
        bb = BollingerBands(
            close=df["close"], window=self._bb_period, window_dev=self._bb_std
        )
        # bollinger_wband = (upper - lower) / middle (доля, не проценты)
        # iloc[-2] = последняя ЗАКРЫТАЯ свеча (iloc[-1] = текущая формирующаяся)
        bw = bb.bollinger_wband()
        return float(bw.iloc[-2]) < self._squeeze_thr

    @staticmethod
    def _calc_vwap(klines: list[dict]) -> float:
        df = pd.DataFrame(klines)
        typical = (df["high"] + df["low"] + df["close"]) / 3.0
        vol_sum = df["volume"].sum()
        if vol_sum == 0:
            return float(df["close"].iloc[-1])
        return float((typical * df["volume"]).sum() / vol_sum)

    def _calc_rsi(self, klines: list[dict]) -> float:
        df = pd.DataFrame(klines)
        rsi = RSIIndicator(close=df["close"], window=self._rsi_period)
        # iloc[-2] = последняя ЗАКРЫТАЯ 1m свеча; iloc[-1] формируется прямо сейчас
        return float(rsi.rsi().iloc[-2])

    # ------------------------------------------------------------------
    # Вспомогательное
    # ------------------------------------------------------------------

    @property
    def _win_rate(self) -> float:
        if self._total_trades == 0:
            return 0.0
        return self._winning_trades / self._total_trades * 100.0

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
                    "symbol":         t.symbol,
                    "entry_price":    t.entry_price,
                    "qty":            t.qty,
                    "usdt_size":      t.usdt_size,
                    "entry_time":     t.entry_time,
                    "tp_price":       t.tp_price,
                    "sl_price":       t.sl_price,
                    "entry_filled":   t.entry_filled,
                    "entry_order_id": t.entry_order_id,
                    "tp_order_id":    t.tp_order_id,
                    "sl_order_id":    t.sl_order_id,
                    "pnl_usdt":       t.pnl_usdt,
                }
                for sym, t in self._trades.items()
            },
            "total_trades":    self._total_trades,
            "winning_trades":  self._winning_trades,
            "total_pnl":       self._total_pnl,
            "total_fees_usdt": self._total_fees_usdt,
        }

    async def restore_state(self, saved: dict) -> None:
        for sym, d in saved.get("trades", {}).items():
            self._trades[sym] = ScalpTrade(**d)
        self._total_trades    = saved.get("total_trades", 0)
        self._winning_trades  = saved.get("winning_trades", 0)
        self._total_pnl       = saved.get("total_pnl", 0.0)
        self._total_fees_usdt = saved.get("total_fees_usdt", 0.0)
        logger.info(f"[scalper] Restored {len(self._trades)} trades from Redis.")
        if not self._paper and self._trades:
            await self._reconcile()

    async def _reconcile(self) -> None:
        """Сверка Redis-состояния с реальными open orders на бирже после рестарта."""
        try:
            stale: list[str] = []
            for sym, trade in self._trades.items():
                if not trade.entry_filled:
                    # entry ордер ещё не исполнился — проверяем что он на бирже
                    open_ids = {o.order_id for o in await self._exchange.get_open_orders(sym)}
                    if trade.entry_order_id and trade.entry_order_id not in open_ids:
                        logger.warning(
                            f"[scalper] {sym}: entry order {trade.entry_order_id} "
                            "не найден на бирже после рестарта — удаляем из Redis"
                        )
                        stale.append(sym)
                else:
                    # Позиция открыта — проверяем наличие BTC на балансе
                    balance = await self._exchange.get_balance(sym.replace("USDT", ""))
                    if balance.available < trade.qty * 0.5:
                        logger.warning(
                            f"[scalper] {sym}: баланс {balance.available:.6f} "
                            f"не соответствует позиции qty={trade.qty:.6f} — очищаем Redis"
                        )
                        stale.append(sym)
            for sym in stale:
                del self._trades[sym]
            if stale:
                await self._save_state()
                logger.info(f"[scalper] Reconcile: удалено {len(stale)} фантомных позиций")
        except Exception as e:
            logger.warning(f"[scalper] Reconcile error: {e}")

    def get_stats(self) -> dict:
        return {
            "active_trades":   len(self._trades),
            "total_trades":    self._total_trades,
            "win_rate_pct":    round(self._win_rate, 1),
            "total_pnl_usdt":  round(self._total_pnl, 4),
            "total_fees_usdt": round(self._total_fees_usdt, 4),
            "open": {
                sym: {
                    "entry":   t.entry_price,
                    "tp":      t.tp_price,
                    "sl":      t.sl_price,
                    "min_ago": round((time.time() - t.entry_time) / 60, 1),
                }
                for sym, t in self._trades.items()
            },
        }
