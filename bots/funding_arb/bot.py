"""
Funding Arb Bot — Стратегия 2: Delta-Neutral сбор финансирования.

Логика:
1. Каждые 5 минут сканируем список символов на ставку финансирования
2. При rate > entry_threshold:
   - Покупаем spot X BTC  (long hedge)
   - Шортим  perp X BTC   (collect funding)
   → delta = 0, направленного риска нет
3. Каждые 8 часов Bybit начисляет funding payment шортеру → наша прибыль
4. Выходим когда:
   - Rate упал ниже exit_threshold (невыгодно держать)
   - Rate стал отрицательным 2 периода подряд (сами бы платили)
   - ADL-ранк шорта ≥ порога (риск принудительного закрытия)
   - Unrealized PnL < -stop_loss_pct (что-то пошло не так)

Paper mode: симулируем funding payments по реальным ставкам с биржи.
Live mode:  реальные ордера spot + linear на Bybit.

Bybit расчёт финансирования: 00:00, 08:00, 16:00 UTC каждый день.
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

# Дефолтный интервал расчёта funding: 8h. Bybit назначает волатильным парам
# 1h/2h/4h (пример: ONDO/STRK/TAC = 4h, замер 06.07.2026) — фактический интервал
# берётся из get_instrument_meta и хранится в позиции (12_CARRY_WATCHLIST_SPEC).
FUNDING_INTERVAL_SEC = 8 * 3600
# Легаси-дефолты для restore старых снапшотов (BTC-эпоха watchlist'а)
BYBIT_BTC_MIN_QTY   = 0.000048
BTC_QTY_STEP         = 0.000001


@dataclass
class ArbPosition:
    symbol:              str
    spot_qty:            float   # BTC в spot-лонге
    usdt_size:           float   # USDT вложено (одна сторона)
    spot_entry_price:    float
    perp_entry_price:    float
    entry_rate:          float   # funding rate при входе
    entry_time:          float   # unix timestamp
    last_funding_check:  float   # когда последний раз считали funding
    funding_collected:   float = 0.0
    negative_rate_count: int   = 0
    spot_order_id:       str   = ""
    perp_order_id:       str   = ""
    perp_sl_price:       float = 0.0  # exchange-level SL на перп-шорте
    # per-symbol метаданные (дефолты = BTC-легаси для restore старых снапшотов)
    funding_interval_sec: float = FUNDING_INTERVAL_SEC
    qty_step:            float = BTC_QTY_STEP
    min_qty:             float = BYBIT_BTC_MIN_QTY


class FundingArbBot(BaseBot):
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
        super().__init__("funding_arb", state_store, emergency_stop)
        self._exchange = exchange
        self._cfg      = config
        self._paper    = paper_mode
        self._notify   = notifier  # async callable(msg) или None

        self._symbols: list[str]          = config.get("symbols", ["BTCUSDT"])
        self._capital_per_pos: float      = config.get("capital_usdt", 300.0)
        self._max_positions: int          = config.get("max_positions", 2)
        self._entry_rate: float           = config.get("entry_rate_threshold", 0.0003)
        self._exit_rate: float            = config.get("exit_rate_threshold", 0.0001)
        self._neg_exit_periods: int       = config.get("negative_rate_exit_periods", 2)
        self._stop_loss_pct: float        = config.get("stop_loss_pct", 2.0) / 100.0
        self._adl_reduce_rank: int        = config.get("adl_reduce_threshold", 4)
        self._adl_close_rank: int         = config.get("adl_close_threshold", 5)
        self._fee_pct: float              = config.get("fee_pct", 0.0040)

        self._positions: dict[str, ArbPosition] = {}
        self._meta: dict[str, dict]             = {}   # кэш метаданных инструментов
        self._total_funding_earned: float       = 0.0
        self._total_fees_usdt: float            = 0.0
        self._total_closed_positions: int       = 0

    # ------------------------------------------------------------------
    # Основной тик
    # ------------------------------------------------------------------

    async def tick(self) -> None:
        rates = await self._exchange.get_funding_rates(self._symbols)
        now   = time.time()

        # 1. Обработать активные позиции
        for sym in list(self._positions.keys()):
            pos   = self._positions[sym]
            rate  = rates.get(sym, 0.0)
            price = await self._exchange.get_ticker(sym)
            await self._update_position(sym, pos, rate, price, now)

        # 2. Проверить новые входы (пороги — в 8h-эквиваленте ставки:
        #    у 4h-пар сырая ставка вдвое меньше при той же годовой доходности)
        open_slots = self._max_positions - len(self._positions)
        if open_slots > 0:
            for sym in self._symbols:
                if sym in self._positions:
                    continue
                rate = rates.get(sym, 0.0)
                if rate <= 0:
                    continue
                meta = await self._get_meta(sym)
                if not meta:
                    continue
                r8 = rate * (FUNDING_INTERVAL_SEC / meta["funding_interval_sec"])
                if r8 >= self._entry_rate:
                    price = await self._exchange.get_ticker(sym)
                    await self._enter(sym, rate, price, now)
                    open_slots -= 1
                    if open_slots <= 0:
                        break

        # 3. Логируем статус раз в тик
        self._log_status(rates)

    # ------------------------------------------------------------------
    # Вход в позицию
    # ------------------------------------------------------------------

    async def _enter(self, symbol: str, rate: float, price: float, now: float) -> None:
        meta = await self._get_meta(symbol)
        if not meta:
            logger.warning(
                f"[funding_arb] {symbol}: метаданные инструмента недоступны — "
                "вход пропущен (не сайзим вслепую)"
            )
            return

        # Обе ноги торгуют одинаковый qty → шаг = более грубый из spot/perp
        step = max(meta["spot_qty_step"], meta["perp_qty_step"])
        min_qty = max(meta["spot_min_qty"], meta["perp_min_qty"])
        qty = self._floor_to_step(self._capital_per_pos / price, step)

        if qty < min_qty or qty <= 0:
            logger.warning(
                f"[funding_arb] {symbol}: qty={qty} < min={min_qty}. "
                f"Увеличь capital_usdt (нужно >= ${min_qty * price:.0f})"
            )
            return

        logger.info(
            f"[funding_arb] ENTER {symbol}: rate={rate*100:.4f}% "
            f"qty={qty} size=${self._capital_per_pos:.0f} "
            f"interval={meta['funding_interval_sec']/3600:.0f}h"
        )

        # Exchange-level SL на перп-шорте: защита при API-зависании 30–60 мин.
        # Если цена идёт против нас (вверх) на 5% — биржа закрывает шорт автоматически.
        # Цена — вниз к сетке tickSize (чуть жёстче = безопаснее, и не будет reject).
        perp_sl_price = self._floor_to_step(price * 1.05, meta["perp_tick_size"])

        spot_id = perp_id = ""
        spot_filled = False
        try:
            # Long spot
            spot_order = await self._exchange.place_market_order(symbol, "Buy", qty)
            spot_id = spot_order.order_id
            spot_filled = True
            # Short perp + exchange-level SL
            perp_order = await self._exchange.place_perp_market_order(
                symbol, "Sell", qty, stop_loss=perp_sl_price
            )
            perp_id = perp_order.order_id
            logger.info(
                f"[funding_arb] {symbol}: exchange SL на перп-шорте @ {perp_sl_price:.4f} "
                f"(+5% от {price:.4f})"
            )
        except Exception as e:
            logger.error(f"[funding_arb] Failed to enter {symbol}: {e}")
            if spot_filled:
                # Откатить спот-лонг — иначе остаётся непокрытая позиция
                logger.warning(f"[funding_arb] Rolling back spot position for {symbol}")
                try:
                    await self._exchange.place_market_order(symbol, "Sell", qty)
                    logger.info(f"[funding_arb] Spot rollback OK for {symbol}")
                except Exception as e2:
                    logger.critical(
                        f"[funding_arb] ROLLBACK FAILED for {symbol}: {e2}. "
                        "Manual close required!"
                    )
                    if self._notify:
                        await self._notify(
                            f"🚨 КРИТИЧНО: не удалось откатить спот {symbol} qty={qty}!\n"
                            f"Закрой вручную на Bybit."
                        )
            return

        pos = ArbPosition(
            symbol=symbol,
            spot_qty=qty,
            usdt_size=self._capital_per_pos,
            spot_entry_price=price,
            perp_entry_price=price,
            entry_rate=rate,
            entry_time=now,
            last_funding_check=now,
            spot_order_id=spot_id,
            perp_order_id=perp_id,
            perp_sl_price=perp_sl_price,
            funding_interval_sec=meta["funding_interval_sec"],
            qty_step=step,
            min_qty=min_qty,
        )
        self._positions[symbol] = pos
        await self._save_state()

        if self._notify:
            periods_per_day = 86400.0 / meta["funding_interval_sec"]
            apr = rate * periods_per_day * 365 * 100
            await self._notify(
                f"Funding Arb ВХОД: {symbol}\n"
                f"Rate: {rate*100:.4f}% per {meta['funding_interval_sec']/3600:.0f}h "
                f"(~{apr:.1f}% APR)\n"
                f"Size: ${self._capital_per_pos:.0f} | Qty: {qty}"
            )

    # ------------------------------------------------------------------
    # Обновление и выход из позиции
    # ------------------------------------------------------------------

    async def _update_position(
        self, symbol: str, pos: ArbPosition, rate: float, price: float, now: float
    ) -> None:
        # Начислить funding за прошедшие периоды (paper + live stats)
        periods = self._count_funding_periods(
            pos.last_funding_check, now, pos.funding_interval_sec
        )
        if periods > 0:
            earned = periods * rate * pos.usdt_size
            pos.funding_collected  += earned
            self._total_funding_earned += earned
            pos.last_funding_check = now
            logger.info(
                f"[funding_arb] {symbol}: +{earned:.4f} USDT funding "
                f"({periods} period(s) @ {rate*100:.4f}%). "
                f"Total: {pos.funding_collected:.4f} USDT"
            )
            await self._save_state()

        # Обновить счётчик отрицательного rate
        if rate < 0:
            pos.negative_rate_count += 1
        else:
            pos.negative_rate_count = 0

        # Unrealized PnL в paper mode ≈ только funding (delta-нейтральна)
        unrealized_pnl_pct = pos.funding_collected / pos.usdt_size

        # Проверить условия выхода (порог — в 8h-эквиваленте)
        r8 = rate * (FUNDING_INTERVAL_SEC / pos.funding_interval_sec)
        reason = None
        if rate < 0 and pos.negative_rate_count >= self._neg_exit_periods:
            reason = f"ставка отрицательна {pos.negative_rate_count} периода подряд"
        elif 0 <= r8 < self._exit_rate:
            reason = (
                f"rate {r8*100:.4f}%/8h-экв < порога {self._exit_rate*100:.4f}%"
            )
        elif unrealized_pnl_pct < -self._stop_loss_pct and not self._paper:
            reason = f"stop loss: PnL {unrealized_pnl_pct*100:.2f}%"

        if not self._paper:
            adl = await self._check_adl(symbol, pos)
            if adl:
                reason = adl

        if reason:
            await self._exit(symbol, pos, reason)

    async def _check_adl(self, symbol: str, pos: ArbPosition) -> Optional[str]:
        try:
            rank = await self._exchange.get_adl_rank(symbol)
        except Exception:
            return None

        if rank >= self._adl_close_rank:
            return f"ADL ранк {rank} ≥ {self._adl_close_rank} (принудительное закрытие)"

        if rank >= self._adl_reduce_rank:
            # Снизить позицию на 50% (шаг лота — из позиции)
            reduce_qty = self._floor_to_step(pos.spot_qty * 0.5, pos.qty_step)
            if reduce_qty >= pos.min_qty:
                logger.warning(f"[funding_arb] {symbol}: ADL ранк {rank}, снижаем позицию -50%")
                try:
                    await self._exchange.place_market_order(symbol, "Sell", reduce_qty)
                    await self._exchange.close_perp_position(symbol, "Buy", reduce_qty)
                    pos.spot_qty   -= reduce_qty
                    pos.usdt_size  *= 0.5
                    await self._save_state()
                except Exception as e:
                    logger.error(f"[funding_arb] ADL reduce failed: {e}")
        return None

    async def _exit(self, symbol: str, pos: ArbPosition, reason: str) -> None:
        hold_h = (time.time() - pos.entry_time) / 3600
        logger.info(
            f"[funding_arb] EXIT {symbol}: {reason}. "
            f"Держали {hold_h:.1f}ч, собрали {pos.funding_collected:.4f} USDT"
        )

        try:
            # Закрыть spot long
            await self._exchange.place_market_order(symbol, "Sell", pos.spot_qty)
            # Закрыть perp short — reduceOnly гарантирует что не откроем новую позицию
            await self._exchange.close_perp_position(symbol, "Buy", pos.spot_qty)
        except Exception as e:
            logger.error(f"[funding_arb] Failed to exit {symbol}: {e}")
            return

        fee_usdt = self._fee_pct * pos.usdt_size
        net_pnl  = pos.funding_collected - fee_usdt
        self._total_fees_usdt += fee_usdt

        del self._positions[symbol]
        self._total_closed_positions += 1
        await self._save_state()

        if self._notify:
            await self._notify(
                f"Funding Arb ВЫХОД: {symbol}\n"
                f"Причина: {reason}\n"
                f"Держали: {hold_h:.1f}ч\n"
                f"Funding gross: {pos.funding_collected:.4f} USDT\n"
                f"Комиссия: -{fee_usdt:.4f} USDT\n"
                f"Net PnL: {net_pnl:+.4f} USDT"
            )

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------

    @staticmethod
    def _floor_to_step(value: float, step: float) -> float:
        """Округление вниз к сетке шага (лот/тик) с нормализацией FP-хвоста."""
        if step <= 0:
            return value
        floored = math.floor(value / step + 1e-9) * step
        return float(f"{floored:.10f}".rstrip("0").rstrip(".") or "0")

    async def _get_meta(self, symbol: str) -> dict:
        """Метаданные инструмента с кэшем; {} при ошибке API (вход пропускается)."""
        if symbol not in self._meta:
            try:
                self._meta[symbol] = await self._exchange.get_instrument_meta(symbol)
            except Exception as e:
                logger.warning(f"[funding_arb] meta {symbol}: {e}")
                return {}
        return self._meta[symbol]

    @staticmethod
    def _count_funding_periods(
        since: float, until: float, interval_sec: float = FUNDING_INTERVAL_SEC
    ) -> int:
        """Сколько funding-расчётов Bybit прошло между двумя timestamp'ами.

        Интервал зависит от символа (8h у мажоров, 1–4h у волатильных пар).
        """
        count = 0
        t = since
        while True:
            next_s = math.ceil(t / interval_sec + 1e-9) * interval_sec
            if next_s <= until:
                count += 1
                t = next_s + 1
            else:
                break
        return count

    def _log_status(self, rates: dict[str, float]) -> None:
        if not self._positions:
            best = max(rates.items(), key=lambda x: x[1], default=("—", 0.0))
            logger.info(
                f"[funding_arb] Нет активных позиций. "
                f"Лучший rate: {best[0]} {best[1]*100:.4f}% "
                f"(порог входа {self._entry_rate*100:.4f}%)"
            )
        else:
            for sym, pos in self._positions.items():
                rate = rates.get(sym, 0.0)
                logger.info(
                    f"[funding_arb] {sym}: rate={rate*100:.4f}% "
                    f"funding={pos.funding_collected:.4f} USDT "
                    f"hold={((time.time()-pos.entry_time)/3600):.1f}h"
                )

    async def _save_state(self) -> None:
        snapshot = await self.get_state_snapshot()
        if snapshot:
            await self._state.set_bot_state(self.name, snapshot)

    # ------------------------------------------------------------------
    # Персистентность (Redis)
    # ------------------------------------------------------------------

    async def get_state_snapshot(self) -> Optional[dict]:
        return {
            "positions": {
                sym: {
                    "symbol":              pos.symbol,
                    "spot_qty":            pos.spot_qty,
                    "usdt_size":           pos.usdt_size,
                    "spot_entry_price":    pos.spot_entry_price,
                    "perp_entry_price":    pos.perp_entry_price,
                    "entry_rate":          pos.entry_rate,
                    "entry_time":          pos.entry_time,
                    "last_funding_check":  pos.last_funding_check,
                    "funding_collected":   pos.funding_collected,
                    "negative_rate_count": pos.negative_rate_count,
                    "spot_order_id":       pos.spot_order_id,
                    "perp_order_id":       pos.perp_order_id,
                    "perp_sl_price":       pos.perp_sl_price,
                    "funding_interval_sec": pos.funding_interval_sec,
                    "qty_step":            pos.qty_step,
                    "min_qty":             pos.min_qty,
                }
                for sym, pos in self._positions.items()
            },
            "total_funding_earned":      self._total_funding_earned,
            "total_fees_usdt":           self._total_fees_usdt,
            "total_closed_positions":    self._total_closed_positions,
        }

    async def restore_state(self, saved: dict) -> None:
        for sym, d in saved.get("positions", {}).items():
            self._positions[sym] = ArbPosition(**d)
        self._total_funding_earned   = saved.get("total_funding_earned", 0.0)
        self._total_fees_usdt        = saved.get("total_fees_usdt", 0.0)
        self._total_closed_positions = saved.get("total_closed_positions", 0)
        logger.info(
            f"[funding_arb] Restored {len(self._positions)} positions from Redis."
        )
        if not self._paper and self._positions:
            await self._reconcile()

    async def _reconcile(self) -> None:
        """Сверка Redis-позиций с биржей после рестарта (04_ARCHITECTURE §3.3).

        Пока процесс лежал, перп-ногу мог закрыть DMS/вручную — тогда позиция
        в Redis фантомная. Фантом удаляем, но спот-ногу НЕ продаём автоматически:
        BTC на балансе не различим от инвентаря grid-бота — алертим владельцу.
        """
        try:
            real_positions = await self._exchange.get_open_positions()
            real_symbols = {p.symbol for p in real_positions}
        except Exception as e:
            logger.warning(f"[funding_arb] Reconcile error: {e}")
            return
        stale = [sym for sym in self._positions if sym not in real_symbols]
        for sym in stale:
            pos = self._positions.pop(sym)
            logger.warning(
                f"[funding_arb] {sym}: позиция в Redis, но перп-ноги нет на бирже — "
                f"удаляем фантом (spot_qty={pos.spot_qty:.6f})"
            )
            if self._notify:
                await self._notify(
                    f"⚠️ Funding Arb: перп-нога {sym} не найдена на бирже после рестарта.\n"
                    f"Фантом удалён из состояния. ПРОВЕРЬ спот-баланс: возможно, "
                    f"{pos.spot_qty:.6f} BTC остались без хеджа — закрой вручную."
                )
        if stale:
            await self._save_state()
            logger.info(f"[funding_arb] Reconcile: удалено {len(stale)} фантомных позиций")

    async def get_sleep_interval(self) -> float:
        return self.TICK_INTERVAL

    def get_stats(self) -> dict:
        return {
            "active_positions":       len(self._positions),
            "total_funding_earned":   round(self._total_funding_earned, 4),
            "total_fees_usdt":        round(self._total_fees_usdt, 4),
            "total_closed_positions": self._total_closed_positions,
            "positions": {
                sym: {
                    "funding_collected": round(pos.funding_collected, 4),
                    "hold_hours":        round((time.time() - pos.entry_time) / 3600, 1),
                    "entry_rate_pct":    round(pos.entry_rate * 100, 4),
                }
                for sym, pos in self._positions.items()
            },
        }
