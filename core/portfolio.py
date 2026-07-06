"""
Управление портфелем: балансы, аллокация, P&L, реинвестирование.
"""

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from loguru import logger

from core.exchange import ExchangeManager


@dataclass
class BotAllocation:
    name: str
    exchange: str
    capital_usdt: float        # Текущий выделенный капитал
    initial_capital: float     # Стартовый капитал (для расчёта P&L)
    max_capital_pct: float     # Максимум от общего портфеля, %


@dataclass
class PortfolioSnapshot:
    timestamp: float
    total_usdt: float
    allocations: dict[str, float]   # bot_name → current_value
    daily_pnl: float
    total_pnl: float


class PortfolioManager:
    """
    Отслеживает капитал каждого бота и управляет реинвестированием.

    Аллокация по умолчанию:
      grid_bot      → 50% капитала
      funding_arb   → 30% капитала
      nfi_bot       → 10% капитала
      scalper       → фиксированные $300 (агрессивный бот)
    """

    SCALPER_FIXED_CAPITAL = 300.0   # Скальпер всегда торгует этой суммой
    MIN_REDISTRIBUTE = 5.0          # Минимальная прибыль для реинвестирования $

    def __init__(self, exchange_manager: ExchangeManager, total_capital: float):
        self._em = exchange_manager
        self._total_capital = total_capital
        self._start_time = time.time()
        self._daily_start_balance: float = total_capital
        self._last_day: int = 0

        self._allocations: dict[str, BotAllocation] = self._init_allocations(
            total_capital
        )
        self._scalper_baseline = self.SCALPER_FIXED_CAPITAL
        self._scalper_pnl_today = 0.0

    def _init_allocations(self, total: float) -> dict[str, BotAllocation]:
        tradeable = total - self.SCALPER_FIXED_CAPITAL  # Остаток без скальпера
        return {
            "grid_bot": BotAllocation(
                name="grid_bot", exchange="bybit",
                capital_usdt=tradeable * 0.50,
                initial_capital=tradeable * 0.50,
                max_capital_pct=60.0,
            ),
            "funding_arb": BotAllocation(
                name="funding_arb", exchange="bybit",
                capital_usdt=tradeable * 0.30,
                initial_capital=tradeable * 0.30,
                max_capital_pct=40.0,
            ),
            "nfi_bot": BotAllocation(
                name="nfi_bot", exchange="bybit",
                capital_usdt=tradeable * 0.20,
                initial_capital=tradeable * 0.20,
                max_capital_pct=25.0,
            ),
            "scalper": BotAllocation(
                name="scalper", exchange="bybit",
                capital_usdt=self.SCALPER_FIXED_CAPITAL,
                initial_capital=self.SCALPER_FIXED_CAPITAL,
                max_capital_pct=15.0,
            ),
        }

    def get_allocation(self, bot_name: str) -> Optional[BotAllocation]:
        return self._allocations.get(bot_name)

    def get_capital(self, bot_name: str) -> float:
        alloc = self._allocations.get(bot_name)
        return alloc.capital_usdt if alloc else 0.0

    def report_scalper_pnl(self, pnl: float) -> None:
        """Скальпер сообщает свой P&L за день."""
        self._scalper_pnl_today += pnl

    async def redistribute_daily(self) -> Optional[dict[str, float]]:
        """
        Вызывается раз в день (00:00 UTC).
        Если скальпер в плюсе → распределяет прибыль по консервативным ботам.
        Возвращает словарь с суммами пополнений или None если нечего делить.
        """
        profit = self._scalper_pnl_today
        self._scalper_pnl_today = 0.0  # Сброс на новый день

        if profit < self.MIN_REDISTRIBUTE:
            logger.info(
                f"Redistribution skipped: scalper profit ${profit:.2f} < "
                f"${self.MIN_REDISTRIBUTE} minimum"
            )
            return None

        added = {
            "grid_bot":    profit * 0.50,
            "funding_arb": profit * 0.30,
            "nfi_bot":     profit * 0.20,
        }

        for bot, amount in added.items():
            self._allocations[bot].capital_usdt += amount
            logger.info(
                f"Redistribution: +${amount:.2f} → {bot} "
                f"(new total: ${self._allocations[bot].capital_usdt:.2f})"
            )

        self._total_capital += profit
        return added

    async def get_snapshot(self) -> PortfolioSnapshot:
        total = await self._em.get_total_balance_usdt()

        # Сбрасываем базовую линию в полночь UTC
        today = datetime.now(timezone.utc).day
        if today != self._last_day:
            self._daily_start_balance = total
            self._last_day = today

        daily_pnl = total - self._daily_start_balance
        total_pnl = total - self._total_capital

        return PortfolioSnapshot(
            timestamp=time.time(),
            total_usdt=total,
            allocations={
                name: alloc.capital_usdt
                for name, alloc in self._allocations.items()
            },
            daily_pnl=daily_pnl,
            total_pnl=total_pnl,
        )

    def format_report(self, snapshot: PortfolioSnapshot) -> str:
        pnl_emoji = "📈" if snapshot.daily_pnl >= 0 else "📉"
        lines = [
            f"💼 Портфель: ${snapshot.total_usdt:.2f}",
            f"{pnl_emoji} За день: {snapshot.daily_pnl:+.2f}$",
            f"📊 Всего P&amp;L: {snapshot.total_pnl:+.2f}$",
            "─────────────",
        ]
        for bot, capital in snapshot.allocations.items():
            lines.append(f"  {bot}: ${capital:.2f}")
        return "\n".join(lines)
