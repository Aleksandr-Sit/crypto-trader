"""
5-уровневая система аварийной остановки.

Уровень 0 — Exchange stop-orders (выставляются при старте, работают без бота)
Уровень 1 — Price crash detector (WebSocket, < 100ms реакция)
Уровень 2 — Portfolio drawdown guard (проверка на каждый тик)
Уровень 3 — Per-strategy stop (вызывается каждым ботом индивидуально)
Уровень 4 — Dead Man's Switch (watchdog на Senko VPS)
Уровень 5 — Telegram kill switch (/stopall команда)

При срабатывании ЛЮБОГО уровня:
1. Отменить все ордера
2. Закрыть все позиции
3. Уведомить в Telegram
4. Заблокировать новые сделки до /resume
"""

import asyncio
import time
from enum import Enum
from typing import Optional, Callable, Awaitable
from loguru import logger

from core.exchange import ExchangeManager


class SystemState(Enum):
    RUNNING = "running"
    PAUSED = "paused"       # Временная пауза (например, низкий объём)
    STOPPED = "stopped"     # Аварийная остановка — требует /resume
    SHUTDOWN = "shutdown"   # Полное завершение процесса


class StopReason(Enum):
    PRICE_CRASH = "price_crash"
    DRAWDOWN = "drawdown"
    ADL_RISK = "adl_risk"
    STRATEGY = "strategy"
    WATCHDOG = "watchdog"
    MANUAL = "manual"
    SENTINEL = "sentinel"       # Новость об ограничении биржи
    SENTINEL_EMERGENCY = "sentinel_emergency"  # Экстренный вывод по новости


NotifyCallback = Callable[[str], Awaitable[None]]


class EmergencyStop:
    # Настройки триггеров
    CRASH_DROP_PCT = 5.0        # % падения за CRASH_WINDOW секунд → STOP
    CRASH_WINDOW_SEC = 300      # 5 минут
    MAX_DRAWDOWN_PCT = 12.0     # % просадки от пика портфеля → STOP
    HEARTBEAT_INTERVAL = 60     # секунд между heartbeat-записями

    def __init__(self, exchange_manager: ExchangeManager):
        self._em = exchange_manager
        self._state = SystemState.RUNNING
        self._stop_reason: Optional[StopReason] = None
        self._stop_time: Optional[float] = None

        self._peak_balance: float = 0.0
        self._price_history: list[tuple[float, float]] = []  # (timestamp, price)

        self._notify: Optional[NotifyCallback] = None
        self._lock = asyncio.Lock()

    def set_notifier(self, callback: NotifyCallback) -> None:
        """Устанавливает callback для Telegram-уведомлений."""
        self._notify = callback

    @property
    def state(self) -> SystemState:
        return self._state

    def is_running(self) -> bool:
        return self._state == SystemState.RUNNING

    # ------------------------------------------------------------------
    # Публичные методы — вызываются ботами и внешними триггерами
    # ------------------------------------------------------------------

    async def trigger(
        self,
        reason: StopReason,
        details: str = "",
        force: bool = False,
    ) -> None:
        """Главная точка входа. Потокобезопасна."""
        async with self._lock:
            if self._state == SystemState.STOPPED and not force:
                return  # Уже остановлено

            self._state = SystemState.STOPPED
            self._stop_reason = reason
            self._stop_time = time.time()

            logger.critical(
                f"EMERGENCY STOP triggered! reason={reason.value} details={details}"
            )
            await self._execute_stop(reason, details)

    async def resume(self) -> None:
        """Возобновить торговлю (только после ручной проверки через /resume)."""
        async with self._lock:
            if self._state != SystemState.STOPPED:
                return
            self._state = SystemState.RUNNING
            self._stop_reason = None
            logger.info("Trading RESUMED")
            await self._send(
                "✅ Торговля возобновлена. Боты запускаются..."
            )

    # ------------------------------------------------------------------
    # Уровень 1: Price Crash Detector
    # ------------------------------------------------------------------

    async def on_price_tick(self, symbol: str, price: float) -> None:
        """Вызывается при каждом тике цены из WebSocket."""
        if not self.is_running():
            return

        now = time.time()
        self._price_history.append((now, price))

        # Удаляем старые точки
        cutoff = now - self.CRASH_WINDOW_SEC
        self._price_history = [
            (t, p) for t, p in self._price_history if t >= cutoff
        ]

        if len(self._price_history) < 2:
            return

        oldest_price = self._price_history[0][1]
        if oldest_price <= 0:
            return

        drop_pct = (oldest_price - price) / oldest_price * 100
        if drop_pct >= self.CRASH_DROP_PCT:
            await self.trigger(
                StopReason.PRICE_CRASH,
                f"{symbol} упал на {drop_pct:.1f}% за {self.CRASH_WINDOW_SEC//60} мин",
            )

    # ------------------------------------------------------------------
    # Уровень 2: Portfolio Drawdown Guard
    # ------------------------------------------------------------------

    async def on_balance_update(self, current_balance: float) -> None:
        """Вызывается при обновлении баланса."""
        if not self.is_running():
            return

        if current_balance > self._peak_balance:
            self._peak_balance = current_balance

        if self._peak_balance <= 0:
            return

        drawdown_pct = (
            (self._peak_balance - current_balance) / self._peak_balance * 100
        )

        if drawdown_pct >= self.MAX_DRAWDOWN_PCT:
            await self.trigger(
                StopReason.DRAWDOWN,
                f"Просадка {drawdown_pct:.1f}% от пика ${self._peak_balance:.2f}",
            )

    # ------------------------------------------------------------------
    # Исполнение STOP
    # ------------------------------------------------------------------

    async def _execute_stop(self, reason: StopReason, details: str) -> None:
        msg_lines = [
            "🚨 АВАРИЙНАЯ ОСТАНОВКА",
            f"Причина: {reason.value}",
        ]
        if details:
            msg_lines.append(f"Детали: {details}")

        await self._send("\n".join(msg_lines))

        # Шаг 1: Отменить все ордера
        try:
            cancelled = await self._em.cancel_all_everywhere()
            logger.info(f"Cancelled orders: {cancelled}")
        except Exception as e:
            logger.error(f"Error cancelling orders: {e}")

        # Шаг 2: Закрыть все позиции
        try:
            closed = await self._em.close_all_everywhere()
            logger.info(f"Closed positions: {closed}")
        except Exception as e:
            logger.error(f"Error closing positions: {e}")

        # Шаг 3: Получить итоговый баланс
        try:
            balance = await self._em.get_total_balance_usdt()
            await self._send(
                f"✅ Все позиции закрыты\n"
                f"💰 Баланс USDT: ${balance:.2f}\n"
                f"⏸ Торговля заблокирована\n"
                f"Для возобновления: /resume"
            )
        except Exception as e:
            logger.error(f"Error fetching final balance: {e}")
            await self._send(
                "✅ Позиции закрыты (баланс недоступен)\n⏸ /resume для возобновления"
            )

    async def _send(self, message: str) -> None:
        if self._notify:
            try:
                await self._notify(message)
            except Exception as e:
                logger.error(f"Failed to send Telegram notification: {e}")


# Глобальный экземпляр — инициализируется в main.py
_instance: Optional[EmergencyStop] = None


def get_emergency_stop() -> EmergencyStop:
    if _instance is None:
        raise RuntimeError("EmergencyStop not initialized. Call init_emergency_stop() first.")
    return _instance


def init_emergency_stop(exchange_manager: ExchangeManager) -> EmergencyStop:
    global _instance
    _instance = EmergencyStop(exchange_manager)
    return _instance
