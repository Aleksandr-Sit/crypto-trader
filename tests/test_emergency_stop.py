"""
Тесты EmergencyStop: drawdown guard, price crash, resume.
"""
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

from core.emergency_stop import EmergencyStop, StopReason, SystemState


def _make_em() -> EmergencyStop:
    mgr = MagicMock()
    mgr.cancel_all_everywhere = AsyncMock(return_value=0)
    mgr.close_all_everywhere = AsyncMock(return_value=0)
    mgr.get_total_balance_usdt = AsyncMock(return_value=1000.0)
    em = EmergencyStop(mgr)
    em._notify = AsyncMock()
    return em


class TestDrawdownGuard:
    def test_triggers_above_threshold(self):
        async def _run():
            em = _make_em()
            await em.on_balance_update(1000.0)
            assert em.is_running()
            await em.on_balance_update(860.0)   # -14% > 12% threshold
            assert not em.is_running()
            assert em._stop_reason == StopReason.DRAWDOWN

        asyncio.run(_run())

    def test_no_trigger_below_threshold(self):
        async def _run():
            em = _make_em()
            await em.on_balance_update(1000.0)
            await em.on_balance_update(900.0)   # -10% < 12% threshold
            assert em.is_running()

        asyncio.run(_run())

    def test_peak_tracks_new_high(self):
        async def _run():
            em = _make_em()
            await em.on_balance_update(1000.0)
            await em.on_balance_update(1200.0)  # новый пик
            await em.on_balance_update(1100.0)  # -8.3% от 1200 < 12%
            assert em.is_running()
            await em.on_balance_update(1020.0)  # -15% от 1200 > 12%
            assert not em.is_running()

        asyncio.run(_run())

    def test_no_trigger_before_peak_set(self):
        async def _run():
            em = _make_em()
            # Первый вызов устанавливает пик = 0 → просадка не считается
            await em.on_balance_update(0.0)
            assert em.is_running()

        asyncio.run(_run())


class TestPriceCrashDetector:
    def test_triggers_on_crash(self):
        async def _run():
            em = _make_em()
            em.CRASH_DROP_PCT = 5.0
            now = time.time()
            em._price_history = [(now - 200, 60000.0)]
            await em.on_price_tick("BTCUSDT", 56400.0)  # -6% > 5%
            assert not em.is_running()
            assert em._stop_reason == StopReason.PRICE_CRASH

        asyncio.run(_run())

    def test_no_trigger_on_small_drop(self):
        async def _run():
            em = _make_em()
            em.CRASH_DROP_PCT = 5.0
            now = time.time()
            em._price_history = [(now - 200, 60000.0)]
            await em.on_price_tick("BTCUSDT", 57600.0)  # -4% < 5%
            assert em.is_running()

        asyncio.run(_run())

    def test_old_prices_pruned(self):
        async def _run():
            em = _make_em()
            em.CRASH_WINDOW_SEC = 300
            now = time.time()
            # Старая цена вне окна — не должна влиять
            em._price_history = [(now - 400, 60000.0)]
            await em.on_price_tick("BTCUSDT", 50000.0)
            assert em.is_running()

        asyncio.run(_run())


class TestResume:
    def test_resume_restores_running(self):
        async def _run():
            em = _make_em()
            await em.trigger(StopReason.MANUAL, "test")
            assert not em.is_running()
            await em.resume()
            assert em.is_running()
            assert em.state == SystemState.RUNNING

        asyncio.run(_run())

    def test_double_trigger_idempotent(self):
        async def _run():
            em = _make_em()
            await em.trigger(StopReason.MANUAL, "first")
            await em.trigger(StopReason.DRAWDOWN, "second")  # force=False → игнорируется
            assert em._stop_reason == StopReason.MANUAL  # первый сохранён

        asyncio.run(_run())
