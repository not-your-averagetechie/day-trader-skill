#!/usr/bin/env python3
"""Hyperliquid 5m / 15m long and short signals with a trade plan (trader-hl). Read-only: no key, no orders.

    python3 trader-hl/signals.py [--intervals 5m,15m] [--size 100] [--max-loss 30] [--leverage 1] [--loop 60]

Signal: the last COMPLETED candle closes beyond the prior 20-candle high (long) or low (short)
on at least 1.5x average volume. Plan: stop 1.5 ATR(14) away, target 1.5R, a time stop after
48 candles. Odds: 10,000 bootstrap replays from the coin's own recent candles plus the same
rule re-run on those candles, both net of taker fees. Writes trader/hl_signals.json.
"""
import argparse
import json
import os
import random
import time

import hl

LOOKBACK, VOL_X, ATR_N, STOP_ATR, TARGET_R, HORIZON = 20, 1.5, 14, 1.5, 1.5, 48
FEE_RT = 0.0009  # taker 0.045 % each way, round trip; check your own fee tier


def atr(cs, i, n=ATR_N):
    trs = [max(cs[k][1] - cs[k][2], abs(cs[k][1] - cs[k - 1][3]), abs(cs[k][2] - cs[k - 1][3])) for k in range(i - n + 1, i + 1)]
    return sum(trs) / n


def signal_at(cs, i):
    """'long', 'short' or None for the candle at index i (needs LOOKBACK+1 candles before it)."""
    if i < LOOKBACK + 1:
        return None
    win = cs[i - LOOKBACK:i]
    vavg = sum(x[4] for x in win) / LOOKBACK
    if vavg <= 0 or cs[i][4] < VOL_X * vavg:
        return None
    if cs[i][3] > max(x[1] for x in win):
        return "long"
    if cs[i][3] < min(x[2] for x in win):
        return "short"
    return None


def levels(entry, side, a):
    risk = STOP_ATR * a
    s = 1 if side == "long" else -1
    return entry - s * risk, entry + s * TARGET_R * risk, risk


def walk(bars, entry, side, stop, target, risk):
    """Result in R for bars after the entry. Stop wins when a candle touches both."""
    s = 1 if side == "long" else -1
    for n, (o, h, l, c, v) in enumerate(bars[:HORIZON]):
        if (l <= stop) if s == 1 else (h >= stop):
            return -1.0, "stop"
        if (h >= target) if s == 1 else (l <= target):
            return TARGET_R, "target"
    last = bars[min(len(bars), HORIZON) - 1][3]
    return s * (last - entry) / risk, "time"


def history(cs, side):
    res, i = [], LOOKBACK + ATR_N + 1
    while i < len(cs) - 2:
        if signal_at(cs, i) == side:
            entry = cs[i][3]
            stop, target, risk = levels(entry, side, atr(cs, i))
            r, how = walk(cs[i + 1:], entry, side, stop, target, risk)
            res.append((r - FEE_RT * entry / risk, how))
            i += min(HORIZON, len(cs) - i - 2)
        else:
            i += 1
    n = len(res)
    return {"fires": n, "target": sum(1 for _, h in res if h == "target"), "stop": sum(1 for _, h in res if h == "stop"),
            "time": sum(1 for _, h in res if h == "time"), "avg_R": round(sum(r for r, _ in res) / n, 3) if n else None}


def replay(cs, side, entry, stop, target, risk, n=10000, seed=7):
    rnd = random.Random(seed)
    shapes = [(cs[k][1] / cs[k - 1][3], cs[k][2] / cs[k - 1][3], cs[k][3] / cs[k - 1][3]) for k in range(1, len(cs))]
    s = 1 if side == "long" else -1
    t = st = 0
    for _ in range(n):
        p = entry
        for _ in range(HORIZON):
            hr, lr, cr = rnd.choice(shapes)
            h, l = p * hr, p * lr
            if (l <= stop) if s == 1 else (h >= stop):
                st += 1
                break
            if (h >= target) if s == 1 else (l <= target):
                t += 1
                break
            p *= cr
    return {"replays": n, "p_target": round(t / n, 3), "p_stop": round(st / n, 3), "p_neither": round(1 - (t + st) / n, 3)}


def plan(sym_row, cs, side, a, args, max_lev):
    entry = cs[-1][3]
    stop, target, risk = levels(entry, side, a)
    dist = risk
    qty = min(args.max_loss / dist, args.size * args.leverage / entry)
    notional = qty * entry
    mm = 1 / (2 * max_lev)
    liq = None
    if args.leverage > 1 or side == "short":
        liq = entry * (1 - 1 / args.leverage + mm) if side == "long" else entry * (1 + 1 / args.leverage - mm)
        if side == "long" and liq <= 0:
            liq = None
    return {"entry": entry, "stop": stop, "target": target, "risk_per_unit": risk, "stop_pct": dist / entry,
            "qty": qty, "notional": round(notional, 2), "margin": round(notional / args.leverage, 2),
            "loss_at_stop": round(qty * dist + notional * FEE_RT, 2), "gain_at_target": round(qty * TARGET_R * dist - notional * FEE_RT, 2),
            "fees_round_trip": round(notional * FEE_RT, 2), "leverage": args.leverage,
            "liquidation": liq, "stop_before_liquidation": (liq is None) or ((stop > liq) if side == "long" else (stop < liq))}


def verdict(h, odds, p):
    if not p["stop_before_liquidation"]:
        return "BLOCK: liquidation comes before the stop. Lower the leverage."
    if h["fires"] < 8:
        return "Thin evidence: only %d past signals of this kind on this coin." % h["fires"]
    edge = odds["p_target"] - odds["p_stop"]
    if h["avg_R"] is not None and h["avg_R"] <= 0:
        return "History is negative after fees (avg %+.2fR over %d signals). Skip or wait." % (h["avg_R"], h["fires"])
    if edge < 0.03:
        return "Close to a coin flip once fees are counted."
    return "History and replay lean your way, but it is still odds, not a promise."


def run_once(args):
    meta, ctxs = hl.post({"type": "metaAndAssetCtxs"})
    rows = []
    for m, x in zip(meta["universe"], ctxs):
        if not m.get("isDelisted") and float(x["dayNtlVlm"]) >= args.min_volume:
            rows.append((m, x))
    rows.sort(key=lambda r: -float(r[1]["dayNtlVlm"]))
    rows = rows[:args.limit] if args.limit else rows
    out, scanned = [], 0
    for iv in args.intervals:
        for m, x in rows:
            try:
                cs = hl.candles(m["name"], iv, 1500)
            except Exception:  # noqa: BLE001
                continue
            scanned += 1
            side = signal_at(cs, len(cs) - 1) if len(cs) > LOOKBACK + ATR_N + 2 else None
            if side:
                a = atr(cs, len(cs) - 1)
                p = plan(m, cs, side, a, args, m["maxLeverage"])
                h = history(cs, side)
                od = replay(cs, side, p["entry"], p["stop"], p["target"], p["risk_per_unit"])
                out.append({"symbol": m["name"], "interval": iv, "side": side, "plan": p, "history": h, "odds": od,
                            "volume_24h": float(x["dayNtlVlm"]), "funding_hr": float(x["funding"]),
                            "max_leverage": m["maxLeverage"], "verdict": verdict(h, od, p)})
            time.sleep(args.pause)
    doc = {"created_by": "trader-hl", "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "as_of": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()), "intervals": args.intervals,
           "rule": {"lookback": LOOKBACK, "volume_x": VOL_X, "stop_atr": STOP_ATR, "target_r": TARGET_R, "horizon_candles": HORIZON, "fee_round_trip": FEE_RT},
           "limits": {"size": args.size, "max_loss": args.max_loss, "leverage": args.leverage},
           "scanned": scanned, "signals": out,
           "note": "A signal is a rule firing on the last completed candle, not advice. Nothing here places an order."}
    os.makedirs("trader", exist_ok=True)
    with open("trader/hl_signals.json", "w") as fh:
        json.dump(doc, fh, indent=1)
    print("%s  %d candle sets scanned, %d signals" % (doc["as_of"], scanned, len(out)))
    for s in out:
        p, od = s["plan"], s["odds"]
        print("  %-7s %-3s %-5s entry %-10.5g stop %-10.5g target %-10.5g size $%-6.2f risk $%-5.2f | target %.0f%% stop %.0f%% | %s" % (
            s["symbol"], s["interval"], s["side"].upper(), p["entry"], p["stop"], p["target"], p["notional"], p["loss_at_stop"],
            od["p_target"] * 100, od["p_stop"] * 100, s["verdict"]))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--intervals", default="5m,15m")
    ap.add_argument("--size", type=float, default=100, help="max margin per trade, USD")
    ap.add_argument("--max-loss", type=float, default=30, help="max loss at the stop, USD")
    ap.add_argument("--leverage", type=float, default=1)
    ap.add_argument("--min-volume", type=float, default=2e6)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--pause", type=float, default=0.12)
    ap.add_argument("--loop", type=int, default=0, help="rescan every N seconds")
    args = ap.parse_args()
    args.intervals = [s.strip() for s in args.intervals.split(",")]
    while True:
        run_once(args)
        if not args.loop:
            break
        time.sleep(args.loop)


if __name__ == "__main__":
    main()
