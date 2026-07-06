"""
Grid Bot — Стратегия 1: Spot Grid Trading на BTCUSDT через Bybit.

Логика:
1. Строим сетку цен в диапазоне ±range_pct% от текущей цены
2. BUY limit-ордера на каждом уровне НИЖЕ текущей цены (оплата в USDT)
3. SELL limit-ордера на каждом уровне ВЫШЕ текущей цены (оплата в BTC)
4. При исполнении BUY → ставим SELL на шаг выше → фиксируем прибыль grid_step * qty
5. При исполнении SELL → ставим BUY на шаг ниже → продолжаем цикл
6. Каждые rebalance_interval_h часов: пересчитываем сетку если цена ушла >20% от центра
7. Hard stop: если цена ниже нижней границы сетки → Emergency Stop

Paper mode: симулируем исполнение ордеров по пересечению цены (без реальных заявок).
Live mode: отслеживаем реальные ордера через polling get_open_orders каждые 30 сек.

Защита:
- PostOnly ордера на Bybit (maker rebate -0.01%, не платим комиссию)
- Интеграция с EmergencyStop (hard_stop_below_range)
- Состояние персистентно в Redis (переживает рестарты)
"""

import asyncio
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from loguru import logger

from bots.base import BaseBot
from core.exchange import ExchangeAdapter
from core.state import StateStore
from core.emergency_stop import EmergencyStop

# Минимальный размер ордера на Bybit BTCUSDT
BYBIT_BTC_MIN_QTY = 0.000048
BTC_QTY_PRECISION = 6   # 6 знаков после запятой
PRICE_PRECISION    = 2   # 2 знака для цены


@dataclass
class ActiveOrder:
    side: str       # "Buy" или "Sell"
    qty: float      # BTC
    order_id: str   # ID на бирже (или "paper-N" в paper mode)


class GridBot(BaseBot):
    TICK_INTERVAL = 30.0  # секунд между проверками

    def __init__(
        self,
        exchange: ExchangeAdapter,
        config: dict,
        state_store: StateStore,
        emergency_stop: EmergencyStop,
        paper_mode: bool = False,
    ):
        super().__init__("grid_bot", state_store, emergency_stop)
        self._exchange   = exchange
        self._cfg        = config
        self._paper      = paper_mode

        # Параметры из конфига
        self._symbol        : str   = config["symbol"]
        self._grid_count    : int   = config["grid_count"]
        self._range_pct     : float = config["range_pct"] / 100.0
        self._hard_stop     : bool  = config.get("hard_stop_below_range", True)
        self._rebalance_h   : float = config.get("rebalance_interval_h", 24.0)

        # Состояние сетки (персистируется в Redis)
        self._active_orders  : dict[float, ActiveOrder] = {}
        self._grid_low       : float = 0.0
        self._grid_high      : float = 0.0
        self._grid_step      : float = 0.0
        self._capital_usdt   : float = 0.0
        self._qty_per_level  : float = 0.0
        self._last_rebalance : float = 0.0
        self._initialized    : bool  = False

        # Комиссия: отрицательное = maker rebate (зарабатываем), положительное = платим
        self._fee_pct       : float = config.get("fee_pct", -0.0002)

        # Статистика
        self._total_trades  : int   = 0
        self._total_profit  : float = 0.0
        self._total_fees_usdt: float = 0.0

        # Инвентарь BTC — для честного mark-to-market PnL (02_VALIDATION_REPORT §4.3):
        # без него бот показывает «прибыль» в медвежьем тренде, теряя на инвентаре
        self._btc_inventory  : float = 0.0   # BTC на руках (seed + buy-филлы − sell-филлы)
        self._inventory_cost : float = 0.0   # стоимость покупки этого BTC, USDT
        self._last_price     : float = 0.0

        # Regime-фильтр (07_SPRINT_VALIDATION: donchian20+flatten — DD −77%→−30%):
        # в подтверждённом down-тренде сетка ставится на паузу с продажей инвентаря
        rf_cfg = config.get("regime_filter", {})
        self._rf_enabled     : bool  = rf_cfg.get("enabled", False)
        self._rf_days        : int   = rf_cfg.get("donchian_days", 20)
        self._regime_allowed : bool  = True
        self._rf_checked_at  : float = 0.0   # кэш пересчёта фильтра (1 час)

        # Счётчик пересборок сетки (сбрасывается в полночь UTC)
        self._rebuilds_today: int   = 0
        self._rebuild_date  : str   = ""

        # Paper mode счётчик ID
        self._paper_counter : int = 0

    # ------------------------------------------------------------------
    # Основной цикл
    # ------------------------------------------------------------------

    async def tick(self) -> None:
        current_price = await self._exchange.get_ticker(self._symbol)
        self._last_price = current_price

        # Regime-фильтр: в down-тренде паузим сетку (flatten), в up — работаем
        if self._rf_enabled:
            await self._refresh_regime()
            if not self._regime_allowed:
                if self._initialized:
                    await self._flatten_and_pause(current_price)
                return

        if not self._initialized:
            await self._initialize_grid(current_price)
            return

        # Цена вышла за нижнюю границу — перестраиваем сетку, а не останавливаем всё.
        # Глобальный EmergencyStop здесь неуместен: рыночная коррекция не повод
        # убивать Freqtrade, Funding Arb и остальные независимые боты.
        if self._hard_stop and current_price < self._grid_low:
            logger.warning(
                f"[grid_bot] Price {current_price:.2f} < grid floor {self._grid_low:.2f}. "
                "Rebuilding grid around new price."
            )
            self._increment_rebuild_counter()
            if not self._paper:
                try:
                    await self._exchange.cancel_all_orders(self._symbol)
                except Exception as e:
                    logger.error(f"[grid_bot] cancel_all_orders failed on rebuild: {e}")
            self._active_orders.clear()
            self._initialized = False
            return

        # Ребаланс по расписанию
        hours_elapsed = (time.time() - self._last_rebalance) / 3600
        if hours_elapsed >= self._rebalance_h:
            await self._maybe_rebalance(current_price)
            return

        # Синхронизировать исполненные ордера
        if self._paper:
            await self._sync_paper_fills(current_price)
        else:
            await self._sync_live_fills(current_price)

    async def get_sleep_interval(self) -> float:
        return self.TICK_INTERVAL

    # ------------------------------------------------------------------
    # Инициализация сетки
    # ------------------------------------------------------------------

    async def _initialize_grid(self, current_price: float) -> None:
        logger.info(f"[grid_bot] Initializing grid at price {current_price:.2f}")

        # Live: отменяем возможные «осиротевшие» ордера от предыдущей сетки.
        # (Если rebalance/crash не успел отменить их — дубликаты сожгут капитал.)
        if not self._paper:
            try:
                await self._exchange.cancel_all_orders(self._symbol)
            except Exception as e:
                logger.warning(f"[grid_bot] Pre-init cancel error (non-critical): {e}")

        # Получить капитал
        cfg_capital = self._cfg.get("capital_usdt", 0.0)
        if self._paper:
            # Paper mode: используем виртуальный капитал из конфига
            self._capital_usdt = cfg_capital if cfg_capital > 0 else 500.0
            logger.info(f"[grid_bot] [PAPER] Simulated capital: {self._capital_usdt:.2f} USDT")
        else:
            balance = await self._exchange.get_balance("USDT")
            if cfg_capital > 0:
                self._capital_usdt = min(cfg_capital, balance.available)
            else:
                self._capital_usdt = balance.available * 0.60

        if self._capital_usdt < 20:
            logger.warning(
                f"[grid_bot] Insufficient capital: {self._capital_usdt:.2f} USDT "
                "(need >= $20). Waiting 60s."
            )
            await asyncio.sleep(60)
            return

        # Вычислить параметры сетки
        self._grid_low  = round(current_price * (1 - self._range_pct), PRICE_PRECISION)
        self._grid_high = round(current_price * (1 + self._range_pct), PRICE_PRECISION)
        self._grid_step = round(
            (self._grid_high - self._grid_low) / self._grid_count, PRICE_PRECISION
        )

        # Уровни сетки (исключаем текущую цену ± один шаг)
        prices = [
            round(self._grid_low + i * self._grid_step, PRICE_PRECISION)
            for i in range(self._grid_count + 1)
        ]
        buy_prices  = [p for p in prices if p < current_price - self._grid_step * 0.1]
        sell_prices = [p for p in prices if p > current_price + self._grid_step * 0.1]

        # Капитал делим пополам: USDT на BUY, USDT для покупки BTC под SELL
        n_buy = len(buy_prices)
        usdt_per_buy = (self._capital_usdt / 2) / max(n_buy, 1)
        self._qty_per_level = math.floor(
            (usdt_per_buy / current_price) * 10 ** BTC_QTY_PRECISION
        ) / 10 ** BTC_QTY_PRECISION

        if self._qty_per_level < BYBIT_BTC_MIN_QTY:
            logger.warning(
                f"[grid_bot] qty_per_level={self._qty_per_level:.6f} BTC < min {BYBIT_BTC_MIN_QTY}. "
                f"Increase capital (need >= ${BYBIT_BTC_MIN_QTY * current_price * n_buy * 2:.0f} USDT). "
                "Waiting 60s."
            )
            await asyncio.sleep(60)
            return

        logger.info(
            f"[grid_bot] Grid: {self._grid_low:.2f} — {self._grid_high:.2f} | "
            f"step={self._grid_step:.2f} | buy={n_buy} sell={len(sell_prices)} | "
            f"qty={self._qty_per_level:.6f} BTC | capital={self._capital_usdt:.2f} USDT"
        )

        # В live-режиме: купить BTC для SELL-стороны сетки
        if not self._paper and sell_prices:
            await self._seed_sell_side(sell_prices, current_price)

        # Инвентарный учёт seed-а (paper и live единообразно): добираем недостающий
        # BTC под sell-сторону по текущей цене. Уже имеющийся инвентарь сохраняется.
        seed_qty = len(sell_prices) * self._qty_per_level
        if seed_qty > self._btc_inventory:
            add = seed_qty - self._btc_inventory
            self._inventory_cost += add * current_price
            self._btc_inventory = seed_qty

        # Разместить начальные ордера
        for price in buy_prices:
            await self._place_order(price, "Buy", self._qty_per_level)
        for price in sell_prices:
            await self._place_order(price, "Sell", self._qty_per_level)

        self._initialized    = True
        self._last_rebalance = time.time()
        await self._save_state()

        mode_tag = "[PAPER]" if self._paper else "[LIVE]"
        logger.info(
            f"[grid_bot] {mode_tag} Grid ready. "
            f"{len(self._active_orders)} active orders."
        )

    async def _seed_sell_side(self, sell_prices: list[float], current_price: float) -> None:
        """Покупаем BTC по рынку для заполнения SELL-стороны сетки."""
        btc_needed = len(sell_prices) * self._qty_per_level
        try:
            current_btc = await self._exchange.get_balance("BTC")
            btc_to_buy = max(0.0, btc_needed - current_btc.available)
        except Exception:
            btc_to_buy = btc_needed

        btc_to_buy = math.floor(btc_to_buy * 10 ** BTC_QTY_PRECISION) / 10 ** BTC_QTY_PRECISION
        if btc_to_buy >= BYBIT_BTC_MIN_QTY:
            logger.info(f"[grid_bot] Buying {btc_to_buy:.6f} BTC at market to seed sell side")
            try:
                await self._exchange.place_market_order(self._symbol, "Buy", btc_to_buy)
            except Exception as e:
                logger.error(f"[grid_bot] Failed to seed sell side: {e}")

    # ------------------------------------------------------------------
    # Размещение одного ордера
    # ------------------------------------------------------------------

    async def _place_order(self, price: float, side: str, qty: float) -> None:
        if price in self._active_orders:
            return  # Уже есть ордер на этом уровне

        if self._paper:
            order_id = f"paper-{self._paper_counter}"
            self._paper_counter += 1
        else:
            try:
                order = await self._exchange.place_limit_order(
                    self._symbol, side, qty, price
                )
                order_id = order.order_id
                await asyncio.sleep(0.12)  # ~8 ордеров/сек, Bybit rate limit
            except Exception as e:
                logger.error(f"[grid_bot] Failed to place {side} @ {price:.2f}: {e}")
                return

        self._active_orders[price] = ActiveOrder(side=side, qty=qty, order_id=order_id)
        logger.debug(
            f"[grid_bot] {'[P]' if self._paper else ''} "
            f"{side} @ {price:.2f} qty={qty:.6f} id={order_id}"
        )

    # ------------------------------------------------------------------
    # Обнаружение исполненных ордеров
    # ------------------------------------------------------------------

    async def _sync_paper_fills(self, current_price: float) -> None:
        for price, ao in list(self._active_orders.items()):
            filled = (
                (ao.side == "Buy"  and current_price <= price) or
                (ao.side == "Sell" and current_price >= price)
            )
            if filled:
                del self._active_orders[price]
                await self._handle_fill(price, ao.side, ao.qty, current_price)

    async def _sync_live_fills(self, current_price: float) -> None:
        try:
            open_orders = await self._exchange.get_open_orders(self._symbol)
        except Exception as e:
            logger.error(f"[grid_bot] get_open_orders failed: {e}")
            return

        open_ids = {o.order_id for o in open_orders}

        # Если 2+ отслеживаемых ордера и ВСЕ пропали с биржи разом —
        # это, скорее всего, emergency cancel, а не нормальное исполнение.
        # При нормальной торговле ордера исполняются по одному.
        # Трактуем как отмену → перестраиваем сетку, а не создаём встречные ордера.
        if len(self._active_orders) >= 2 and not any(
            ao.order_id in open_ids for ao in self._active_orders.values()
        ):
            logger.warning(
                f"[grid_bot] Все {len(self._active_orders)} ордеров пропали "
                "с биржи разом — вероятно, emergency cancel. Перестраиваем сетку."
            )
            self._active_orders.clear()
            self._initialized = False
            await self._save_state()
            return

        for price, ao in list(self._active_orders.items()):
            if ao.order_id not in open_ids:
                # Ордер пропал из открытых → считаем исполненным.
                # (Cancel во время ребаланса очищает _active_orders до вызова cancel_all,
                #  поэтому сюда попадают только реально исполненные ордера.)
                del self._active_orders[price]
                await self._handle_fill(price, ao.side, ao.qty, current_price)

    # ------------------------------------------------------------------
    # Обработка исполнения: разместить встречный ордер
    # ------------------------------------------------------------------

    async def _handle_fill(
        self, filled_price: float, filled_side: str, qty: float, current_price: float
    ) -> None:
        self._total_trades += 1
        # Прибыль считаем только для SELL fills — каждая продажа закрывает BUY на шаг ниже.
        # fee_pct × notional × 2 = round-trip комиссия (отрицательная = maker rebate)
        if filled_side == "Sell":
            fee_usdt = self._fee_pct * qty * filled_price * 2
            profit = self._grid_step * qty - fee_usdt
            self._total_profit += profit
            self._total_fees_usdt += fee_usdt
            # Инвентарь: списываем проданный BTC по средней стоимости
            if self._btc_inventory > 1e-12:
                avg_cost = self._inventory_cost / self._btc_inventory
                sold = min(qty, self._btc_inventory)
                self._inventory_cost -= avg_cost * sold
                self._btc_inventory -= sold
        else:
            profit = 0.0
            # Инвентарь: купленный BTC добавляем по цене филла
            self._btc_inventory += qty
            self._inventory_cost += qty * filled_price

        await self._save_state()

        logger.info(
            f"[grid_bot] FILL #{self._total_trades}: {filled_side} @ {filled_price:.2f} | "
            f"~profit ${profit:.4f} | total profit ${self._total_profit:.4f}"
        )

        # Встречный ордер
        if filled_side == "Buy":
            counter_price = round(filled_price + self._grid_step, PRICE_PRECISION)
            counter_side  = "Sell"
        else:
            counter_price = round(filled_price - self._grid_step, PRICE_PRECISION)
            counter_side  = "Buy"

        # Проверяем границы сетки
        if counter_price < self._grid_low or counter_price > self._grid_high:
            logger.debug(
                f"[grid_bot] Counter @ {counter_price:.2f} outside grid — skip"
            )
            return

        await self._place_order(counter_price, counter_side, qty)

    # ------------------------------------------------------------------
    # Ребаланс (раз в 24ч)
    # ------------------------------------------------------------------

    async def _maybe_rebalance(self, current_price: float) -> None:
        center = (self._grid_low + self._grid_high) / 2
        drift_pct = abs(current_price - center) / center * 100

        logger.info(
            f"[grid_bot] Rebalance check: center={center:.2f} "
            f"current={current_price:.2f} drift={drift_pct:.1f}%"
        )

        if drift_pct > 20:
            logger.warning(
                f"[grid_bot] Drift {drift_pct:.1f}% > 20% — rebuilding grid"
            )
            self._increment_rebuild_counter()
            # Очищаем внутреннее состояние ДО отмены ордеров на бирже,
            # чтобы _sync_live_fills не воспринял отменённые ордера как fills.
            self._active_orders.clear()
            self._initialized = False
            if not self._paper:
                try:
                    await self._exchange.cancel_all_orders(self._symbol)
                except Exception as e:
                    logger.error(f"[grid_bot] cancel_all_orders failed: {e}")
        else:
            logger.info(
                f"[grid_bot] Drift {drift_pct:.1f}% within tolerance — grid unchanged"
            )
            self._last_rebalance = time.time()

    # ------------------------------------------------------------------
    # Regime-фильтр (donchian20 + flatten, 07_SPRINT_VALIDATION)
    # ------------------------------------------------------------------

    async def _refresh_regime(self) -> None:
        """Гистерезис по дневкам: OFF при close < min(low N дней),
        ON при close > max(high N дней), иначе держим состояние. Кэш 1 час."""
        now = time.time()
        if now - self._rf_checked_at < 3600:
            return
        try:
            klines = await self._exchange.get_klines(
                self._symbol, "D", self._rf_days + 3
            )
        except Exception as e:
            logger.warning(f"[grid_bot] Regime klines error: {e}")
            return
        closed = klines[:-1]  # последний бар формируется
        if len(closed) < self._rf_days + 1:
            return
        last = closed[-1]
        window = closed[-(self._rf_days + 1):-1]
        prev_low = min(c["low"] for c in window)
        prev_high = max(c["high"] for c in window)
        old = self._regime_allowed
        if last["close"] < prev_low:
            self._regime_allowed = False
        elif last["close"] > prev_high:
            self._regime_allowed = True
        self._rf_checked_at = now
        if old != self._regime_allowed:
            state_txt = "ON (up-тренд)" if self._regime_allowed else "OFF (down-тренд)"
            logger.warning(f"[grid_bot] Regime-фильтр: {state_txt}")
            await self._save_state()

    async def _flatten_and_pause(self, price: float) -> None:
        """Down-режим: отменить ордера, продать инвентарь, паузить сетку.
        Только flatten — пауза с удержанием инвентаря отвергнута данными
        (07_SPRINT_VALIDATION: hold ухудшает DD)."""
        logger.warning(
            f"[grid_bot] Regime OFF: flatten — продаём инвентарь "
            f"{self._btc_inventory:.6f} BTC @ ~{price:.2f}, пауза сетки"
        )
        self._active_orders.clear()
        self._initialized = False
        if not self._paper:
            try:
                await self._exchange.cancel_all_orders(self._symbol)
            except Exception as e:
                logger.error(f"[grid_bot] Flatten cancel error: {e}")

        if self._btc_inventory > 1e-9:
            qty = math.floor(self._btc_inventory * 10 ** BTC_QTY_PRECISION) \
                / 10 ** BTC_QTY_PRECISION
            if not self._paper and qty >= BYBIT_BTC_MIN_QTY:
                try:
                    await self._exchange.place_market_order(self._symbol, "Sell", qty)
                except Exception as e:
                    logger.error(f"[grid_bot] Flatten sell error: {e}")
                    await self._save_state()
                    return  # инвентарь не продан — не обнуляем учёт
            # Реализуем PnL инвентаря в учёте (paper и live единообразно)
            avg_cost = (self._inventory_cost / self._btc_inventory
                        if self._btc_inventory > 1e-12 else price)
            fee = 0.001 * self._btc_inventory * price  # taker за flatten
            realized = (price - avg_cost) * self._btc_inventory - fee
            self._total_profit += realized
            self._total_fees_usdt += fee
            logger.info(f"[grid_bot] Flatten realized PnL: {realized:+.2f} USDT")
            self._btc_inventory = 0.0
            self._inventory_cost = 0.0
        await self._save_state()

    # ------------------------------------------------------------------
    # Состояние для Redis
    # ------------------------------------------------------------------

    async def _save_state(self) -> None:
        snap = await self.get_state_snapshot()
        if snap:
            await self._state.set_bot_state(self.name, snap)

    async def get_state_snapshot(self) -> Optional[dict]:
        return {
            "grid_low"       : self._grid_low,
            "grid_high"      : self._grid_high,
            "grid_step"      : self._grid_step,
            "capital_usdt"   : self._capital_usdt,
            "qty_per_level"  : self._qty_per_level,
            "last_rebalance" : self._last_rebalance,
            "initialized"    : self._initialized,
            "total_trades"    : self._total_trades,
            "total_profit"    : self._total_profit,
            "total_fees_usdt" : self._total_fees_usdt,
            "btc_inventory"   : self._btc_inventory,
            "inventory_cost"  : self._inventory_cost,
            "last_price"      : self._last_price,
            "regime_allowed"  : self._regime_allowed,
            "paper_counter"   : self._paper_counter,
            "rebuilds_today" : self._rebuilds_today,
            "rebuild_date"   : self._rebuild_date,
            "active_orders"  : {
                str(price): {
                    "side"    : ao.side,
                    "qty"     : ao.qty,
                    "order_id": ao.order_id,
                }
                for price, ao in self._active_orders.items()
            },
        }

    async def restore_state(self, saved: dict) -> None:
        self._grid_low       = saved.get("grid_low", 0.0)
        self._grid_high      = saved.get("grid_high", 0.0)
        self._grid_step      = saved.get("grid_step", 0.0)
        self._capital_usdt   = saved.get("capital_usdt", 0.0)
        self._qty_per_level  = saved.get("qty_per_level", 0.0)
        self._last_rebalance = saved.get("last_rebalance", 0.0)
        self._initialized    = saved.get("initialized", False)
        self._total_trades    = saved.get("total_trades", 0)
        self._total_profit    = saved.get("total_profit", 0.0)
        self._total_fees_usdt = saved.get("total_fees_usdt", 0.0)
        self._btc_inventory   = saved.get("btc_inventory", 0.0)
        self._inventory_cost  = saved.get("inventory_cost", 0.0)
        self._last_price      = saved.get("last_price", 0.0)
        self._regime_allowed  = saved.get("regime_allowed", True)
        self._paper_counter   = saved.get("paper_counter", 0)
        self._rebuilds_today = saved.get("rebuilds_today", 0)
        self._rebuild_date   = saved.get("rebuild_date", "")

        for price_str, ao in saved.get("active_orders", {}).items():
            self._active_orders[float(price_str)] = ActiveOrder(
                side=ao["side"], qty=ao["qty"], order_id=ao["order_id"]
            )
        logger.info(
            f"[grid_bot] State restored: {len(self._active_orders)} orders, "
            f"{self._total_trades} trades, ${self._total_profit:.4f} profit, "
            f"{self._rebuilds_today} rebuilds today"
        )

    # ------------------------------------------------------------------
    # Счётчик пересборок (сбрасывается ежедневно в полночь UTC)
    # ------------------------------------------------------------------

    def _increment_rebuild_counter(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._rebuild_date:
            self._rebuilds_today = 0
            self._rebuild_date   = today
        self._rebuilds_today += 1
        logger.info(f"[grid_bot] Rebuild #{self._rebuilds_today} today ({today})")

    # ------------------------------------------------------------------
    # Публичные метрики (для Telegram /status и AnalyzerBot)
    # ------------------------------------------------------------------

    def get_stats(self) -> dict:
        buy_orders  = sum(1 for ao in self._active_orders.values() if ao.side == "Buy")
        sell_orders = sum(1 for ao in self._active_orders.values() if ao.side == "Sell")
        # Mark-to-market: без него в тренде вниз total_profit «растёт», а портфель
        # теряет на инвентаре (02_VALIDATION_REPORT §4.3: 2022 — отчёт +$102, факт −$207)
        avg_cost = (
            self._inventory_cost / self._btc_inventory
            if self._btc_inventory > 1e-12 else 0.0
        )
        unrealized = (
            (self._last_price - avg_cost) * self._btc_inventory
            if self._last_price > 0 and self._btc_inventory > 1e-12 else 0.0
        )
        return {
            "symbol"          : self._symbol,
            "grid_low"        : self._grid_low,
            "grid_high"       : self._grid_high,
            "grid_step"       : self._grid_step,
            "capital_usdt"    : self._capital_usdt,
            "qty_per_level"   : self._qty_per_level,
            "buy_orders"      : buy_orders,
            "sell_orders"     : sell_orders,
            "total_orders"    : len(self._active_orders),
            "total_trades"    : self._total_trades,
            "total_profit_usd": self._total_profit,
            "btc_inventory"       : round(self._btc_inventory, 6),
            "inventory_avg_cost"  : round(avg_cost, 2),
            "unrealized_pnl_usd"  : round(unrealized, 2),
            "net_equity_pnl_usd"  : round(self._total_profit + unrealized, 2),
            "total_fees_usdt" : round(self._total_fees_usdt, 4),
            "rebuilds_today"  : self._rebuilds_today,
            "regime"          : ("ON" if self._regime_allowed else "OFF (paused)")
                                if self._rf_enabled else "filter disabled",
            "mode"            : "paper" if self._paper else "live",
        }
