"""
Единственное место в проекте, где читаются секреты.
Все остальные модули получают ключи только через этот класс.

Правила безопасности:
- Ключи никогда не логируются целиком
- Ключи никогда не передаются в Telegram-сообщениях
- API-ключи бирж создаются БЕЗ права Withdraw
- Право Withdraw только у whitelist-адресов на самой бирже
"""

import os
import sys
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv
from loguru import logger


class SecretsManager:
    REQUIRED_KEYS = [
        "BYBIT_API_KEY",
        "BYBIT_API_SECRET",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "WITHDRAWAL_ADDRESS_USDT_TRC20",
        "REDIS_URL",
        "TRADING_MODE",
    ]

    OPTIONAL_KEYS = [
        "MEXC_API_KEY",
        "MEXC_API_SECRET",
        "OKX_API_KEY",
        "OKX_API_SECRET",
        "OKX_PASSPHRASE",
        "WITHDRAWAL_ADDRESS_USDT_ERC20",
        "WITHDRAWAL_ADDRESS_USDT_ARB",
        "WITHDRAWAL_ADDRESS_USDT_OP",
        "WITHDRAWAL_ADDRESS_BTC",
        "CRYPTOPANIC_API_KEY",
        "BYBIT_TESTNET",
    ]

    def __init__(self, env_path: Optional[Path] = None):
        env_file = env_path or Path(__file__).parent.parent / ".env"
        if env_file.exists():
            load_dotenv(env_file)
        else:
            logger.warning(f".env not found at {env_file}, reading from environment")

        self._store: dict[str, str] = {}
        self._validate_and_load()

    def _validate_and_load(self) -> None:
        missing = []
        for key in self.REQUIRED_KEYS:
            value = os.getenv(key, "").strip()
            if not value:
                missing.append(key)
            else:
                self._store[key] = value

        if missing:
            logger.error(f"Missing required secrets: {missing}")
            logger.error("Copy .env.example to .env and fill in all values")
            sys.exit(1)

        for key in self.OPTIONAL_KEYS:
            value = os.getenv(key, "").strip()
            if value:
                self._store[key] = value

        mode = self._store.get("TRADING_MODE", "paper")
        logger.info(f"Secrets loaded. Mode: {mode}. Keys: {self._log_summary()}")

    def get(self, key: str, default: str = "") -> str:
        return self._store.get(key, default)

    def require(self, key: str) -> str:
        """Получить ключ или упасть с понятной ошибкой."""
        value = self._store.get(key)
        if not value:
            raise RuntimeError(
                f"Secret '{key}' is required but not set. Check your .env file."
            )
        return value

    def is_testnet(self) -> bool:
        return self._store.get("BYBIT_TESTNET", "false").lower() == "true"

    def is_paper_mode(self) -> bool:
        return self._store.get("TRADING_MODE", "paper").lower() == "paper"

    def has_mexc(self) -> bool:
        return bool(self._store.get("MEXC_API_KEY"))

    def has_okx(self) -> bool:
        return bool(self._store.get("OKX_API_KEY"))

    def _log_summary(self) -> str:
        """Возвращает список ключей с маскированными значениями для логов."""
        parts = []
        for key in self._store:
            parts.append(f"{key}={self.mask(self._store[key])}")
        return ", ".join(parts)

    @staticmethod
    def mask(value: str) -> str:
        """Маскирует секрет для безопасного логирования."""
        if not value:
            return "[empty]"
        if len(value) <= 8:
            return "***"
        return value[:4] + "****" + value[-3:]

    def safe_dict(self) -> dict[str, str]:
        """Словарь с маскированными значениями — только для логов и Telegram."""
        return {k: self.mask(v) for k, v in self._store.items()}


_instance: Optional[SecretsManager] = None


def get_secrets() -> SecretsManager:
    """Синглтон — инициализируется один раз при старте."""
    global _instance
    if _instance is None:
        _instance = SecretsManager()
    return _instance
