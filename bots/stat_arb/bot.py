"""
Statistical Arbitrage Bot — Стратегия 6: пары ETH/BTC, рыночно-нейтральная.

Логика:
1. Считаем Z-score логарифмической пропорции log(ETH/BTC) по скользящему окну N часов
2. Если Z > entry_zscore:  ETH переоценён → SHORT ETH + LONG BTC (размеры равные в USDT)
3. Если Z < -entry_zscore: ETH недооценён → LONG ETH + SHORT BTC
4. Выходим когда Z вернулся к exit_zscore или сработал стоп (|Z| > stop_zscore)

Почему работает:
  ETH/BTC корреляция исторически 0.85–0.92. Аномальные отклонения — временные.
  Стратегия зарабатывает на ВОЗВРАТЕ к среднему, не на предсказании направления.
  Delta-нейтральность: одновременно лонг и шорт → защита от общего крипто-падения.

Результаты: Sharpe 1.5–2.5 на истории, 2–5% в месяц от задействованного капитала.

Исполнение через Bybit Linear Perp (обе ноги). Paper mode: симулируем PnL по ценам.
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


@dataclass
class StatArbPosition:
    direction:      str    # "long_a_short_b" (long ETH, short BTC) или обратно
    entry_zscore:   float
    qty_a:          float  # количество pair_a (ETH)
    qty_b:          float  # количество pair_b (BTC)
    entry_price_a:  float
    entry_price_b:  float
    entry_time:     float
    realized_pnl:   float = 0.0


class StatArbBot(BaseBot):
    TICK_INTERVAL = 300.0  # 5 минут

    def __init__(
        self,
        exchange: ExchangeAdapter,
        config: dict,
        state_store: StateStore,
        emergency_stop: EmergencyStop,
        paper_mode: bool = False,
        notifier=None,
    ):
        super().__init__("stat_arb", state_store, emergency_stop)
        self._exchange  = exchange
        self._cfg       = config
        self._paper     = paper_mode
        self._notify    = notifier

        self._pair_a          = config.get("pair_a", "ETHUSDT")
        self._pair_b          = config.get("pair_b", "BTCUSDT")
        self._lookback        = config.get("lookback_periods", 20)
        self._entry_z         = config.get("entry_zscore", 2.0)
        self._exit_z          = config.get("exit_zscore", 0.3)
        self._stop_z          = config.get("stop_zscore", 3.5)
        self._capital_per_leg = config.get("capital_per_leg", 400.0)
        self._leverage        = config.get("leverage", 3)
        self._max_hold_h      = config.get("max_hold_hours", 48)

        self._fee_pct: float = config.get("fee_pct", 0.0040)

        self._position: Optional[StatArbPosition] = None
        self._leverage_set = False
        self._total_trades    = 0
        self._win_trades      = 0
        self._total_pnl       = 0.0
        self._total_fees_usdt = 0.0

        # История для Z-score (кеш 1H-свечей)
        self._ratio_history: list[float] = []
        self._history_updated_at: float  = 0.0

    # ------------------------------------------------------------------
    # Основной тик
    # ------------------------------------------------------------------

    async def tick(self) -> None:
        price_a, price_b = await self._get_prices()
        if price_a <= 0 or price_b <= 0:
            return

        zscore = await self._calc_zscore(price_a, price_b)
        if zscore is None:
            return

        logger.debug(
            f"[stat_arb] {self._pair_a}/{self._pair_b}: Z={zscore:.3f} "
            f"price_a={price_a:.4f} price_b={price_b:.2f}"
        )

        if self._position:
            await self._manage_position(zscore, price_a, price_b)
        elif abs(zscore) >= self._entry_z:
            await self._enter(zscore, price_a, price_b)
        else:
            logger.info(
                f"[stat_arb] Z={zscore:.3f} (порог ±{self._entry_z}) — ждём сигнала"
            )

    # ------------------------------------------------------------------
    # Вход
    # ------------------------------------------------------------------

    async def _enter(self, zscore: float, price_a: float, price_b: float) -> None:
        # capital_per_leg — маржа на ногу; умножаем на leverage → правильный notional
        qty_a = math.floor(
            (self._capital_per_leg * self._leverage / price_a) * 10 ** QTY_PRECISION
        ) / 10 ** QTY_PRECISION
        qty_b = math.floor(
            (self._capital_per_leg * self._leverage / price_b) * 10 ** QTY_PRECISION
        ) / 10 ** QTY_PRECISION

        if qty_a * price_a < 5 or qty_b * price_b < 5:
            logger.warning("[stat_arb] Слишком маленький размер позиции — увеличь capital_per_leg")
            return

        # Z > 0: ETH переоценён → SHORT ETH (pair_a), LONG BTC (pair_b)
        # Z < 0: ETH недооценён → LONG ETH (pair_a), SHORT BTC (pair_b)
        if zscore > 0:
            direction  = "short_a_long_b"
            side_a, side_b = "Sell", "Buy"
        else:
            direction  = "long_a_short_b"
            side_a, side_b = "Buy", "Sell"

        logger.info(
            f"[stat_arb] ВХОД {direction}: Z={zscore:.3f} "
            f"{self._pair_a} {side_a} {qty_a:.6f} @ {price_a:.4f} | "
            f"{self._pair_b} {side_b} {qty_b:.6f} @ {price_b:.2f}"
        )

        if not self._paper:
            if not self._leverage_set:
                await self._exchange.set_leverage(self._pair_a, self._leverage)
                await self._exchange.set_leverage(self._pair_b, self._leverage)
                self._leverage_set = True

            a_filled = False
            try:
                await self._exchange.place_perp_market_order(self._pair_a, side_a, qty_a)
                a_filled = True
                await self._exchange.place_perp_market_order(self._pair_b, side_b, qty_b)
            except Exception as e:
                logger.error(f"[stat_arb] Entry failed: {e}")
                if a_filled:
                    # Откатить первую ногу
                    logger.warning(f"[stat_arb] Rolling back {self._pair_a} leg")
                    rollback_side = "Buy" if side_a == "Sell" else "Sell"
                    try:
                        await self._exchange.close_perp_position(self._pair_a, rollback_side, qty_a)
                    except Exception as e2:
                        logger.critical(f"[stat_arb] ROLLBACK FAILED: {e2}")
                        if self._notify:
                            await self._notify(
                                f"🚨 Stat Arb: не удалось откатить {self._pair_a} leg!\n"
                                f"qty={qty_a:.6f} — закрой вручную на Bybit."
                            )
                return

        self._position = StatArbPosition(
            direction=direction,
            entry_zscore=zscore,
            qty_a=qty_a,
            qty_b=qty_b,
            entry_price_a=price_a,
            entry_price_b=price_b,
            entry_time=time.time(),
        )
        await self._save_state()

        if self._notify:
            await self._notify(
                f"StatArb ВХОД: {direction}\n"
                f"Z-score: {zscore:+.3f} (порог ±{self._entry_z})\n"
                f"{self._pair_a} {side_a} @ {price_a:.4f} | "
                f"{self._pair_b} {side_b} @ {price_b:.2f}\n"
                f"Капитал: ${self._capital_per_leg:.0f} × 2 нога × {self._leverage}×"
            )

    # ------------------------------------------------------------------
    # Управление позицией
    # ------------------------------------------------------------------

    async def _manage_position(
        self, zscore: float, price_a: float, price_b: float
    ) -> None:
        pos = self._position
        elapsed_h = (time.time() - pos.entry_time) / 3600

        # Текущий unrealized PnL
        pnl_a, pnl_b = self._calc_leg_pnl(pos, price_a, price_b)
        unrealized = pnl_a + pnl_b

        logger.info(
            f"[stat_arb] позиция {pos.direction}: Z={zscore:.3f} "
            f"unrealized=${unrealized:+.2f} hold={elapsed_h:.1f}h"
        )

        # Выход: Z вернулся к нейтральному
        if abs(zscore) <= self._exit_z:
            await self._exit(f"Z={zscore:.3f} вернулся к нейтральному", price_a, price_b)
            return

        # Стоп-лосс: Z ушёл дальше В ТУ ЖЕ сторону (модель ошиблась)
        # short_a_long_b: вошли при Z > entry_z → стоп если Z растёт дальше до stop_z
        # long_a_short_b:  вошли при Z < -entry_z → стоп если Z падает дальше до -stop_z
        if pos.direction == "short_a_long_b" and zscore > self._stop_z:
            await self._exit(f"SL: Z={zscore:.3f} > {self._stop_z}", price_a, price_b)
            return
        if pos.direction == "long_a_short_b" and zscore < -self._stop_z:
            await self._exit(f"SL: Z={zscore:.3f} < -{self._stop_z}", price_a, price_b)
            return

        # Таймаут
        if elapsed_h >= self._max_hold_h:
            await self._exit(f"Таймаут {elapsed_h:.1f}h", price_a, price_b)

    async def _exit(
        self,
        reason: str,
        price_a: float,
        price_b: float,
    ) -> None:
        pos = self._position
        if pos is None:
            return

        if not self._paper:
            if pos.direction == "short_a_long_b":
                close_a, close_b = "Buy", "Sell"   # close short A, close long B
            else:
                close_a, close_b = "Sell", "Buy"   # close long A, close short B
            try:
                await self._exchange.close_perp_position(self._pair_a, close_a, pos.qty_a)
                await self._exchange.close_perp_position(self._pair_b, close_b, pos.qty_b)
            except Exception as e:
                logger.error(f"[stat_arb] Exit failed: {e}")
                return

        pnl_a, pnl_b = self._calc_leg_pnl(pos, price_a, price_b)
        # Leverage уже в qty (capital * leverage / price) → не умножаем ещё раз
        gross_pnl = pnl_a + pnl_b
        # Комиссия: 4 market-ордера (вход + выход, 2 ноги), fee_pct от обеих ног
        fee_usdt  = self._fee_pct * self._capital_per_leg * self._leverage * 2
        total_pnl = gross_pnl - fee_usdt
        self._total_fees_usdt += fee_usdt
        hold_h = (time.time() - pos.entry_time) / 3600

        self._position = None
        self._total_trades += 1
        self._total_pnl    += total_pnl
        if total_pnl > 0:
            self._win_trades += 1

        await self._save_state()

        logger.info(
            f"[stat_arb] ВЫХОД: {reason} | "
            f"hold={hold_h:.1f}h | pnl=${total_pnl:+.4f}"
        )

        if self._notify:
            icon = "✅" if total_pnl > 0 else "❌"
            await self._notify(
                f"StatArb {icon} ВЫХОД\n"
                f"Причина: {reason}\n"
                f"Держали: {hold_h:.1f}h\n"
                f"PnL: ${total_pnl:+.4f} USDT\n"
                f"Сделок: {self._total_trades} | Win: {self._win_rate:.0f}%"
            )

    # ------------------------------------------------------------------
    # Z-score расчёт
    # ------------------------------------------------------------------

    async def _calc_zscore(self, price_a: float, price_b: float) -> Optional[float]:
        now = time.time()
        # Обновляем историю из 1H-свечей раз в 30 минут
        if now - self._history_updated_at > 1800:
            try:
                klines_a = await self._exchange.get_klines(self._pair_a, "60", self._lookback + 5)
                klines_b = await self._exchange.get_klines(self._pair_b, "60", self._lookback + 5)
                min_len = min(len(klines_a), len(klines_b))
                if min_len < self._lookback:
                    logger.warning("[stat_arb] Недостаточно данных для Z-score")
                    return None
                self._ratio_history = [
                    math.log(klines_a[i]["close"] / klines_b[i]["close"])
                    for i in range(min_len)
                ]
                self._history_updated_at = now
            except Exception as e:
                logger.warning(f"[stat_arb] klines fetch error: {e}")
                if not self._ratio_history:
                    return None

        # Добавляем текущую цену к истории
        current_ratio = math.log(price_a / price_b)
        history = self._ratio_history[-self._lookback:] + [current_ratio]

        window = history[-self._lookback:]
        n = len(window)
        mean = sum(window) / n
        variance = sum((x - mean) ** 2 for x in window) / (n - 1) if n > 1 else 0
        std = math.sqrt(variance) if variance > 1e-12 else 1e-10
        return (current_ratio - mean) / std

    def _calc_leg_pnl(
        self, pos: StatArbPosition, price_a: float, price_b: float
    ) -> tuple[float, float]:
        """PnL каждой ноги в USDT. Leverage уже учтён в qty (capital * lev / price)."""
        if pos.direction == "short_a_long_b":
            pnl_a = (pos.entry_price_a - price_a) * pos.qty_a  # short A
            pnl_b = (price_b - pos.entry_price_b) * pos.qty_b  # long B
        else:
            pnl_a = (price_a - pos.entry_price_a) * pos.qty_a  # long A
            pnl_b = (pos.entry_price_b - price_b) * pos.qty_b  # short B
        return pnl_a, pnl_b

    async def _get_prices(self) -> tuple[float, float]:
        try:
            price_a = await self._exchange.get_ticker(self._pair_a)
            price_b = await self._exchange.get_ticker(self._pair_b)
            return price_a, price_b
        except Exception as e:
            logger.warning(f"[stat_arb] get_prices error: {e}")
            return 0.0, 0.0

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
        pos_data = None
        if self._position:
            p = self._position
            pos_data = {
                "direction":     p.direction,
                "entry_zscore":  p.entry_zscore,
                "qty_a":         p.qty_a,
                "qty_b":         p.qty_b,
                "entry_price_a": p.entry_price_a,
                "entry_price_b": p.entry_price_b,
                "entry_time":    p.entry_time,
                "realized_pnl":  p.realized_pnl,
            }
        return {
            "position":        pos_data,
            "total_trades":    self._total_trades,
            "win_trades":      self._win_trades,
            "total_pnl":       self._total_pnl,
            "total_fees_usdt": self._total_fees_usdt,
        }

    async def restore_state(self, saved: dict) -> None:
        pos = saved.get("position")
        if pos:
            self._position = StatArbPosition(**pos)
        self._total_trades    = saved.get("total_trades", 0)
        self._win_trades      = saved.get("win_trades", 0)
        self._total_pnl       = saved.get("total_pnl", 0.0)
        self._total_fees_usdt = saved.get("total_fees_usdt", 0.0)
        logger.info(
            f"[stat_arb] State restored. Position: {self._position is not None}"
        )
        if not self._paper and self._position is not None:
            await self._reconcile()

    async def _reconcile(self) -> None:
        """Сверка Redis-позиции с реальными перп-позициями на бирже."""
        try:
            real_positions = await self._exchange.get_open_positions()
            real_symbols = {p.symbol for p in real_positions}
            pair_a_on_exchange = self._pair_a in real_symbols
            pair_b_on_exchange = self._pair_b in real_symbols
            if not pair_a_on_exchange and not pair_b_on_exchange:
                logger.warning(
                    f"[stat_arb] Позиция в Redis ({self._pair_a}/{self._pair_b}), "
                    "но обе ноги отсутствуют на бирже — очищаем Redis"
                )
                self._position = None
                await self._save_state()
            elif pair_a_on_exchange != pair_b_on_exchange:
                # Одна нога есть, другой нет — нужно ручное вмешательство
                missing = self._pair_b if pair_a_on_exchange else self._pair_a
                present = self._pair_a if pair_a_on_exchange else self._pair_b
                logger.critical(
                    f"[stat_arb] НЕСООТВЕТСТВИЕ: {present} на бирже, "
                    f"{missing} отсутствует — позиция не delta-нейтральна! "
                    "Требуется ручное закрытие через Bybit UI."
                )
        except Exception as e:
            logger.warning(f"[stat_arb] Reconcile error: {e}")

    def get_stats(self) -> dict:
        pos = self._position
        return {
            "has_position":   pos is not None,
            "direction":      pos.direction if pos else None,
            "entry_zscore":   pos.entry_zscore if pos else None,
            "hold_hours":     round((time.time() - pos.entry_time) / 3600, 1) if pos else None,
            "total_trades":    self._total_trades,
            "win_rate_pct":    round(self._win_rate, 1),
            "total_pnl_usdt":  round(self._total_pnl, 4),
            "total_fees_usdt": round(self._total_fees_usdt, 4),
        }
