"""Rules-based ranking of liquid Hyperliquid perps: which coin comes closest to a clean setup right now.
Read-only. A checklist score, not advice."""
import os
import sys
import time
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "trader-hl"))
import hl  # noqa: E402
import signals as S  # noqa: E402
from btc import ema  # noqa: E402

_cache = {"t": 0, "v": None}
CHECKS = ["4h trend agrees", "1h trend agrees", "15m trend agrees", "15m breakout on volume", "fees under 25% of reward",
          "history net positive (5+ fires)", "funding and liquidation OK"]


def trend(cs):
    c = [x[3] for x in cs]
    e20, e50 = ema(c, 20), ema(c, 50)
    sc = (c[-1] > e20[-1]) + (c[-1] > e50[-1]) + (e20[-1] > e50[-1])
    return "long" if sc == 3 else "short" if sc == 0 else None


def evaluate(m, x, args):
    c15, c1h, c4h = hl.candles(m["name"], "15m", 800), hl.candles(m["name"], "1h", 120), hl.candles(m["name"], "4h", 120)
    if min(len(c15), len(c1h), len(c4h)) < 60:
        return None
    best = None
    fund = float(x["funding"])
    for side in ("long", "short"):
        p = S.plan(None, c15, side, S.atr(c15, len(c15) - 1), args, m["maxLeverage"])
        h = S.history(c15, side)
        fee_ok = S.FEE_RT <= 0.25 * S.TARGET_R * p["stop_pct"]
        fund_ok = (fund <= 0.00005 if side == "long" else fund >= -0.00005) and p["stop_before_liquidation"]
        res = [trend(c4h) == side, trend(c1h) == side, trend(c15) == side, S.signal_at(c15, len(c15) - 1) == side, fee_ok,
               h["fires"] >= 5 and (h["avg_R"] or 0) > 0, fund_ok]
        cand = {"symbol": m["name"], "side": side, "score": sum(res), "checks": res, "plan": p, "history": h,
                "price": float(x["markPx"]),
                "trigger": min(z[2] for z in c15[-20:]) if side == "short" else max(z[1] for z in c15[-20:]), "volume_24h": float(x["dayNtlVlm"]), "funding_hr": fund}
        if best is None or cand["score"] > best["score"]:
            best = cand
    return best


def ranking(limit=25):
    if time.time() - _cache["t"] < 60 and _cache["v"]:
        return _cache["v"]
    meta, ctxs = hl.post({"type": "metaAndAssetCtxs"})
    rows = [(m, x) for m, x in zip(meta["universe"], ctxs) if not m.get("isDelisted")]
    rows.sort(key=lambda r: -float(r[1]["dayNtlVlm"]))
    args = types.SimpleNamespace(size=100, max_loss=30, leverage=1)
    out = []
    for m, x in rows[:limit]:
        try:
            r = evaluate(m, x, args)
        except Exception:  # noqa: BLE001
            continue
        if r:
            out.append(r)
        time.sleep(0.2)
    out.sort(key=lambda r: (-r["score"], -r["volume_24h"]))
    top = out[0]["score"] if out else 0
    call = ("SETUP" if out and out[0]["checks"][3] else "ARMED") if top >= 6 else "WATCH" if top >= 4 else "NO TRADE"
    v = {"as_of": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()), "checks": CHECKS, "call": call, "top_score": top,
         "scanned": len(out), "candidates": out[:6]}
    _cache.update(t=time.time(), v=v)
    return v
