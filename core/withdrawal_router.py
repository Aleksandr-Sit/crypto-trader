"""
WithdrawalRouter — динамический выбор сети для вывода USDT.

Перед выводом запрашивает у Bybit текущие комиссии и доступность сетей.
Выбирает дешевейшую из доступных сетей, для которых задан адрес в .env.

.env ключи (любой подмножество):
  WITHDRAWAL_ADDRESS_USDT_ARB   — Arbitrum One (USDT0)
  WITHDRAWAL_ADDRESS_USDT_OP    — Optimism
  WITHDRAWAL_ADDRESS_USDT_TRC20 — Tron (TRC20, запасной)
  WITHDRAWAL_ADDRESS_USDT_ERC20 — Ethereum mainnet (самый дорогой)

Bybit chain ID: ARBI / OP / TRC20 / ETH
"""

from dataclasses import dataclass
from typing import Optional
from loguru import logger

from core.security import SecretsManager


# Bybit chain ID → ключ .env
CHAIN_ENV_KEY: dict[str, str] = {
    "ARBI":  "WITHDRAWAL_ADDRESS_USDT_ARB",
    "OP":    "WITHDRAWAL_ADDRESS_USDT_OP",
    "TRC20": "WITHDRAWAL_ADDRESS_USDT_TRC20",
    "ETH":   "WITHDRAWAL_ADDRESS_USDT_ERC20",
}

# Порядок предпочтения при одинаковой комиссии
CHAIN_PRIORITY: list[str] = ["ARBI", "OP", "TRC20", "ETH"]


@dataclass
class NetworkOption:
    chain: str      # Bybit chain ID (ARBI / OP / TRC20 / ETH)
    address: str    # адрес кошелька-получателя
    fee: float      # комиссия вывода в USDT (0.0 если API недоступен)


class WithdrawalRouter:
    def __init__(self, secrets: SecretsManager, bybit_adapter) -> None:
        self._adapter = bybit_adapter
        self._addresses: dict[str, str] = {}
        for chain, env_key in CHAIN_ENV_KEY.items():
            addr = secrets.get(env_key, "")
            if addr:
                self._addresses[chain] = addr

        if self._addresses:
            logger.info(f"WithdrawalRouter: настроены сети {list(self._addresses.keys())}")
        else:
            logger.warning("WithdrawalRouter: ни один адрес вывода не задан в .env")

    async def pick_best(self, coin: str = "USDT") -> Optional[NetworkOption]:
        """
        Запрашивает live-комиссии у Bybit, возвращает дешевейшую доступную сеть
        из тех, для которых задан адрес в .env.
        При недоступности API — fallback на первую настроенную сеть по приоритету.
        """
        if not self._addresses:
            return None

        try:
            live = await self._adapter.get_coin_networks(coin)
        except Exception as e:
            logger.warning(f"WithdrawalRouter: Bybit API недоступен ({e}), fallback")
            return self._fallback()

        options: list[NetworkOption] = []
        for net in live:
            chain = net["chain"]
            if chain not in self._addresses:
                continue
            if not net["withdraw_enabled"]:
                logger.info(f"WithdrawalRouter: {chain} временно недоступен для вывода")
                continue
            options.append(NetworkOption(
                chain=chain,
                address=self._addresses[chain],
                fee=net["fee"],
            ))

        if not options:
            logger.warning("WithdrawalRouter: нет доступных сетей из настроенных, fallback")
            return self._fallback()

        # Сортируем: дешевле = лучше; при равной цене — по CHAIN_PRIORITY
        options.sort(key=lambda o: (
            o.fee,
            CHAIN_PRIORITY.index(o.chain) if o.chain in CHAIN_PRIORITY else 99,
        ))
        best = options[0]
        logger.info(
            f"WithdrawalRouter: выбрана {best.chain} fee=${best.fee:.4f} | "
            f"все варианты: {[(o.chain, f'${o.fee:.4f}') for o in options]}"
        )
        return best

    def _fallback(self) -> Optional[NetworkOption]:
        """Fallback без live-комиссий: первая настроенная сеть по приоритету."""
        for chain in CHAIN_PRIORITY:
            if chain in self._addresses:
                return NetworkOption(chain=chain, address=self._addresses[chain], fee=0.0)
        return None

    def describe(self) -> str:
        """Список настроенных сетей — для Telegram-уведомлений."""
        if not self._addresses:
            return "Нет настроенных адресов вывода."
        configured = [c for c in CHAIN_PRIORITY if c in self._addresses]
        return "Настроены: " + ", ".join(configured)
