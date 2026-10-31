#!/usr/bin/env python3
"""Hyperliquid perp scanner (trader-hl). Read-only: public info API, no key, no orders.

    python3 trader-hl/hl.py scan [--interval 1h|4h|1d] [--min-volume 5e6] [--limit N]

Scans liquid perpetual markets for setups on completed candles (the still
forming candle is dropped) and writes trader/hl_watchlist.json.
Crypto trades 24/7, so there is no opening range and no close: setups use
rolling windows instead.
"""
import argparse
import json
import os
import time
import urllib.request

URL = "https://api.hyperliquid.xyz/info"
MS = {"15m": 900000, "1h": 3600000, "4h": 14400000, "1d": 86400000}


def post(body):
    req = urllib.request.Request(URL, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def candles(coin, interval, n=60):
    now = int(time.time() * 1000)
    rows = post({"type": "candleSnapshot", "req": {"coin": coin, "interval": interval,
                                                    "startTime": now - (n + 2) * MS[interval], "endTime": now}})
    rows = [r for r in rows if r["T"] < now]  # completed candles only
    return [(float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"]), float(r["v"])) for r in rows]


def rsi(closes, n=14):
    if len(closes) <= n:
        return None
    g = l = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        g += max(d, 0)
        l += max(-d, 0)
    g, l = g / n, l / n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        g = (g * (n - 1) + max(d, 0)) / n
        l = (l * (n - 1) + max(-d, 0)) / n
    return 100.0 if l == 0 else 100 - 100 / (1 + g / l)


def setups(cs):
    c = [x[3] for x in cs]
    out = []
    if len(c) < 30:
        return out, None
    r = rsi(c)
    prior_high = max(x[1] for x in cs[-21:-1])
    sma = sum(c[-20:]) / 20
    prev_sma = sum(c[-21:-1]) / 20
    if c[-1] > prior_high:
        out.append("breakout_20")
    if c[-2] <= prev_sma < c[-1]:
        out.append("reclaim_20")
    if r is not None and r < 30:
        out.append("oversold")
    if r is not None and r > 70:
        out.append("overbought")
    if len(c) > 6 and min(c[-6:-1]) == min(c[-15:-1]) and r is not None and r < 40 and c[-1] > c[-2]:
        out.append("oversold_bounce")
    # flag: strong pole over the last 12 bars then a tight, shallow pullback
    pole = c[-6] / c[-12] - 1
    rng = (max(x[1] for x in cs[-5:]) - min(x[2] for x in cs[-5:])) / c[-1]
    if pole > 0.05 and rng < 0.03 and c[-1] >= 0.97 * max(c[-5:]):
        out.append("bull_flag")
    return out, r


def scan(a):
    meta, ctxs = post({"type": "metaAndAssetCtxs"})
    rows = []
    for m, x in zip(meta["universe"], ctxs):
        if m.get("isDelisted"):
            continue
        vol = float(x["dayNtlVlm"])
        if vol >= a.min_volume:
            px, prev = float(x["markPx"]), float(x["prevDayPx"])
            rows.append({"symbol": m["name"], "price": px, "chg_24h": px / prev - 1 if prev else None,
                         "volume_24h": vol, "funding_hr": float(x["funding"]), "open_interest_usd": float(x["openInterest"]) * px,
                         "max_leverage": m["maxLeverage"]})
    rows.sort(key=lambda r: -r["volume_24h"])
    if a.limit:
        rows = rows[:a.limit]
    print("%d of %d perps pass the $%s 24h volume filter; fetching %s candles..." % (
        len(rows), len(meta["universe"]), format(int(a.min_volume), ","), a.interval))
    hits, failed = [], 0
    for r in rows:
        try:
            s, rs = setups(candles(r["symbol"], a.interval))
        except Exception:  # noqa: BLE001
            failed += 1
            continue
        r["rsi"] = round(rs, 1) if rs is not None else None
        r["setups"] = s
        if s:
            hits.append(r)
        time.sleep(a.pause)
    doc = {"created_by": "trader-hl", "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "mode": "hyperliquid " + a.interval, "as_of": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
           "universe": len(meta["universe"]), "scanned": len(rows) - failed, "failed": failed, "source": "hyperliquid",
           "note": "A scan is a list of charts that match a rule, not a list of trades. Perps are leveraged: "
                   "check funding and your liquidation price before anything else.",
           "hits": hits}
    os.makedirs("trader", exist_ok=True)
    with open("trader/hl_watchlist.json", "w") as fh:
        json.dump(doc, fh, indent=1)
    print("%d hits. Wrote trader/hl_watchlist.json" % len(hits))
    for h in hits[:15]:
        print("  %-8s %12.4f  RSI %5s  fund %+.4f%%/h  %s" % (h["symbol"], h["price"], h["rsi"], h["funding_hr"] * 100, ", ".join(h["setups"])))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("scan")
    p.add_argument("--interval", default="1h", choices=list(MS))
    p.add_argument("--min-volume", type=float, default=1e6)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--pause", type=float, default=0.15)
    scan(ap.parse_args())


if __name__ == "__main__":
    main()
