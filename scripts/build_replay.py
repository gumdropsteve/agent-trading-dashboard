#!/usr/bin/env python3
"""Build a replay file for Baum Live from a Solana wallet's on-chain history.

    python3 scripts/build_replay.py <wallet> [--rpc URL] [--out data/replay-<first4>.js]

    # with Helius (recommended): faster, no public rate limits, and swaps get their venue
    HELIUS_API_KEY=... python3 scripts/build_replay.py <wallet>

Stdlib only. RPC resolution: --rpc, else SOLANA_RPC_URL, else Helius when HELIUS_API_KEY is set
(or when SOLANA_RPC_URL is a Helius URL, its api-key is reused), else the public endpoint. The key
never reaches the output file. Pulls every transaction for the wallet from a Solana RPC, reduces each one to the
wallet's own balance changes (SOL + SPL tokens), classifies it as a swap, deposit or withdrawal,
and marks holdings to market with 15-minute candles from GeckoTerminal (DexScreener fallback
for finding a pool). All accounting happens here, and the page only replays the result:
positions and PnL come from confirmed balance changes and market prices, never from the agent.

PnL is equity minus net deposits, so money moving in and out of the wallet is not counted as
profit. Realized PnL uses average cost per asset. Swaps don't record why they were made, so each
event carries a factual, auto-generated description (reason_source: "auto") until the harness
logs Baum's own rationale.
"""
import argparse, bisect, json, os, sys, time, urllib.parse, urllib.request

STABLES = {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT"}
SOL_MINT = "So11111111111111111111111111111111111111112"
QUOTE_ORDER = ["USDC", "USDT", "USDX", "SOL"]  # the leg that prices a swap; the other leg is the "asset"
SOL_DUST = 0.0025  # SOL moves below this are fees/rent, not a trade leg
GRID = 15 * 60


def http_json(url, body=None, tries=8):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, json.dumps(body).encode() if body else None,
                                         {"content-type": "application/json", "accept": "application/json", "user-agent": "baum-live/0.1"})
            return json.load(urllib.request.urlopen(req, timeout=30))
        except Exception as e:  # rate limits on public endpoints; back off, longer on 429
            err = e
            time.sleep((15 if getattr(e, "code", None) == 429 else 2) * (i + 1))
    raise RuntimeError(f"{redact(url)}: {err}")


def redact(url):
    """Drop query strings and path keys so API keys never reach logs."""
    u = urllib.parse.urlparse(url)
    path = "/<key>" if "alchemy.com" in (u.hostname or "") and u.path.count("/") >= 2 else u.path
    return f"{u.scheme}://{u.hostname}{path}" + ("?<redacted>" if u.query else "")


def rpc(url, method, params):
    r = http_json(url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    if "result" not in r:
        raise RuntimeError(f"{method}: {r.get('error')}")
    return r["result"]


def helius_key(rpc_url):
    if os.environ.get("HELIUS_API_KEY"):
        return os.environ["HELIUS_API_KEY"]
    u = urllib.parse.urlparse(rpc_url or "")
    if u.hostname and u.hostname.endswith("helius-rpc.com"):
        return (urllib.parse.parse_qs(u.query).get("api-key") or [None])[0]
    return None


def helius_venues(key, wallet):
    """signature -> (type, source) from Helius's parsed-transactions API, e.g. ("SWAP", "JUPITER")."""
    out, before = {}, None
    while True:
        q = {"api-key": key, "limit": 100, **({"before": before} if before else {})}
        page = http_json(f"https://api.helius.xyz/v0/addresses/{wallet}/transactions?{urllib.parse.urlencode(q)}")
        for t in page:
            out[t["signature"]] = (t.get("type"), t.get("source"))
        if len(page) < 100:
            return out
        before = page[-1]["signature"]


VENUE = {"JUPITER": "Jupiter", "DFLOW": "DFlow", "RAYDIUM": "Raydium", "ORCA": "Orca", "METEORA": "Meteora",
         "PUMP_FUN": "Pump.fun", "PUMP_AMM": "PumpSwap", "OKX_DEX_ROUTER": "OKX", "PHOENIX": "Phoenix", "LIFINITY": "Lifinity"}


# Venue from the programs a tx invoked (top-level or CPI, read from the logs). Works on any RPC.
PROGRAM_VENUE = {
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "Jupiter", "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB": "Jupiter",
    "DF1ow4tspfHX9JwWJsAb9epbkA8hmpSEAtxXy1V27QBH": "DFlow",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "Pump.fun", "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "PumpSwap",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium", "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "Raydium",
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": "Raydium", "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "Orca",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "Meteora", "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG": "Meteora",
}


def program_venue(tx):
    for pid, name in PROGRAM_VENUE.items():  # aggregators first: they're listed before AMMs
        if pid in tx["programs"] or f"Program {pid} invoke" in tx["logs"]:
            return name
    return None


def fetch_txs(rpc_url, wallet, delay=0.4):
    sigs, before = [], None
    while True:
        opts = {"limit": 1000, **({"before": before} if before else {})}
        page = rpc(rpc_url, "getSignaturesForAddress", [wallet, opts])
        sigs += page
        if len(page) < 1000:
            break
        before = page[-1]["signature"]
    out = []
    for s in reversed(sigs):
        if s.get("err"):
            continue
        tx = rpc(rpc_url, "getTransaction", [s["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
        meta, msg = tx["meta"], tx["transaction"]["message"]
        keys = [k["pubkey"] for k in msg["accountKeys"]]
        d = {}
        if wallet in keys:
            i = keys.index(wallet)
            d[SOL_MINT] = (meta["postBalances"][i] - meta["preBalances"][i]) / 1e9
        for sign, bal in ((-1, meta.get("preTokenBalances") or []), (1, meta.get("postTokenBalances") or [])):
            for b in bal:
                if b.get("owner") == wallet:
                    d[b["mint"]] = d.get(b["mint"], 0) + sign * float(b["uiTokenAmount"]["uiAmount"] or 0)
        out.append({"sig": s["signature"], "t": s["blockTime"], "signer": keys[0], "memo": s.get("memo"),
                    "programs": sorted({ix.get("programId") for ix in msg["instructions"]}),
                    "logs": " ".join(meta.get("logMessages") or []),
                    "delta": {m: v for m, v in d.items() if abs(v) > 1e-12}})
        time.sleep(delay)
    return out


def token_meta(mint):
    if mint == SOL_MINT:
        return {"symbol": "SOL", "name": "Solana"}
    if mint in STABLES:
        return {"symbol": STABLES[mint], "name": STABLES[mint]}
    try:
        a = http_json(f"https://api.geckoterminal.com/api/v2/networks/solana/tokens/{mint}")["data"]["attributes"]
        if a.get("symbol"):
            return {"symbol": a["symbol"], "name": a["name"]}
    except Exception:
        pass
    pairs = http_json(f"https://api.dexscreener.com/latest/dex/tokens/{mint}").get("pairs") or []
    for p in pairs:
        if p["baseToken"]["address"] == mint:
            return {"symbol": p["baseToken"]["symbol"], "name": p["baseToken"]["name"]}
    return {"symbol": mint[:4] + "…", "name": mint}


def candles(mint):
    """15m closes in USD from the token's deepest pool: [[unix, close], ...]."""
    pool = None
    try:
        pools = http_json(f"https://api.geckoterminal.com/api/v2/networks/solana/tokens/{mint}/pools?page=1")["data"]
        pool = pools[0]["attributes"]["address"]
        base = pools[0]["relationships"]["base_token"]["data"]["id"].split("_", 1)[1]
    except Exception:
        pairs = sorted(http_json(f"https://api.dexscreener.com/latest/dex/tokens/{mint}").get("pairs") or [],
                       key=lambda p: -(p.get("liquidity") or {}).get("usd", 0))
        if pairs:
            pool, base = pairs[0]["pairAddress"], pairs[0]["baseToken"]["address"]
    if not pool:
        return []
    time.sleep(2.5)  # GeckoTerminal free tier: ~30 req/min
    d = http_json(f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool}/ohlcv/minute"
                  f"?aggregate=15&limit=1000&currency=usd&token={'base' if base == mint else 'quote'}")
    return sorted([int(c[0]), float(c[4])] for c in d["data"]["attributes"]["ohlcv_list"])


class Prices:
    def __init__(self, series):
        self.s = {k: v for k, v in series.items() if v}
        self.t = {k: [c[0] for c in v] for k, v in self.s.items()}

    def at(self, sym, t):
        if sym in ("USDC", "USDT"):
            return 1.0
        if sym not in self.s:
            return 0.0
        i = bisect.bisect_right(self.t[sym], t) - 1
        return self.s[sym][max(0, i)][1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("wallet")
    ap.add_argument("--rpc", default=os.environ.get("SOLANA_RPC_URL"))
    ap.add_argument("--out")
    ap.add_argument("--label", default="Baum wallet")
    a = ap.parse_args()
    W = a.wallet
    out_path = a.out or os.path.join(os.path.dirname(__file__), "..", "data", f"replay-{W[:4]}.js")

    key = helius_key(a.rpc)
    rpc_url = a.rpc or (f"https://mainnet.helius-rpc.com/?api-key={key}" if key else "https://api.mainnet-beta.solana.com")
    host = urllib.parse.urlparse(rpc_url).hostname
    print(f"fetching transactions for {W} via {host}…", file=sys.stderr)
    txs = fetch_txs(rpc_url, W, delay=0.4 if host == "api.mainnet-beta.solana.com" else 0.05)
    venues = {}
    if key:
        try:
            venues = helius_venues(key, W)
            print(f"  venues for {sum(1 for v in venues.values() if v[1] in VENUE)} swaps from Helius", file=sys.stderr)
        except Exception as e:
            print(f"  Helius parsed transactions unavailable ({e}); using program IDs for venues", file=sys.stderr)
    mints = sorted({m for t in txs for m in t["delta"]})
    meta = {m: token_meta(m) for m in mints}
    sym = {m: meta[m]["symbol"] for m in mints}
    print("tokens:", ", ".join(sym.values()), file=sys.stderr)
    series = {}
    for m in mints:
        s = sym[m]
        if s in ("USDC", "USDT"):
            continue
        series[s] = candles(m)
        print(f"  {s}: {len(series[s])} candles", file=sys.stderr)
    P = Prices(series)

    units, cost = {}, {}  # per symbol
    realized = net_dep = 0.0
    events = []
    for tx in txs:
        t = tx["t"]
        legs = {sym[m]: v for m, v in tx["delta"].items()}
        sol = legs.get("SOL", 0.0)
        trade = {s: v for s, v in legs.items() if not (s == "SOL" and abs(v) < SOL_DUST)}
        ins = {s: v for s, v in trade.items() if v > 0}
        outs = {s: -v for s, v in trade.items() if v < 0}
        # apply every balance change (fees and rent included) to holdings
        for s, v in legs.items():
            units[s] = units.get(s, 0.0) + v
        ev = {"t": t, "sig": tx["sig"], "relayer": tx["signer"] if tx["signer"] != W else None}
        src = (venues.get(tx["sig"]) or (None, None))[1]
        venue = VENUE.get(src) or program_venue(tx)
        if venue:
            ev["venue"] = venue
        if len(ins) == 1 and len(outs) == 1:
            (si, ai), (so, ao) = next(iter(ins.items())), next(iter(outs.items()))
            quote = min((si, so), key=lambda s: QUOTE_ORDER.index(s) if s in QUOTE_ORDER else 99)
            asset = so if quote == si else si
            usd = (ai if quote == si else ao) * P.at(quote, t)
            if quote == so:  # bought asset
                cost[asset] = cost.get(asset, 0.0) + usd
                cost[quote] = cost.get(quote, 0.0) * (1 - ao / max(units[quote] + ao, 1e-18))
                amt, px = ai, usd / ai
                ev.update(lane=asset, kind="inc", label=f"Buy {asset}", units=amt, price=px, notional=usd, realized=0.0)
                if asset.upper().startswith("MUSDX") and quote == "USDX":
                    ev["label"] = "Stake USDX → mUSDX"
            else:  # sold asset
                held = units[asset] + ao
                basis = cost.get(asset, 0.0) * (ao / held) if held > 0 else 0.0
                pnl = usd - basis
                cost[asset] = cost.get(asset, 0.0) - basis
                cost[quote] = cost.get(quote, 0.0) + usd
                realized += pnl
                px = usd / ao
                share = ao / held if held > 0 else 1
                ev.update(lane=asset, kind="red", label=f"Sell {asset}" + (f" for {quote}" if quote not in ("USDC", "USDT") else ""),
                          units=-ao, price=px, notional=usd, realized=pnl, share=share,
                          ret=(pnl / basis) if basis > 0 else None)
            ev["pair"] = f"{so} → {si}"
        elif ins and not outs:
            s, v = next(iter(ins.items()))
            usd = sum(v * P.at(s, t) for s, v in ins.items())
            for s2, v2 in ins.items():
                cost[s2] = cost.get(s2, 0.0) + v2 * P.at(s2, t)
            net_dep += usd
            ev.update(lane="WALLET", kind="dep", label=f"Deposit {s}", units=v, price=P.at(s, t), notional=usd, realized=0.0)
        elif outs and not ins:
            s, v = next(iter(outs.items()))
            usd = sum(v * P.at(s, t) for s, v in outs.items())
            for s2, v2 in outs.items():
                held = units[s2] + v2
                cost[s2] = cost.get(s2, 0.0) * (1 - v2 / held) if held > 0 else 0.0
            net_dep -= usd
            ev.update(lane="WALLET", kind="wd", label=f"Withdraw {s}", units=-v, price=P.at(s, t), notional=usd, realized=0.0)
        else:
            continue  # fee-only or multi-leg tx we don't model yet
        ev["reason"] = describe(ev)
        ev["reason_source"] = "auto"
        ev["holdings"] = {s: round(u, 9) for s, u in units.items() if abs(u) > 1e-9}
        ev["net_deposits"] = net_dep
        ev["realized_total"] = realized
        events.append(ev)

    # mark-to-market series on a 15m grid, plus a point on each side of every event
    t0 = (events[0]["t"] // GRID) * GRID
    t_end = max(max(v[-1][0] for v in series.values() if v), events[-1]["t"]) + GRID
    ts = sorted(set(range(t0, t_end + 1, GRID)) | {e["t"] for e in events} | {e["t"] - 1 for e in events})
    pts, ei, hold, dep, real = [], 0, {}, 0.0, 0.0
    syms = sorted({s for e in events for s in e["holdings"]})
    for t in ts:
        while ei < len(events) and events[ei]["t"] <= t:
            hold, dep, real = events[ei]["holdings"], events[ei]["net_deposits"], events[ei]["realized_total"]
            ei += 1
        vals = {s: hold.get(s, 0.0) * P.at(s, t) for s in syms}
        eq = sum(vals.values())
        pts.append({"t": t, "eq": round(eq, 4), "dep": round(dep, 4), "pnl": round(eq - dep, 4), "real": round(real, 4),
                    "pos": {s: round(v, 4) for s, v in vals.items() if abs(v) > 0.005}})

    data = {"wallet": W, "chain": "solana", "label": a.label, "generated_at": int(time.time()), "t0": t0,
            "symbols": syms, "tokens": {sym[m]: {"mint": m, **meta[m]} for m in mints},
            "events": events, "series": pts,
            "notes": "Derived from on-chain balance changes and GeckoTerminal 15m closes. Reasons are auto-generated descriptions, not Baum's own rationale."}
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w") as f:
        f.write("// Generated by scripts/build_replay.py. Do not edit by hand.\nwindow.BAUM_REPLAY = ")
        json.dump(data, f, separators=(",", ":"))
        f.write(";\n")
    last = pts[-1]
    print(f"wrote {out_path}: {len(events)} events, {len(pts)} points; equity ${last['eq']:.2f}, "
          f"net deposits ${last['dep']:.2f}, PnL ${last['pnl']:.2f}, realized ${last['real']:.2f}", file=sys.stderr)


def fmt_px(p):
    return f"${p:,.2f}" if p >= 1 else f"${p:.6f}".rstrip("0")


def describe(ev):
    who = f"{ev['relayer'][:4]}…{ev['relayer'][-4:]}" if ev.get("relayer") else ""
    relay = f" Filled by relayer {who}." if who else ""
    via = f" via {ev['venue']}" if ev.get("venue") else ""
    if ev["kind"] == "dep":
        src = f" from {who}" if who else ""
        return f"{abs(ev['units']):,.4g} {ev['label'].split()[-1]} (${ev['notional']:,.2f}) arrived{src}. Counted as a deposit, not profit."
    if ev["kind"] == "wd":
        return f"{abs(ev['units']):,.4g} {ev['label'].split()[-1]} (${ev['notional']:,.2f}) left the wallet. Counted as a withdrawal, not a loss.{relay}"
    if ev["kind"] == "inc":
        return f"{ev['pair']}{via}: {abs(ev['units']):,.4g} {ev['lane']} for ${ev['notional']:,.2f} at {fmt_px(ev['price'])}.{relay}"
    pos = "the whole position" if ev.get("share", 1) > 0.98 else f"{ev['share'] * 100:.0f}% of the position"
    r = ev.get("ret")
    perf = f" {'+' if r >= 0 else '−'}{abs(r) * 100:.1f}% vs average cost, realized {'+' if ev['realized'] >= 0 else '−'}${abs(ev['realized']):,.2f}." if r is not None else ""
    return f"{ev['pair']}{via}: sold {pos} at {fmt_px(ev['price'])} for ${ev['notional']:,.2f}.{perf}{relay}"


if __name__ == "__main__":
    main()
