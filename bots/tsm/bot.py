"""
TSM Trend-Following Bot — Donchian 55/20 long/short на дневных барах (спринт 07/2026).

Валидация: 07_SPRINT_VALIDATION.md — PF 2.06, плато Donchian 16-55, corr с Grid
в 2022-окне −0.79 (закрывает медвежью дыру портфеля). Сайзинг R=$5/сделку
зафиксирован портфельным Монте-Карло (08_SPRINT_SPECS.md).

Правила (только ЗАКРЫТЫЕ дневные бары):
- Вход long:  close > max(high 55 предыдущих дней); short — зеркально по min(low).
- Выход:      противоположный 20-дневный экстремум; flip по противоположному входу.
- Стоп:       2×ATR(14,1D) от входа, exchange-level при входе (переживает краш процесса).
- Сайзинг:    qty = risk_usd / (2×ATR), кап capital_usdt/price (1× слота на символ).
"""

import math
import time
from dataclasses import dataclass
from typing import Optional

from loguru import logger

from bots.base import BaseBot
from core.exchange import ExchangeAdapter
from core.state import StateStore
from core.emergency_stop import EmergencyStop

QTY_PRECISION = 6
PERP_MIN_QTY: dict[str, float] = {"BTCUSDT": 0.001, "ETHUSDT": 0.01}


@dataclass
class TsmTrade:
    symbol:      str
    direction:   str    # "long" | "short"
    entry_price: float
    qty:         float
    stop_price:  float
    entry_time:  float
    entry_bar_ts: int   # timestamp дневного бара, давшего сигнал (dedup)


class TsmBot(BaseBot):
    TICK_INTERVAL = 3600.0  # дневная стратегия: часовой тик достаточен

    def __init__(
        self,
        exchange: ExchangeAdapter,
        config: dict,
        state_store: StateStore,
        emergency_stop: EmergencyStop,
        paper_mode: bool = False,
        notifier=None,
    ):
        super().__init__("tsm", state_store, emergency_stop)
        self._exchange = exchange
        self._cfg      = config
        self._paper    = paper_mode
        self._notify   = notifier

        self._symbols        = config.get("symbols", ["BTCUSDT", "ETHUSDT"])
        self._capital        = config.get("capital_usdt", 200.0)
        self._risk_usd       = config.get("risk_per_trade_usd", 5.0)
        self._entry_period   = config.get("entry_period", 55)
        self._exit_period    = config.get("exit_period", 20)
        self._atr_period     = config.get("atr_period", 14)
        self._atr_stop_mult  = config.get("atr_stop_mult", 2.0)
        self._leverage       = config.get("leverage", 1)
        self._fee_pct        = config.get("fee_pct", 0.0011)

        self._trades: dict[str, TsmTrade] = {}
        self._last_entry_bar: dict[str, int] = {}   # symbol → ts бара последнего входа
        self._leverage_set: set[str] = set()

        self._total_trades    = 0
        self._win_trades      = 0
        self._total_pnl       = 0.0
        self._total_fees_usdt = 0.0

    # ------------------------------------------------------------------
    # Основной тик
    # ------------------------------------------------------------------

    async def tick(self) -> None:
        for sym in self._symbols:
            try:
                klines = await self._exchange.get_klines(sym, "D", self._entry_period + 20)
                closed = klines[:-1]  # последний бар формируется — не сигнальный
                if len(closed) < self._entry_period + 2:
                    continue
                price = await self._exchange.get_ticker(sym)
                if sym in self._trades:
                    await self._manage(sym, self._trades[sym], closed, price)
                else:
                    await self._check_entry(sym, closed, price)
            except Exception as e:
                logger.warning(f"[tsm] {sym} tick error: {e}")

    # ------------------------------------------------------------------
    # Сигналы (по закрытым дневным барам)
    # ------------------------------------------------------------------

    def _signals(self, closed: list[dict]) -> dict:
        last = closed[-1]
        win_e = closed[-(self._entry_period + 1):-1]   # 55 баров ДО последнего
        win_x = closed[-(self._exit_period + 1):-1]
        return {
            "bar_ts":    int(last["timestamp"]),
            "long_in":   last["close"] > max(c["high"] for c in win_e),
            "short_in":  last["close"] < min(c["low"] for c in win_e),
            "long_out":  last["close"] < min(c["low"] for c in win_x),
            "short_out": last["close"] > max(c["high"] for c in win_x),
            "atr":       self._calc_atr(closed, self._atr_period),
        }

    @staticmethod
    def _calc_atr(klines: list[dict], period: int) -> float:
        trs = []
        for i in range(1, len(klines)):
            prev_close = klines[i - 1]["close"]
            h, low = klines[i]["high"], klines[i]["low"]
            trs.append(max(h - low, abs(h - prev_close), abs(low - prev_close)))
        if len(trs) < period:
            return sum(trs) / len(trs) if trs else 0.0
        return sum(trs[-period:]) / period

    def _size(self, price: float, atr: float, symbol: str) -> float:
        """Risk-based сайзинг с капом 1× слота (валидирован портфельным MC)."""
        sl_dist = self._atr_stop_mult * atr
        if sl_dist <= 0 or price <= 0:
            return 0.0
        qty = self._risk_usd / sl_dist
        qty = min(qty, self._capital / price)
        qty = math.floor(qty * 10 ** QTY_PRECISION) / 10 ** QTY_PRECISION
        if qty < PERP_MIN_QTY.get(symbol, 0.001):
            return 0.0
        return qty

    # ------------------------------------------------------------------
    # Вход
    # ------------------------------------------------------------------

    async def _check_entry(self, symbol: str, closed: list[dict], price: float) -> None:
        sig = self._signals(closed)
        if not (sig["long_in"] or sig["short_in"]):
            return
        # один вход на один дневной бар (сигнал не «перезваниваем» каждый час)
        if self._last_entry_bar.get(symbol) == sig["bar_ts"]:
            return
        direction = "long" if sig["long_in"] else "short"
        qty = self._size(price, sig["atr"], symbol)
        if qty <= 0:
            logger.warning(f"[tsm] {symbol}: qty ниже минимума — пропуск входа")
            return
        stop = (price - self._atr_stop_mult * sig["atr"] if direction == "long"
                else price + self._atr_stop_mult * sig["atr"])

        if not self._paper:
            if symbol not in self._leverage_set:
                await self._exchange.set_leverage(symbol, self._leverage)
                self._leverage_set.add(symbol)
            side = "Buy" if direction == "long" else "Sell"
            try:
                await self._exchange.place_perp_market_order(
                    symbol, side, qty, stop_loss=round(stop, 4)
                )
            except Exception as e:
                logger.error(f"[tsm] Entry failed {symbol}: {e}")
                return

        self._trades[symbol] = TsmTrade(
            symbol=symbol, direction=direction, entry_price=price, qty=qty,
            stop_price=stop, entry_time=time.time(), entry_bar_ts=sig["bar_ts"],
        )
        self._last_entry_bar[symbol] = sig["bar_ts"]
        await self._save_state()
        logger.info(
            f"[tsm] ВХОД {direction.upper()} {symbol} @ {price:.2f} "
            f"stop={stop:.2f} qty={qty:.6f}"
        )
        if self._notify:
            await self._notify(
                f"TSM {'📈 LONG' if direction == 'long' else '📉 SHORT'} [{symbol}]\n"
                f"@ {price:.2f} | SL {stop:.2f} | qty {qty:.6f}\n"
                f"Donchian {self._entry_period}/{self._exit_period} | "
                f"{'paper' if self._paper else 'live'}"
            )

    # ------------------------------------------------------------------
    # Управление позицией
    # ------------------------------------------------------------------

    async def _manage(self, symbol: str, trade: TsmTrade,
                      closed: list[dict], price: float) -> None:
        sig = self._signals(closed)

        # Paper: стоп симулируем пересечением текущей цены (live закрывает биржа)
        if self._paper:
            stop_hit = (
                (trade.direction == "long" and price <= trade.stop_price) or
                (trade.direction == "short" and price >= trade.stop_price)
            )
            if stop_hit:
                await self._close(symbol, trade, trade.stop_price, "STOP")
                return
        else:
            # Live: если биржа исполнила SL — позиции уже нет; reconcile на тике
            try:
                real = await self._exchange.get_open_positions()
                if symbol not in {p.symbol for p in real}:
                    await self._close(symbol, trade, price, "STOP_EXCHANGE",
                                      already_closed=True)
                    return
            except Exception as e:
                logger.warning(f"[tsm] {symbol} position check error: {e}")

        exit_sig = (
            (trade.direction == "long" and sig["long_out"]) or
            (trade.direction == "short" and sig["short_out"])
        )
        flip_sig = (
            (trade.direction == "long" and sig["short_in"]) or
            (trade.direction == "short" and sig["long_in"])
        )
        if exit_sig or flip_sig:
            await self._close(symbol, trade, price, "FLIP" if flip_sig else "EXIT")
            # flip: вход в противоположную сторону произойдёт на следующем тике
            # (условие входа сохранится, dedup по бару пропустит только тот же бар)

    async def _close(self, symbol: str, trade: TsmTrade, close_price: float,
                     reason: str, already_closed: bool = False) -> None:
        if not self._paper and not already_closed:
            close_side = "Sell" if trade.direction == "long" else "Buy"
            try:
                await self._exchange.close_perp_position(symbol, close_side, trade.qty)
            except Exception as e:
                logger.error(f"[tsm] Close {symbol} failed: {e}")
                return

        mult = 1.0 if trade.direction == "long" else -1.0
        gross = (close_price - trade.entry_price) * trade.qty * mult
        fee = self._fee_pct * trade.qty * trade.entry_price
        pnl = gross - fee
        self._total_fees_usdt += fee

        del self._trades[symbol]
        self._total_trades += 1
        self._total_pnl += pnl
        if pnl > 0:
            self._win_trades += 1
        await self._save_state()

        logger.info(
            f"[tsm] ЗАКРЫТ {trade.direction.upper()} {symbol} [{reason}] "
            f"@ {close_price:.2f} pnl={pnl:+.4f} USDT"
        )
        if self._notify:
            icon = "✅" if pnl > 0 else "❌"
            await self._notify(
                f"TSM {icon} {reason} [{symbol}]\n"
                f"{trade.direction.upper()} {trade.entry_price:.2f} → {close_price:.2f}\n"
                f"PnL: {pnl:+.4f} USDT\n"
                f"Сделок: {self._total_trades} | Итого: {self._total_pnl:+.2f}"
            )

    # ------------------------------------------------------------------
    # Персистентность (Redis) + reconcile
    # ------------------------------------------------------------------

    async def _save_state(self) -> None:
        snap = await self.get_state_snapshot()
        if snap:
            await self._state.set_bot_state(self.name, snap)

    async def get_state_snapshot(self) -> Optional[dict]:
        return {
            "trades": {
                sym: {
                    "symbol": t.symbol, "direction": t.direction,
                    "entry_price": t.entry_price, "qty": t.qty,
                    "stop_price": t.stop_price, "entry_time": t.entry_time,
                    "entry_bar_ts": t.entry_bar_ts,
                }
                for sym, t in self._trades.items()
            },
            "last_entry_bar":  dict(self._last_entry_bar),
            "total_trades":    self._total_trades,
            "win_trades":      self._win_trades,
            "total_pnl":       self._total_pnl,
            "total_fees_usdt": self._total_fees_usdt,
        }

    async def restore_state(self, saved: dict) -> None:
        for sym, d in saved.get("trades", {}).items():
            self._trades[sym] = TsmTrade(**d)
        self._last_entry_bar  = {k: int(v) for k, v in saved.get("last_entry_bar", {}).items()}
        self._total_trades    = saved.get("total_trades", 0)
        self._win_trades      = saved.get("win_trades", 0)
        self._total_pnl       = saved.get("total_pnl", 0.0)
        self._total_fees_usdt = saved.get("total_fees_usdt", 0.0)
        logger.info(f"[tsm] Restored {len(self._trades)} trades from Redis.")
        if not self._paper and self._trades:
            await self._reconcile()

    async def _reconcile(self) -> None:
        """После рестарта: позиции, закрытые биржей (SL) пока процесс лежал."""
        try:
            real = await self._exchange.get_open_positions()
            real_symbols = {p.symbol for p in real}
        except Exception as e:
            logger.warning(f"[tsm] Reconcile error: {e}")
            return
        stale = [sym for sym in self._trades if sym not in real_symbols]
        for sym in stale:
            t = self._trades.pop(sym)
            logger.warning(f"[tsm] {sym}: позиция в Redis, нет на бирже — фантом удалён")
            if self._notify:
                await self._notify(
                    f"⚠️ TSM: позиция {sym} ({t.direction}) закрыта вне бота "
                    f"(вероятно exchange-SL). Состояние очищено."
                )
        if stale:
            await self._save_state()

    async def get_sleep_interval(self) -> float:
        return self.TICK_INTERVAL

    @property
    def _win_rate(self) -> float:
        return self._win_trades / self._total_trades * 100.0 if self._total_trades else 0.0

    def get_stats(self) -> dict:
        return {
            "active_trades":   len(self._trades),
            "total_trades":    self._total_trades,
            "win_rate_pct":    round(self._win_rate, 1),
            "total_pnl_usdt":  round(self._total_pnl, 4),
            "total_fees_usdt": round(self._total_fees_usdt, 4),
            "open": {
                sym: {"direction": t.direction, "entry": t.entry_price,
                      "stop": t.stop_price,
                      "days": round((time.time() - t.entry_time) / 86400, 1)}
                for sym, t in self._trades.items()
            },
        }
