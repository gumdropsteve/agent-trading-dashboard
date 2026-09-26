# Baum Live: a real-time view of an agent trading

A prototype of a live dashboard for watching a Baum agent trade a Broker wallet. It shows a timeline of every action, positions that resize as trades land, PnL marked to market, and the reason Baum gave for each move.

The prompt was [Kyle Samani's question](https://x.com/KyleSamani/status/2103336166158188570): *"Has anyone built any really good real-time live visualizations of an agent trading? … You can see some sort of timeline, position sizes, PNLs, those kinds of things in some sort of beautiful moving system."* Baum is well placed to answer it. It already trades (chat swaps on Solana and Robinhood Chain), it has run the USDX vault for six months, and it goes out to 3,888 Broker wallets when the beta opens around Oct 1.

![Baum Live replaying a real Solana wallet](docs/replay.png)

*Above: replay of a real Baum wallet on Solana. [Simulated mode](docs/screenshot.png) runs a mock Broker wallet through every Portal action.*

## Run it

It's one HTML file with no build step and no dependencies:

```sh
open index.html          # macOS
# or serve it
python3 -m http.server 8000   # then http://localhost:8000
```

The page has two sources, switched in the header:

- **Simulated** (default): a seeded simulator in the page generates the prices, actions and reasons. Nothing touches a chain.
- **Real wallet** (`?replay=6s8J`): a replay of wallet `6s8JDoMHFQNtJG5LFdKvqnPjqai3ucxrdANQJxjKzyqd` on Solana, rebuilt from on-chain data. See [Replaying a real wallet](#replaying-a-real-wallet).

URL parameters:

| Param | Effect |
|---|---|
| `replay=<id>` | Load `data/replay-<id>.js` instead of the simulator |
| `live=1` | With `replay`, follow the wallet live (needs `scripts/follow.py` running) |
| `at=end` | Jump straight to the end of a replay (for screenshots and sharing a still) |
| `theme=light` / `theme=dark` | Force a theme (otherwise the page follows the OS) |

Space pauses and resumes.

## What's on the page

| Panel | What it shows | Why it matters |
|---|---|---|
| **Tiles** | Equity, total PnL, realized PnL (closed trades + harvested yield), action count, gross exposure | The headline numbers |
| **PnL line** | Total PnL vs. starting capital, marked to market every minute | The shape of performance, not just the latest number |
| **Action lanes** | One lane per asset or venue: ETH, BAUM, NVDA (a Stock Token), ETH-PERP, LP, Lend. ▲ adds exposure, ▼ reduces it, ● harvests yield. Marker size scales with notional. | It shares the PnL line's time axis, so you can see which action moved the line |
| **Hollow → filled** | A marker is hollow while its tx is sent but unconfirmed, and fills on confirmation | Shows the gap between what the agent intended and what actually settled |
| **Positions** | Diverging bars (long right, short left) with unrealized PnL. Bars animate to their new size | Current state at a glance |
| **Why Baum acted** | The decision log: action, size, price, reason, tx, confirmation time, realized PnL | Block explorers can't show this. It's the main thing this page adds |
| **Table view** | Every action as a table (under the chart) | Makes the data accessible without hovering |

Hovering the PnL chart shows a crosshair with PnL and equity. Hovering a marker shows the full action with its reason.

The simulated agent runs the Portal actions from `baum-docs/portal/actions.md`: a BAUM DCA schedule with take-profit trims, a limit-order ladder on a Stock Token, an ETH momentum rebalance, LP on USDX/USDG, and USDG lending with periodic harvests. It also runs an ETH perp with take-profit, stop and time stop, because perps are on the "coming later" list and Kyle asked about them specifically.

## Replaying a real wallet

`scripts/build_replay.py` turns any Solana wallet into a replay file. It uses only the standard library:

```sh
SOLANA_RPC_URL=<alchemy or helius url> python3 scripts/build_replay.py <wallet> --label "Baum wallet"   # writes data/replay-<first4>.js
open "index.html?replay=<first4>"
```

Use a paid RPC. Stable's Alchemy Solana URL is `ALCHEMY_SOLANA_RPC_URL` in `ecosystem-contracts/usdx-contracts/ecosystem-api/.env`. The Helius key there (`SOLANA_RPC_URL`) is currently out of credits. When Helius is used (a Helius `SOLANA_RPC_URL` or `HELIUS_API_KEY`), swaps also get their venue from its parsed-transactions API. Otherwise the venue comes from the program IDs in the transaction logs, which covers Jupiter, DFlow, Raydium, Orca, Meteora and Pump.fun. Keys are redacted from logs and never written to the output.

What it does, which is a small version of the pipeline below:

1. **Pulls every transaction** for the wallet from a Solana RPC (`--rpc` or `SOLANA_RPC_URL`; falls back to the public endpoint, which rate-limits).
2. **Reduces each one to the wallet's own balance changes** (SOL and SPL tokens), so it doesn't matter which aggregator routed the swap (Jupiter, DFlow, a relayer).
3. **Classifies each one**: one asset in and one out is a swap, only in is a deposit, only out is a withdrawal. SOL moves under 0.0025 are fees or rent, not a trade leg. The swap's quote leg (USDC > USDT > USDX > SOL) prices it, and the other leg is the lane it appears in.
4. **Accounts for it**: average cost per asset, realized PnL on sells, and deposits and withdrawals tracked as net deposits so moving money in and out never shows up as profit or loss. **PnL = equity − net deposits.**
5. **Marks holdings to market** every 15 minutes using GeckoTerminal candles from each token's deepest pool (DexScreener finds the pool when GeckoTerminal doesn't know the token). USDC and USDT are pinned at $1.
6. **Writes `data/replay-<id>.js`**: the events, a mark-to-market series (equity, net deposits, PnL, realized, per-asset value), and token metadata. The page only replays it and derives nothing itself.

What the `6s8J` wallet shows (Sep 20–26): the trades were placed through the Baum chat, 6 routed via Jupiter and 4 via DFlow. That's 11 trades and 4 deposits or withdrawals, ending at **$180.34 equity on $165.93 net deposits (+$14.40), with +$3.90 realized**. The best trade was selling half a PAID position at +134%. The chart shows a SHARTCOIN position that ran to 3x on paper, then sold a day later at +7.6%. Unrealized PnL like that is exactly what a live view makes visible.

Limitations of this first version:

- **The descriptions aren't the reason for each trade.** They're generated from the chain ("sold 50% of the position at +134% vs average cost"). These trades were placed through the Baum chat, so the reason is the chat message that asked for them. Join the Telegram bot's log to each tx signature to show it. For trades Baum makes on its own (Portal actions), the harness hook below supplies the reason.
- **Multi-leg transactions are skipped** (e.g. an LP deposit that takes two tokens in one tx). None occur in this wallet.
- **Marks are 15-minute closes**, so a position bought and sold inside one candle shows its realized PnL but not its intra-candle path.
- **Prices come from GeckoTerminal's free API**, which rate-limits at about 30 requests a minute. The script backs off; neither Alchemy nor Helius serves historical token prices. For many wallets, add a paid price source (Birdeye, or CoinGecko Pro's on-chain OHLCV).
- **Solana only.** Robinhood Chain needs the same reducer over Blockscout's token-transfer API.

## Following a wallet live

```sh
SOLANA_RPC_URL=<alchemy or helius url> python3 scripts/follow.py <wallet>
```

This serves the repo at `http://localhost:8765` and opens the **Live** view (`?replay=<first4>&live=1`). Every 15 seconds (`--interval`) the script:

- fetches only the wallet's **new** transactions, at `confirmed` commitment, so trades show up within seconds of landing;
- marks holdings with **current** GeckoTerminal prices, so the PnL line moves between trades, and refreshes the 15-minute candles every 10 minutes to stay under the free rate limit;
- rewrites `data/replay-<first4>.js` atomically.

The page re-reads that file every 5 seconds and animates new trades onto the lanes and into the feed. The badge shows **LIVE · ON-CHAIN** while updates are arriving, and **NOT LIVE** when the file is more than a minute stale (the follower isn't running).

This is the local version of the hosted design below. The follower becomes a webhook-fed service, and the file re-read becomes an SSE stream.

## How to build the real thing

### 1. One event stream, from two sources

```
Baum harness ──(decision: intent + reason)──┐
                                            ├─► event store ─► live API (SSE / WebSocket) ─► dashboard
Chain indexer ──(confirmed tx + fills)──────┘       │
                                                    └─► price marks ─► positions / PnL (derived)
```

- **The harness emits a decision event** whenever Baum decides to act, before it signs anything: `{decision_id, broker_id, wallet, chain, action, asset_in, asset_out, intended_size, limit/slippage, reason}`. The reason is the one-line rationale, not a transcript. **Only the harness can produce this, so it's the one backend change the project needs.**
- **The indexer confirms it.** Subscribe to logs for every Broker wallet on Robinhood Chain (RPC `eth_subscribe` or a hosted indexer) and on Solana (Helius webhooks / geyser). Join each confirmed tx back to its decision by tx hash (the harness records the hash when it submits).
- **The chain is the source of truth for sizes and prices.** The dashboard shows a trade as filled only after the indexer has seen it. The hollow/filled marker in the prototype is this rule made visible.
- **Positions and PnL are derived, never reported by the model.** Compute them from confirmed fills plus price marks (DEX pool prices for USDX pairs, oracle or pool prices for Stock Tokens, the perp venue's mark price). The @baumreview worker already follows the same rule: numbers come from data, never from Claude.

Suggested event schema (one row per action):

```ts
type AgentAction = {
  id: string;               // decision_id
  broker_id: number;        // Deed Deck token id
  wallet: string;
  chain: "robinhood" | "solana";
  lane: string;             // asset or venue, e.g. "ETH", "BAUM", "ETH-PERP", "LP:USDX/USDG"
  kind: "inc" | "red" | "yld";
  label: string;            // "DCA", "Limit buy", "Open short", "Harvest LP fees", ...
  size: number; price: number; notional_usd: number;
  realized_pnl_usd?: number;
  reason: string;
  decided_at: string; sent_at?: string; confirmed_at?: string;
  tx_hash?: string;
  status: "decided" | "sent" | "confirmed" | "failed";
  // perps
  leverage?: number; liq_price?: number; funding_paid_usd?: number;
};
```

### 2. Storage and transport

- **Postgres** for actions, fills and one-minute equity snapshots per wallet. **Redis pub/sub** or Postgres `LISTEN/NOTIFY` for fan-out.
- **Server-Sent Events** carry the live feed (one-way, works through proxies, reconnects on its own). Send a snapshot on connect, then deltas.
- **Replay** uses the same endpoint with `?from=&to=&speed=`. The server streams history at a multiple of real time, and the client can't tell replay from live. That's how the prototype's speed control works.
- Hosting: Railway (like the `baum` worker) or SST (like `landing-page-v2`) both work.

### 3. Frontend

- Build it as a route in `baum-monorepo/apps/landing-page-v2` (Next.js), e.g. `/live` and `/live/[brokerId]`.
- Use **Canvas** for the moving parts (PnL line, action lanes) and plain DOM for tiles, positions and the feed. The prototype already works this way, and the rendering code ports almost directly into a React component that owns the canvases.
- For the fleet view below, use **PixiJS** (WebGL). 3,888 animated cards is too many for DOM or SVG.
- Swap the neutral palette for Baum's brand colors, but keep the three action colors distinguishable for colorblind viewers (run the palette through a CVD check).

### 4. The fleet view (the part only Baum can show)

A grid of all 3,888 Deed Deck cards. A card flashes in the action's color when its agent acts, and the fleet-wide totals (TVL in USDX, actions per minute, aggregate PnL) tick along the top. Click a card to open that Broker's single-agent view (this prototype). It answers Kyle's question for thousands of agents at once, and it doubles as the mint and activation pitch.

### 5. Privacy and compliance

- Broker wallets are public on-chain, but a public page ranking named wallets by PnL is a different thing. **Make per-Broker pages opt-in** (a toggle in the Portal) and show only aggregates in the fleet view by default.
- Carry the "not financial advice" line from `baum-docs/disclaimer.md` on every view that shows PnL.
- Never show a PnL figure that didn't come from confirmed fills and marks.

## Rollout

1. **Wallet replay (no harness changes).** ✅ Prototyped: `scripts/build_replay.py` plus `?replay=`. The USDX vault turned out to be mostly deposits and locks, so the first real replay is a Baum trading wallet instead. Next: run it on the wallets with the most activity, and record a clip for @baumreview to post in reply to Kyle.
2. **Live.** ✅ Local version: `scripts/follow.py` plus `?live=1`. Next: host it (Helius webhook or Alchemy address activity → Railway service → SSE) so a link works for anyone, and join the Baum chat log so each trade shows the instruction behind it.
3. **Broker wallets at beta (~Oct 1).** Index every Broker wallet, add the opt-in per-Broker pages, and ship the fleet view.
4. **Perps.** When perps reach the Portal, add leverage, liquidation price and funding to the lanes and positions. The schema above already has fields for them.

## Open questions

- Does the harness already log a rationale per action, or does that hook need to be written?
- Which venue prices should mark Stock Tokens outside US market hours?
- Should the reasons be shown verbatim, or passed through a short summarizer with guardrails, as @baumreview's replies are?
