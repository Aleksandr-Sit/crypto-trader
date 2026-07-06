"""
Аналитика и параметрический советник.

PerformanceTracker  — сохраняет дневные снимки всех ботов в Redis (35 дней TTL).
ParameterAdvisor    — анализирует 7-дневный тренд, предлагает корректировки параметров.
                      НЕ меняет конфиг автоматически — только рекомендует.
"""

import time
from datetime import datetime, timezone, timedelta
from loguru import logger

from core.state import StateStore

SNAPSHOT_TTL_DAYS = 35


class PerformanceTracker:
    KEY_PREFIX = "analytics:daily:"

    def __init__(self, state: StateStore):
        self._state = state

    async def save_snapshot(self, bots: dict, portfolio) -> dict:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        portfolio_data = None
        try:
            snap = await portfolio.get_snapshot()
            portfolio_data = {
                "total_usdt": round(snap.total_usdt, 2),
                "daily_pnl":  round(snap.daily_pnl, 2),
                "total_pnl":  round(snap.total_pnl, 2),
            }
        except Exception as e:
            logger.warning(f"[analytics] portfolio snapshot error: {e}")

        bot_stats: dict = {}
        for name, bot in bots.items():
            try:
                bot_stats[name] = bot.get_stats()
            except Exception as e:
                logger.warning(f"[analytics] get_stats({name}) error: {e}")

        record = {
            "date":      date_str,
            "ts":        time.time(),
            "portfolio": portfolio_data,
            "bots":      bot_stats,
        }

        key = f"{self.KEY_PREFIX}{date_str}"
        await self._state.set(key, record, ttl=SNAPSHOT_TTL_DAYS * 86400)
        logger.info(f"[analytics] Snapshot saved → {key}")
        return record

    async def get_history(self, days: int = 7) -> list[dict]:
        results = []
        now = datetime.now(timezone.utc)
        for i in range(days):
            d = (now - timedelta(days=i)).strftime("%Y-%m-%d")
            rec = await self._state.get(f"{self.KEY_PREFIX}{d}")
            if rec:
                results.append(rec)
        return list(reversed(results))  # от старого к новому


class ParameterAdvisor:
    def analyze(self, history: list[dict], config: dict) -> list[str]:
        if not history:
            return ["⏳ Нет данных (бот работает первый день)"]

        hints: list[str] = []
        latest = history[-1]

        # ---------------------------------------------------------------
        # Grid Bot
        # ---------------------------------------------------------------
        grid = latest.get("bots", {}).get("grid_bot", {})
        rebuilds = grid.get("rebuilds_today", 0)

        if rebuilds >= 3:
            cur = config.get("grid_bot", {}).get("range_pct", 25)
            hints.append(
                f"⚠️ Grid: {rebuilds} пересборок за день — диапазон слишком узкий.\n"
                f"   Предложение: range_pct {cur}% → {cur + 5}% в config.yaml"
            )
        else:
            if len(history) >= 7:
                avg_trades = sum(
                    h.get("bots", {}).get("grid_bot", {}).get("total_trades", 0)
                    for h in history
                ) / len(history)
                if avg_trades < 5:
                    hints.append(
                        "⚠️ Grid: меньше 5 сделок/день — возможно BTC вышел за диапазон.\n"
                        "   Проверь, что grid инициализирован."
                    )
                else:
                    hints.append(
                        f"✅ Grid: {grid.get('total_trades', 0)} сделок, "
                        f"{rebuilds} пересборок — диапазон оптимален"
                    )

        # ---------------------------------------------------------------
        # Funding Arb
        # ---------------------------------------------------------------
        arb = latest.get("bots", {}).get("funding_arb", {})

        if len(history) >= 3:
            zero_days = sum(
                1 for h in history[-3:]
                if h.get("bots", {}).get("funding_arb", {}).get("active_positions", 0) == 0
            )
            if zero_days >= 3:
                cur = config.get("funding_arb", {}).get("entry_rate_threshold", 0.0003)
                new = round(cur - 0.00005, 6)
                hints.append(
                    f"⚠️ Funding Arb: 3+ дня без позиций — ставки ниже порога входа.\n"
                    f"   Предложение: entry_rate_threshold {cur*100:.4f}% → {new*100:.4f}%"
                )
            elif arb.get("active_positions", 0) > 0:
                earned = arb.get("total_funding_earned", 0)
                hints.append(
                    f"✅ Funding Arb: {arb['active_positions']} позиций, "
                    f"собрано ${earned:.2f}"
                )

        # ---------------------------------------------------------------
        # Scalper
        # ---------------------------------------------------------------
        scalper = latest.get("bots", {}).get("scalper", {})
        total_trades = scalper.get("total_trades", 0)
        win_rate = scalper.get("win_rate_pct", 0)

        if total_trades > 10:
            cur_rsi = config.get("scalper", {}).get("rsi_oversold", 35)
            if win_rate < 40:
                hints.append(
                    f"⚠️ Scalper: win rate {win_rate:.0f}% (ниже 40%).\n"
                    f"   Предложение: rsi_oversold {cur_rsi} → {cur_rsi - 3} (строже вход)"
                )
            elif win_rate > 70:
                hints.append(
                    f"✅ Scalper: win rate {win_rate:.0f}% — отлично!\n"
                    f"   Можно rsi_oversold {cur_rsi} → {cur_rsi + 2} для большего числа сделок"
                )
            else:
                hints.append(
                    f"✅ Scalper: win rate {win_rate:.0f}% при {total_trades} сделках"
                )

        # ---------------------------------------------------------------
        # Breakout Bot
        # ---------------------------------------------------------------
        breakout = latest.get("bots", {}).get("breakout", {})
        bt_trades = breakout.get("total_trades", 0)
        bt_win = breakout.get("win_rate_pct", 0)

        if bt_trades >= 5:
            if bt_win < 35:
                hints.append(
                    f"⚠️ Breakout: win rate {bt_win:.0f}% (ниже 35%) при {bt_trades} сделках.\n"
                    f"   Предложение: rr_min 2.5 → 3.0 или volume_multiplier 2.5 → 3.0 "
                    "(строже фильтр)"
                )
            else:
                hints.append(
                    f"✅ Breakout: {bt_trades} сделок, win {bt_win:.0f}%, "
                    f"PnL ${breakout.get('total_pnl_usdt', 0):+.2f}"
                )

        # ---------------------------------------------------------------
        # StatArb
        # ---------------------------------------------------------------
        stat_arb = latest.get("bots", {}).get("stat_arb", {})
        sa_trades = stat_arb.get("total_trades", 0)
        sa_win = stat_arb.get("win_rate_pct", 0)

        if sa_trades >= 3:
            if sa_win < 50:
                hints.append(
                    f"⚠️ StatArb: win rate {sa_win:.0f}% (ниже 50%) при {sa_trades} сделках.\n"
                    f"   Предложение: entry_zscore 2.0 → 2.2 (входить при большем отклонении)"
                )
            else:
                hints.append(
                    f"✅ StatArb: {sa_trades} сделок, win {sa_win:.0f}%, "
                    f"PnL ${stat_arb.get('total_pnl_usdt', 0):+.2f}"
                )

        # ---------------------------------------------------------------
        # Portfolio trend 7d
        # ---------------------------------------------------------------
        if len(history) >= 7:
            pnls = [
                (h.get("portfolio") or {}).get("daily_pnl", 0) or 0
                for h in history[-7:]
            ]
            avg_7d = sum(pnls) / len(pnls)
            if avg_7d < -10:
                hints.append(
                    f"🚨 Портфель: средний дневной убыток за 7 дней = ${avg_7d:.2f}.\n"
                    f"   Рассмотри снижение exposure или временную паузу."
                )
            elif avg_7d > 0:
                hints.append(
                    f"✅ Портфель: средний доход за 7 дней ${avg_7d:+.2f}/день"
                )

        if not hints:
            hints.append("✅ Все стратегии в норме — изменений не требуется")

        return hints
