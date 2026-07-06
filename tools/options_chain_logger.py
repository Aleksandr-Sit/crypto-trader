"""options_chain_logger.py — форвард-запись цепочки Bybit BTC-опционов.

Назначение: стадия 2 трека D (14_COVERED_CALL_VALIDATION §Гейт, п.1) —
ежедневный снапшот реальных котировок для сверки модельных премий (BS×RV30×κ)
с рыночными: дрейф κ, skew, спреды. Через 4–6 недель данных — калибровка.

Деплой: /opt/research/options_chain_logger.py на vps-trader, cron:
  15 8 * * * python3 /opt/research/options_chain_logger.py >> /opt/research/options_chain/logger.log 2>&1
(08:15 UTC — сразу после дневной экспирации 08:00; python3 хоста, stdlib only).

Выход: /opt/research/options_chain/YYYY-MM-DD.json.gz (~20KB/день):
все BTC-C/P с dte<=60 и живым рынком + underlying, markIv, греки, OI.
Идемпотентен: повторный запуск в тот же день перезаписывает файл.
"""

import gzip
import json
import os
import urllib.request
from datetime import datetime, timezone

BASE = "https://api.bybit.com"
OUT_DIR = os.environ.get("CHAIN_OUT_DIR", "/opt/research/options_chain")
MAX_DTE = 60
MONTH_MAP = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}
KEEP_FIELDS = [
    "symbol", "bid1Price", "bid1Size", "ask1Price", "ask1Size",
    "markPrice", "markIv", "underlyingPrice", "openInterest",
    "volume24h", "turnover24h", "delta", "gamma", "vega", "theta",
]


def get(path: str, **params) -> dict:
    q = "&".join(f"{k}={v}" for k, v in params.items())
    req = urllib.request.Request(f"{BASE}{path}?{q}",
                                 headers={"User-Agent": "chain-logger/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode())
    if data.get("retCode") != 0:
        raise RuntimeError(f"{data.get('retCode')}: {data.get('retMsg')}")
    return data["result"]


def dte_of(symbol: str, now: datetime) -> int:
    token = symbol.split("-")[1]           # BTC-31JUL26-70000-C-USDT
    day = int(token[:-5])
    mon = MONTH_MAP[token[-5:-2]]
    year = 2000 + int(token[-2:])
    exp = datetime(year, mon, day, 8, 0, tzinfo=timezone.utc)
    return (exp - now).days


def main() -> None:
    now = datetime.now(tz=timezone.utc)
    rows = get("/v5/market/tickers", category="option", baseCoin="BTC")["list"]
    keep = []
    for it in rows:
        try:
            dte = dte_of(it["symbol"], now)
        except Exception:
            continue
        if not 0 <= dte <= MAX_DTE:
            continue
        rec = {f: it.get(f, "") for f in KEEP_FIELDS}
        rec["dte"] = dte
        keep.append(rec)
    # спот отдельной строкой (для RV-расчётов при сверке)
    spot = get("/v5/market/tickers", category="spot", symbol="BTCUSDT")["list"][0]
    snapshot = {
        "ts_utc": now.isoformat(),
        "spot_last": spot["lastPrice"],
        "n_instruments": len(keep),
        "chain": keep,
    }
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, now.strftime("%Y-%m-%d") + ".json.gz")
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(snapshot, f)
    print(f"{now.isoformat()} chain snapshot: {len(keep)} instruments -> {path}")


if __name__ == "__main__":
    main()
