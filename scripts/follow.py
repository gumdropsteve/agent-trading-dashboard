#!/usr/bin/env python3
"""Follow a Solana wallet live and serve Baum Live on localhost.

    SOLANA_RPC_URL=<alchemy or helius url> python3 scripts/follow.py <wallet> [--interval 15] [--port 8765]

Every --interval seconds: fetches only the wallet's new transactions (confirmed commitment), marks
holdings with current GeckoTerminal prices, and atomically rewrites data/replay-<first4>.js. The page's
Live view (?replay=<first4>&live=1) re-reads that file and animates new trades in. 15m candles are
refreshed every 10 minutes to stay inside GeckoTerminal's free rate limit. Ctrl-C stops it.
"""
import argparse, functools, http.server, os, sys, threading, time, urllib.parse, webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_replay as B  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CANDLE_REFRESH = 600


class NoCache(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, *args):
        pass


def live_prices(meta):
    """Current USD price per symbol, one GeckoTerminal call per 30 tokens."""
    mints = [m for m, v in meta.items() if v["symbol"] not in ("USDC", "USDT")]
    by_lower = {m.lower(): m for m in mints}
    out = {}
    for i in range(0, len(mints), 30):
        d = B.http_json("https://api.geckoterminal.com/api/v2/simple/networks/solana/token_price/" + ",".join(mints[i:i + 30]), tries=2)
        for m, p in (d["data"]["attributes"]["token_prices"] or {}).items():
            m = by_lower.get(m.lower(), m)
            if p and m in meta:
                out[meta[m]["symbol"]] = float(p)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("wallet")
    ap.add_argument("--rpc", default=os.environ.get("SOLANA_RPC_URL"))
    ap.add_argument("--interval", type=int, default=15)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--label", default="Baum wallet")
    ap.add_argument("--no-open", action="store_true")
    a = ap.parse_args()
    W, out = a.wallet, B.default_out(a.wallet)
    rpc, key = B.resolve_rpc(a.rpc)
    delay = B.rpc_delay(rpc)
    host = urllib.parse.urlparse(rpc).hostname
    if host == "api.mainnet-beta.solana.com":
        print("warning: using the public RPC; set SOLANA_RPC_URL to Alchemy or Helius for reliable polling", file=sys.stderr)

    print(f"loading history for {W} via {host}…", file=sys.stderr)
    txs = B.fetch_txs(rpc, W, delay=delay)
    venues = {}
    if key:
        try:
            venues = B.helius_venues(key, W)
        except Exception as e:
            print(f"  Helius parsed transactions unavailable ({e}); using program IDs for venues", file=sys.stderr)
    meta, series = B.load_market(txs)
    candles_at = time.time()

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", a.port), functools.partial(NoCache, directory=ROOT))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://localhost:{a.port}/index.html?replay={W[:4]}&live=1"
    first = True
    print(f"following {W[:4]}…{W[-4:]} every {a.interval}s · {url}  (ctrl-c to stop)", file=sys.stderr)

    try:
        while True:
            now = time.time()
            try:
                new = B.fetch_txs(rpc, W, delay=delay, until=txs[-1]["sig"] if txs else None)
                new = [t for t in new if t["sig"] not in {x["sig"] for x in txs}]
                txs += new
                refresh = now - candles_at > CANDLE_REFRESH
                meta, series = B.load_market(txs, meta, series, refresh=refresh)
                if refresh:
                    candles_at = now
                try:
                    lp = live_prices(meta)
                except Exception:
                    lp = {}
                marked = {s: (c + [[int(now), lp[s]]] if c and s in lp else c) for s, c in series.items()}
                data = B.build(W, txs, meta, marked, venues, a.label, now=now)
                data["live"] = {"updated": int(now), "interval": a.interval}
                B.write(data, out)
                last = data["series"][-1]
                stamp = time.strftime("%H:%M:%S")
                fresh = {t["sig"] for t in new}
                for e in (e for e in data["events"] if e["sig"] in fresh):
                    print(f"\n{stamp}  NEW  {e['label']} · ${e['notional']:,.2f}" + (f" · realized {e['realized']:+,.2f}" if e.get("realized") else ""), file=sys.stderr)
                print(f"\r{stamp}  equity ${last['eq']:,.2f} · PnL {last['pnl']:+,.2f} · {len(data['events'])} events   ", end="", file=sys.stderr)
            except Exception as e:
                print(f"\n{time.strftime('%H:%M:%S')}  poll failed: {e}", file=sys.stderr)
            if first:
                first = False
                if not a.no_open:
                    webbrowser.open(url)
            time.sleep(max(1, a.interval - (time.time() - now)))
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
        srv.shutdown()


if __name__ == "__main__":
    main()
