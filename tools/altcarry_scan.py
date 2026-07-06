"""altcarry_scan.py — стадия 1 валидации Alt-Funding Carry.

Методология и пороги PASS: 10_TARGET_STRATEGY_SELECTION.md (зафиксированы до данных).

Что делает:
  1. Тянет историю funding ВСЕХ текущих linear-USDT перпов Bybit за окно
     2024-01-01 → сегодня (public API, ключи не нужны).
  2. Детектит эпизоды по правилам: вход r8>=+0.05%/8h (после триггер-периода),
     выход r8<+0.01%/8h; ставки нормализуются к 8h (fundingInterval бывает 1-4h).
  3. Играбельность: спот существовал на дату входа (проверка дневной свечой)
     и текущий оборот спота >= $1M/сутки.
  4. Экономика: net = sum(rates) - RT-издержки {0.31%, 0.5%, 0.8%};
     RoC = net * L/(L+1); гриди-симуляция конфига 2 слота x $250.
  5. Сводка против порогов PASS. Артефакты в data/research/altcarry/.

Restart-safe: по-символьный чекпоинт (symbols.jsonl); повторный запуск
докачивает только несделанное. Вывод ASCII (Windows cp1251-safe).

Запуск:  py -3 tools/altcarry_scan.py            # скан + анализ
         py -3 tools/altcarry_scan.py --analyze  # только анализ по чекпоинту
"""

import argparse
import json
import os
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

BASE = "https://api.bybit.com"
START_MS = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
ENTRY_R8 = 0.0005   # +0.05%/8h
EXIT_R8 = 0.0001    # +0.01%/8h
COSTS_RT = (0.0031, 0.0050, 0.0080)   # base = 0.5%
BASE_COST = 0.0050
SPOT_TURNOVER_MIN = 1_000_000
SLOTS = 2
SLOT_CAPITAL = 250.0

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "data", "research", "altcarry")
SYMBOLS_JSONL = os.path.join(OUT_DIR, "symbols.jsonl")
SUMMARY_JSON = os.path.join(OUT_DIR, "summary.json")

_write_lock = threading.Lock()


def get(path: str, retries: int = 4, **params) -> dict:
    q = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{BASE}{path}?{q}"
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "altcarry-scan/1.0"})
            with urllib.request.urlopen(req, timeout=25) as r:
                data = json.loads(r.read().decode())
            if data.get("retCode") != 0:
                raise RuntimeError(f"retCode={data.get('retCode')} {data.get('retMsg')}")
            time.sleep(0.02)
            return data["result"]
        except Exception as e:  # noqa: BLE001 - сетевой ретрай
            last = e
            time.sleep(1.0 + 2.0 * attempt)
    raise RuntimeError(f"GET {url} failed after {retries} tries: {last}")


def list_linear_usdt() -> list[dict]:
    out, cursor = [], ""
    while True:
        params = {"category": "linear", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        res = get("/v5/market/instruments-info", **params)
        for it in res["list"]:
            if (it.get("quoteCoin") == "USDT"
                    and it.get("contractType") == "LinearPerpetual"
                    and it.get("status") == "Trading"):
                out.append({
                    "symbol": it["symbol"],
                    "launch_ms": int(it.get("launchTime") or 0),
                    "funding_interval_min": int(it.get("fundingInterval") or 480),
                })
        cursor = res.get("nextPageCursor") or ""
        if not cursor:
            return out


def spot_universe() -> dict[str, float]:
    ticks = get("/v5/market/tickers", category="spot")["list"]
    return {t["symbol"]: float(t["turnover24h"] or 0) for t in ticks}


def fetch_funding(symbol: str, start_ms: int) -> list[tuple[int, float]]:
    """Полная история funding символа с start_ms, по возрастанию времени."""
    rows: list[tuple[int, float]] = []
    end = int(time.time() * 1000)
    while True:
        res = get("/v5/market/funding/history", category="linear",
                  symbol=symbol, endTime=end, limit=200)
        batch = [(int(x["fundingRateTimestamp"]), float(x["fundingRate"]))
                 for x in res["list"]]
        if not batch:
            break
        rows.extend(batch)
        oldest = min(ts for ts, _ in batch)
        if oldest <= start_ms or len(batch) < 200:
            break
        end = oldest - 1
    rows = sorted({ts: r for ts, r in rows}.items())
    return [(ts, r) for ts, r in rows if ts >= start_ms]


def detect_episodes(rows: list[tuple[int, float]], interval_min: int) -> list[dict]:
    """Вход ПОСЛЕ триггер-периода (его ставка не в carry), выход при r8<EXIT."""
    k8 = 480.0 / max(interval_min, 1)
    eps: list[dict] = []
    cur: dict | None = None
    for ts, rate in rows:
        r8 = rate * k8
        if cur is None:
            if r8 >= ENTRY_R8:
                cur = {"t_start": ts, "rates": [], "max_r8": r8}
        else:
            cur["rates"].append(rate)
            cur["max_r8"] = max(cur["max_r8"], r8)
            if r8 < EXIT_R8:
                cur["t_end"] = ts
                cur["open"] = False
                eps.append(cur)
                cur = None
    if cur is not None:
        cur["t_end"] = rows[-1][0]
        cur["open"] = True
        eps.append(cur)
    out = []
    for e in eps:
        if not e["rates"]:
            continue
        out.append({
            "t_start": e["t_start"],
            "t_end": e["t_end"],
            "open": e["open"],
            "n_periods": len(e["rates"]),
            "n_neg": sum(1 for r in e["rates"] if r < 0),
            "sum_rate": sum(e["rates"]),
            "max_r8": e["max_r8"],
            "duration_days": (e["t_end"] - e["t_start"]) / 86_400_000,
        })
    return out


class SpotListedCache:
    """Спот существовал к моменту ts? (дневная свеча <= ts). Кэш монотонности."""

    def __init__(self) -> None:
        self._true_from: dict[str, int] = {}
        self._false_upto: dict[str, int] = {}
        self._lock = threading.Lock()

    def listed_by(self, symbol: str, ts: int) -> bool:
        with self._lock:
            t = self._true_from.get(symbol)
            if t is not None and ts >= t:
                return True
            f = self._false_upto.get(symbol)
            if f is not None and ts <= f:
                return False
        res = get("/v5/market/kline", category="spot", symbol=symbol,
                  interval="D", end=ts, limit=1)
        ok = bool(res.get("list"))
        with self._lock:
            if ok:
                self._true_from[symbol] = min(self._true_from.get(symbol, ts), ts)
            else:
                self._false_upto[symbol] = max(self._false_upto.get(symbol, ts), ts)
        return ok


def scan(instruments: list[dict]) -> None:
    done: set[str] = set()
    if os.path.exists(SYMBOLS_JSONL):
        with open(SYMBOLS_JSONL, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "error" not in rec:
                    done.add(rec["symbol"])
    todo = [i for i in instruments if i["symbol"] not in done]
    print(f"scan: {len(instruments)} linear USDT perps, done={len(done)}, todo={len(todo)}")

    def work(inst: dict) -> dict:
        start = max(START_MS, inst["launch_ms"] or START_MS)
        rows = fetch_funding(inst["symbol"], start)
        eps = detect_episodes(rows, inst["funding_interval_min"])
        return {"symbol": inst["symbol"],
                "funding_interval_min": inst["funding_interval_min"],
                "launch_ms": inst["launch_ms"],
                "n_records": len(rows),
                "episodes": eps}

    n_ok = n_err = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(work, i): i for i in todo}
        for fut in as_completed(futs):
            inst = futs[fut]
            try:
                rec = fut.result()
                n_ok += 1
            except Exception as e:  # noqa: BLE001
                rec = {"symbol": inst["symbol"], "error": str(e)}
                n_err += 1
            with _write_lock:
                with open(SYMBOLS_JSONL, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec) + "\n")
            total = n_ok + n_err
            if total % 25 == 0:
                print(f"  progress: {total}/{len(todo)} (errors={n_err})")
    print(f"scan finished: ok={n_ok}, errors={n_err}")


def month_key(ms: int) -> str:
    d = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return f"{d.year:04d}-{d.month:02d}"


def analyze() -> None:
    spot = spot_universe()
    cache = SpotListedCache()

    per_symbol: dict[str, dict] = {}
    with open(SYMBOLS_JSONL, encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "error" not in rec:
                per_symbol[rec["symbol"]] = rec   # последняя запись выигрывает

    episodes = []
    for sym, rec in per_symbol.items():
        for e in rec["episodes"]:
            e2 = dict(e)
            e2["symbol"] = sym
            episodes.append(e2)
    episodes.sort(key=lambda e: e["t_start"])
    print(f"analyze: symbols={len(per_symbol)}, episodes total={len(episodes)}")

    # играбельность
    n_spot_now = n_liquid = 0
    for e in episodes:
        sym = e["symbol"]
        has_spot = sym in spot
        liquid = has_spot and spot[sym] >= SPOT_TURNOVER_MIN
        listed = liquid and cache.listed_by(sym, e["t_start"])
        e["spot_now"] = has_spot
        e["playable"] = liquid and listed
        n_spot_now += has_spot
        n_liquid += liquid
    play = [e for e in episodes if e["playable"]]
    print(f"  spot exists now: {n_spot_now}; spot turnover>=$1M: {n_liquid}; "
          f"playable (incl. listed-by-entry): {len(play)}")

    for e in episodes:
        for c in COSTS_RT:
            e[f"net_{c:.4f}"] = e["sum_rate"] - c
    key = f"net_{BASE_COST:.4f}"

    months_window = []
    d = datetime(2024, 1, 1, tzinfo=timezone.utc)
    now = datetime.now(tz=timezone.utc)
    while d <= now:
        months_window.append(f"{d.year:04d}-{d.month:02d}")
        d = datetime(d.year + (d.month == 12), d.month % 12 + 1, 1, tzinfo=timezone.utc)
    months_with = {m for e in play for m in (month_key(e["t_start"]), month_key(e["t_end"]))}
    coverage = len(months_with & set(months_window)) / len(months_window)

    nets = sorted(e[key] for e in play)
    med = nets[len(nets) // 2] if nets else float("nan")
    mean = sum(nets) / len(nets) if nets else float("nan")
    neg_share = sum(1 for n in nets if n < 0) / len(nets) if nets else float("nan")

    # greedy 2 слота x $250, L=1 (нотионал = капитал/2)
    slot_free = [0] * SLOTS
    captured, missed = [], 0
    for e in play:
        idx = min(range(SLOTS), key=lambda i: slot_free[i])
        if slot_free[idx] <= e["t_start"]:
            slot_free[idx] = e["t_end"]
            captured.append(e)
        else:
            missed += 1
    total_capital = SLOTS * SLOT_CAPITAL
    months_n = len(months_window)
    res_ann = {}
    for L, factor in ((1, 0.5), (2, 2 / 3)):
        usd = sum(e[key] * factor * SLOT_CAPITAL for e in captured)
        res_ann[L] = usd / total_capital / months_n * 12 * 100
    busy_days = sum(e["duration_days"] for e in captured)
    util = busy_days / (SLOTS * months_n * 30.44)

    per_year: dict[str, int] = {}
    for e in play:
        y = month_key(e["t_start"])[:4]
        per_year[y] = per_year.get(y, 0) + 1

    print()
    print("=" * 74)
    print(f"PLAYABLE EPISODES: {len(play)} over {months_n} months "
          f"({len(play)/months_n:.2f}/mo)  by year: {per_year}")
    print(f"month coverage: {coverage*100:.0f}%   open episodes now: "
          f"{sum(1 for e in play if e['open'])}")
    print(f"net per episode @cost0.5%: median={med*100:+.2f}%  mean={mean*100:+.2f}%  "
          f"share<0: {neg_share*100:.0f}%")
    for c in COSTS_RT:
        k = f"net_{c:.4f}"
        s = sorted(e[k] for e in play)
        if s:
            print(f"  sensitivity cost={c*100:.2f}%: median={s[len(s)//2]*100:+.2f}%  "
                  f"mean={(sum(s)/len(s))*100:+.2f}%")
    print(f"2x$250 greedy: captured={len(captured)} missed={missed} "
          f"utilization={util*100:.0f}%")
    print(f"RoC on $500: L=1 -> {res_ann[1]:+.1f}%/yr ({res_ann[1]/12:+.2f}%/mo)   "
          f"L=2 -> {res_ann[2]:+.1f}%/yr ({res_ann[2]/12:+.2f}%/mo)")
    print()
    print("PASS gates (10_TARGET_STRATEGY_SELECTION):")
    g1 = len(play) >= 30 and coverage >= 0.5
    g2 = (nets and med > 0 and mean >= 0.015)
    g3 = res_ann[1] >= 24.0
    g3s = res_ann[1] >= 36.0
    g4 = (nets and neg_share <= 0.40)
    print(f"  1 frequency  (>=30 eps & >=50% months): {'PASS' if g1 else 'FAIL'}")
    print(f"  2 economics  (median>0 & mean>=+1.5%):  {'PASS' if g2 else 'FAIL'}")
    print(f"  3 target     (>=24%/yr on 2x$250 L=1):  "
          f"{'STRONG PASS' if g3s else ('PASS' if g3 else 'FAIL')}")
    print(f"  4 tail share (net<0 <= 40%):            {'PASS' if g4 else 'FAIL'}")

    top = sorted(play, key=lambda e: -e[key])[:12]
    print()
    print("top playable episodes (net @0.5%):")
    for e in top:
        print(f"  {e['symbol']:<16} {datetime.fromtimestamp(e['t_start']/1000, tz=timezone.utc):%Y-%m-%d} "
              f"{e['duration_days']:5.1f}d  periods={e['n_periods']:<4d} "
              f"net={e[key]*100:+.2f}%  max_r8={e['max_r8']*100:.3f}%")

    with open(SUMMARY_JSON, "w", encoding="utf-8") as f:
        json.dump({
            "generated_utc": datetime.now(tz=timezone.utc).isoformat(),
            "window_months": months_n,
            "episodes_total": len(episodes),
            "episodes_playable": len(play),
            "coverage_months": coverage,
            "median_net_base": med, "mean_net_base": mean,
            "neg_share": neg_share,
            "roc_annual_pct": res_ann, "utilization": util,
            "per_year": per_year,
            "gates": {"g1": g1, "g2": g2, "g3": g3, "g3_strong": g3s, "g4": g4},
            "episodes": episodes,
        }, f, indent=1)
    print(f"\nsummary saved: {SUMMARY_JSON}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--analyze", action="store_true",
                    help="skip scan, analyze existing checkpoint")
    args = ap.parse_args()
    os.makedirs(OUT_DIR, exist_ok=True)
    if not args.analyze:
        scan(list_linear_usdt())
    analyze()


if __name__ == "__main__":
    main()
