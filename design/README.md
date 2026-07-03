# Handoff: War Room 2.0 — single-shell cockpit refresh

## Overview

War Room 2.0 replaces the current **tile-grid dashboard + separate drill-down pages**
with a **single Bloomberg-terminal-style cockpit**: a persistent top status bar, a
left nav rail that swaps the main area between four screens (Overview, Trading,
X-Agent, Brain), and a live scrolling ticker tape footer. Same data, same backend
capabilities — one dense, always-on operator surface instead of click-through pages.

The goal of this refresh is **look, density, and information architecture**, not new
backend features. Almost every panel already has a real route in the existing FastAPI
app (see the mapping table below). The one genuinely new data need is the **portfolio /
holdings** block — flagged explicitly in "The one real gap."

---

## About the design files

The file in this bundle — **`War Room 2.0.dc.html`** — is a **design reference created
in HTML**. It is a prototype showing intended look, layout, and behavior. **It is not
production code to drop into the app.** It runs in a bespoke preview runtime (a small
inline-React-style engine loaded from `support.js`, with `<x-dc>` / `<sc-for>` /
`<sc-if>` custom tags), and it fakes or hardcodes some data.

**Your task is to recreate this design inside the existing War Room codebase** — FastAPI
+ Jinja2 templates + HTMX + vanilla CSS — using that stack's established patterns. Do
**not** introduce React, a build step, or the `.dc.html` runtime. Translate the visual
design into:

- Jinja templates (a new `base.html` shell + one template per screen)
- Plain CSS in `static/style.css` (the current file is the v1 blue theme — this refresh
  is a full restyle)
- The existing HTMX + SSE mechanisms for streaming and nav

To open the reference: `War Room 2.0.dc.html` and `support.js` must sit in the same
folder (they do in this bundle). Open the `.html` file in a browser. It will fetch live
crypto/metals prices client-side — that's prototype-only behavior you will replace with
the server-side yfinance feed (see mapping).

---

## Fidelity: HIGH

This is a pixel-level hifi mock. Colors, typography, spacing, and layout are final and
exact — reproduce them faithfully. All values are documented in **Design Tokens** below.
The `.dc.html` uses **inline styles everywhere**; every number you need is literally in
the file. When something is ambiguous, the reference file is the source of truth.

---

## The one real gap: portfolio / holdings

The visual centerpiece of the Overview and Trading screens is a **Robinhood holdings
table**, a **6-up KPI strip** (account value, day P&L, open P&L, positions, buying power,
VIX), and an **Account panel** (portfolio value, stocks/cash split, day/open/realized
P&L, buying power). **There is no Robinhood/holdings backend in the app today.**

- `data/hood_universe.json` is the RH-supported **futures** symbol list — NOT equity
  holdings. Don't confuse the two.
- In the prototype, all of this comes from a hardcoded `rhSnapshot()` function in the
  logic class (search the file for `rhSnapshot`). It returns account totals plus a
  14-row holdings array: `[symbol, name, sector, qty, avgCost, lastPrice, prevClose]`.
  The prototype then computes cost, market value, P/L, P/L %, and day change per row.

**You need a real data source for this before the portfolio panels can go live.** Two options:

1. **Manual snapshot file** (lowest effort, matches existing patterns like
   `active_positions.json`): add `data/holdings.json` in the same shape as `rhSnapshot()`,
   plus a `GET /api/holdings` route + a `GET /portfolio` (or reuse in Overview render)
   that loads it, computes the derived fields server-side, and feeds the templates. User
   updates the JSON by hand when positions change. This is essentially formalizing what
   the prototype already hardcodes.
2. **Live Robinhood pull**: a new `agents/robinhood.py` (e.g. `robin_stocks`) following
   the same loose-coupling pattern as `agents/futures.py`, with caching. More work,
   auth/2FA concerns, but always-fresh.

Until this is decided and built, render the portfolio panels from the manual JSON (or a
clearly-labeled "SNAPSHOT" placeholder). The prototype already frames these as manual
snapshots ("SNAPSHOT 28 JUN · 18:19 ET", "MANUAL REFRESH") — keep that honest.

---

## Architecture: one shell, four screens

The current app is multi-page (`/` tiles → `/brain`, `/x-agent`, `/trading`,
`/trading/futures`). 2.0 collapses navigation into **one persistent shell** whose main
area swaps between four screens. Recommended approach in your stack (no SPA rewrite):

- **`base.html` becomes the shell**: top status bar + left nav rail + `{% block main %}`
  + live tape footer. Every screen template extends it.
- **One route per screen**, each rendering the full shell with its screen in the main
  block: `GET /` (Overview), `GET /trading`, `GET /x-agent`, `GET /brain`. Keep the
  existing sub-routes (`/trading/futures`, `/threads/...`, all the SSE + `/api/*`
  endpoints) unchanged — they already work.
- **Add `hx-boost="true"`** on the nav rail links so screen switches feel instant (HTMX
  swaps `<body>` without a full reload) while staying server-rendered. The nav rail
  highlights the active screen based on the current path.
- The prototype does client-side `setState({screen})` switching; you do it with real
  navigation + `hx-boost`. Same feel, fits your idioms.

The top bar (clock, command input, session/feed status, top tickers) and the tape footer
are **global** — they live in `base.html` and appear on every screen.

---

## Screens / Views

Coordinates below are from the reference file's inline styles. "span N" = columns in the
Overview 12-column grid.

### GLOBAL — Top status bar (`header`, height 48px)
- **Brand block** (min-width 218px, right border): a 15×15px amber diamond (rotated 45°,
  amber glow shadow) + "WAR ROOM" (13px/700, letter-spacing 2.5px, `#f2efe6`) + sublabel
  "OPR · XN  //  v2.0" (8.5px, `#6f7783`).
- **Command input** (flex:1): amber "▸" prompt + text input placeholder `command  ‹GO›`
  (12px/500) + "⌘K" key hint chip (bordered `#20252e`). On Enter, parses the typed code
  and switches screen: `OV`/`OVERVIEW`→Overview, `TR`/`TRADING`/`FU`/`FUTURES`→Trading,
  `XA`/`X-AGENT`→X-Agent, `BR`/`BRAIN`→Brain. Wire this to real navigation.
- **Top stats** (right, left border, gap 18px): 4 mini stats — DAY P&L, BTC, ETH, GOLD.
  Each: tiny live-dot + label (8.5px `#6f7783`) over value (13px/600, colored green/red).
  BTC/ETH/GOLD come from the futures feed (MBT/MET/MGC); DAY P&L from the portfolio.
- **Clock cluster** (right, left border): live HH:MM:SS (15px/600, tabular-nums, updates
  every 1s) over weekday-month-day date. Beside it, two status lines with dots: **market
  session** (NYSE OPEN/PRE-MKT/AFTER/CLOSED, computed from ET time) and **feed status**
  (FEED <time> / CONNECTING… / FEED OFFLINE).

### GLOBAL — Left nav rail (`nav`, width 74px)
- Four vertical buttons: **OV** OVERVIEW, **TR** TRADING, **XA** X-AGENT, **BR** BRAIN.
  Each = a 30×24px code chip over an 8px label. Active item: amber chip border + amber
  text + `borderLeft:2px solid amber` + soft amber background (`rgba(255,174,0,0.12)`).
- Footer of rail: a pulsing green "LIVE" dot (wr-pulse animation) + "⚙ CFG".

### GLOBAL — Live tape (`footer`, height 30px)
- Left: amber "LIVE TAPE" label chip (9.5px/700, letter-spacing 2px, black text on amber).
- Right: horizontally scrolling marquee of every futures contract — `SYMBOL price ±chg%`
  (green/red). Implemented by duplicating the item list twice inside a flex row animated
  `translateX(0 → -50%)` over 42s linear infinite (seamless loop). Feed from the same
  futures data as the Trading screen.

### SCREEN 1 — Overview (12-col grid, gap 10px)
- **KPI strip** (span 12): a 6-column bordered row. Cards: ACCT VALUE, DAY P&L, OPEN P&L,
  POSITIONS (14), BUY POWER (with "% deployed" sub), VIX (13.8). Each: 9px label / 23px
  value (tabular-nums) / 9px sub. Numeric values count up on load (see animations). **All
  from portfolio + VIX.**
- **Robinhood holdings** (span 7): header "ROBINHOOD · HOLDINGS" + "SNAPSHOT <time>" pill.
  Sub-line "INDIVIDUAL ••••0871 · 14 POSITIONS · QUOTES @ …". A 7-column table: SYMBOL
  (with sector sub-label) / QTY / AVG / LAST / MKT VAL / P/L / %. P/L and % colored. Rows
  sorted by P/L % descending. Footer: MKT VALUE total + UNREALIZED total·%.
- **Market regime** (span 5): "MARKET REGIME" + green "RISK-ON" badge. Big VIX number
  (30px, green) beside a calm→neutral→stress gradient bar with a white position marker.
  A green SPY area sparkline (SVG, 290×56). Then 3 rows: SPY vs 50-EMA, SPY vs 200-EMA,
  Breadth (A/D). Fed by the VIX/SPY Regime strategy.
- **Top movers** (span 4): "TOP MOVERS" + 6 rows, each = ★ + symbol / 60×18 sparkline /
  colored ±chg%. From the futures feed (sorted by abs change).
- **X-Agent** (span 4): "X-AGENT · TRENDING" + blinking dot. A headline (13px/600 Sans) +
  up-to-4 momentum bars (topic label / value / amber progress bar). Footer "BRIEF ✓ TODAY"
  / "60 DRAFTS QUEUED". From today's brief.
- **Brain** (span 4): "BRAIN · VAULT" + two stat boxes (THREADS count, NOTES IDX count) +
  "LAST QUERY" text + a fake "ask the vault…" input. Counts from threads + vault index.
- **System strip** (span 12): inline status chips — FEEDS, API, TAILSCALE, VAULT (note
  count), MODEL (sonnet-4-6), COST ($ today). Each = colored dot + label + value.

### SCREEN 2 — Trading (flex row: main flex:1 + right rail 332px)
- **Main → Robinhood holdings** (wider, 8-column variant): adds a DAY % column and a COST
  total in the footer. Same data + styling as the Overview holdings, more detail.
- **Main → Futures map**: a control bar ("FUTURES MAP · MARKET CONTEXT" + feed legend +
  SYNC time + amber ⟳ REFRESH button). Then contracts grouped by sector (EQUITY INDEX,
  CRYPTO, METALS, ENERGY, FX, RATES), each sector a responsive card grid
  (`repeat(auto-fill, minmax(178px, 1fr))`, gap 10px). Each card: ★ (if active) + symbol
  (13px/700) + live dot + name; price (19px/600); colored ±chg% + a 92×26 area sparkline;
  an "ACTIVE" corner tag on held positions. **From `agents/futures.py` — the prototype's
  client-side crypto/metals fetch is a stand-in for your server feed.**
- **Right rail → Account** (see portfolio gap): PORTFOLIO VALUE (27px), STOCKS/CASH boxes,
  then DAY P/L, OPEN P/L, REALIZED · 90D, BUYING POWER rows. "SNAPSHOT <time> · MANUAL SYNC".
- **Right rail → Regime** (compact): VIX 26px + 180×44 SPY sparkline + 50/200-EMA rows.
- **Right rail → Strategy**: "VIX / SPY EMA Regime" title + description + amber ▸ RUN and
  CHART buttons + "LAST RUN · …" line. Wire RUN to the real strategy run/SSE.

### SCREEN 3 — X-Agent (flex row: main flex:1 + right rail 340px)
- **Main → Today's brief**: header "TODAY'S BRIEF" + "<date> · <tokens> · <cost>" + ↻ REGEN
  button. Big headline (21px/600 Sans). A summary paragraph. A "MOMENTUM" list (topic /
  amber bar / metric). Then an "Angle of the day" / "Reply target" analysis block with
  amber ▸ lead-ins. **From the real brief markdown** — you already render brief markdown
  server-side; feed the headline/summary/momentum from it (or restructure the brief format).
- **Right rail → Reply bot**: ACCOUNT picker (radio cards: SmokeySnipe, BullishBytes777),
  a "▸ RUN REPLY BOT" button, and a **streaming subprocess log** area. **The prototype
  fakes this with `setTimeout` steps — replace with the real flow**: POST
  `/x-agent/replies/generate` then consume the `/x-agent/replies/stream` SSE (your existing
  endpoints). Button states: RUN → RUNNING… (with blinking cursor) → ✓ done.
- **Right rail → Past briefs**: date + KB-size rows, click to load. From `list_briefs()`.

### SCREEN 4 — Brain (flex row: threads rail 230px + chat pane flex:1)
- **Threads rail**: "THREADS" + amber "+ NEW CHAT" button + thread list (active item amber,
  left border). From `/threads`.
- **Chat pane**: header (thread title) + scrolling message area (user bubbles right-aligned
  `#13161b`; assistant bubbles left-aligned `#0a0c10` with an "ASSISTANT" label and amber
  `[[wikilinks]]` + source-file chips) + composer (input + amber SEND). **From the existing
  `/brain` + `/threads/{id}/messages` + `/messages/{id}/stream` SSE — this is a restyle of
  the chat you already have.** Keep the markdown-lite + obsidian:// wikilink rendering.

---

## Route → panel mapping (what already exists)

Almost everything is already wired. Reuse these; don't rebuild the data layer.

- **Futures map, top-bar BTC/ETH/GOLD, live tape** → `agents/futures.py`
  `quotes_by_sector()`; routes `GET /trading/futures`, `GET /trading/futures/grid` (HTMX
  partial, `?refresh=1` busts the 30s cache), `GET /api/futures`. Server-side yfinance
  with SVG sparklines already built (`_sparkline_svg`). **Replace the prototype's
  browser-side CoinGecko/gold-api fetch with this feed.** Active-position ★ from
  `data/active_positions.json` (currently `["MHG"]`).
- **Regime / VIX big number / SPY sparkline / strategy card** → `agents/trading.py`
  `STRATEGIES` (registry incl. `vix-spy-regime`) + `latest_output(slug)` /
  `latest_output_age_seconds(slug)`; routes `GET /trading`, `POST /trading/strategy/{slug}/run`,
  `GET /trading/strategy/{slug}/stream` (SSE), `GET /trading/output/{filename}` (PNG),
  `GET /api/strategies`. Charts are matplotlib PNGs rendered by a subprocess.
- **X-Agent brief, momentum, reply bot** → `agents/x_agent.py`; routes `GET /x-agent`,
  `POST /x-agent/brief/generate` + `GET /x-agent/brief/stream` (SSE), `POST
  /x-agent/replies/generate` + `GET /x-agent/replies/stream` (SSE). `ACCOUNTS` registry,
  `list_briefs()`, `read_brief(date)`. Real subprocess streaming — better than the mock.
- **Brain / vault chat** → routes `GET /brain`, `GET/POST /threads`, `GET /threads/{id}`,
  `POST /threads/{id}/messages`, `GET /messages/{id}/stream` (SSE), `POST /reindex`;
  `conversations/threads.py`, `retrieval/search.py`. Markdown + `[[wikilink]]`→obsidian://
  rendering in `app.py` (`_render_markdown_lite`).
- **System strip / Brain counts** → thread count via `t.list_threads()`, vault notes via
  `index_size()`, model from `config.MODEL` (`claude-sonnet-4-6`).
- **Portfolio KPI strip / holdings / Account** → **NO ROUTE YET.** See "The one real gap."

Existing stack: FastAPI, Jinja2 (`templates/`, partials in `templates/partials/`), HTMX
2.0.4 + `htmx-ext-sse` 2.2.2 (loaded in `base.html`), vanilla CSS (`static/style.css`,
cache-busted via `?v={{ cache_v }}`). Single-user; auth is network-level via Tailscale.
Server binds `0.0.0.0:8765`.

---

## Interactions & behavior

- **Screen switching**: nav rail buttons + command-bar codes. Implement as real routes +
  `hx-boost`. Active screen derived from request path.
- **Command bar**: Enter parses uppercased input → screen (mapping above); clears on submit.
- **Clock**: updates every 1000ms (`HH:MM:SS`), tabular-nums so it doesn't jitter.
- **Live feed poll**: prototype re-fetches every 45s. For your version, the futures cache
  is 30s server-side; a small HTMX `hx-trigger="every 45s"` on the tape/tickers/futures
  grid (hitting `/trading/futures/grid`) keeps them fresh without a full reload.
- **KPI count-up**: numeric KPI + Brain-stat values animate 0→target over ~850ms
  (ease-out cubic) on load. Only elements flagged `data-countup="1"` in the prototype
  animate; currency values (`data-countup="0"`) render static. Nice-to-have; skippable.
- **Refresh button** (Trading): triggers a fresh futures pull (`/trading/futures/grid?refresh=1`).
- **Reply bot**: real POST→SSE flow (see Screen 3). Button RUN → RUNNING… (blinking amber
  cursor in the log) → "✓ 60 DRAFTS — RUN AGAIN".
- **Strategy RUN**: POST `/trading/strategy/vix-spy-regime/run` → stream SSE → show PNG.
- **Chat send**: existing message POST → assistant placeholder → SSE token stream.
- **Motion toggle**: prototype has a `motion` prop that disables all animations
  (`[data-motion="off"] * { animation: none !important }`) for reduced-motion. Honor
  `prefers-reduced-motion` in CSS.

### Animations (keyframes, from the reference)
- `wr-blink` — opacity 1↔0.2, ~1.6s, for live/status dots.
- `wr-tape` — `translateX(0 → -50%)`, 42s linear infinite, the ticker marquee (content
  duplicated 2× for a seamless loop).
- `wr-pulse` — expanding box-shadow ring, 2.4s, the nav "LIVE" dot.
- `wr-fade` — fade + 7px rise, entrance.
- Count-up — JS rAF, 850ms, ease-out cubic (`1 - (1-t)^3`).

---

## State & data fetching

- **Client state is minimal** — in the real app, "current screen" is the URL, not JS
  state. Keep it that way.
- **Server data per screen**: Overview needs portfolio + VIX-regime summary + futures
  movers + today's brief summary + thread/vault counts. Trading needs futures + portfolio
  + regime/strategy. X-Agent needs briefs + accounts + run state. Brain needs threads +
  messages. All except portfolio have existing loaders.
- **Streaming**: three SSE endpoints already exist (chat, brief/replies, strategy). They
  send full accumulated HTML fragments per event (HTMX-SSE swaps innerHTML). Preserve
  the auto-reconnect guards in `app.py` (they prevent duplicate subprocess spawns).
- **Caching**: futures 30s (`QUOTE_CACHE_TTL_SECONDS`), strategy PNGs cached per slug by
  latest timestamp, briefs are files on disk.

---

## Design tokens

### Colors
Backgrounds:
- `#070809` — app base (deepest)
- `#0a0c10` — nav rail, top bar, footer, inset boxes
- `#0d0f13` — panel / card surface
- `#13161b` — user chat bubble / chip fill

Borders:
- `#1b1f27` — primary panel/section border
- `#15181e` — subtle row divider
- `#20252e` — chip / input / key-hint border
- `#272d38` — nav code-chip border (inactive); also scrollbar thumb

Text:
- `#f2efe6` — brightest (brand, clock, big values)
- `#e8e5db` — strong (symbols, prices, values)
- `#d7d4ca` — body default
- `#bcc0c8` — secondary
- `#9aa1ad` — bright label / section-header text
- `#6f7783` — muted label
- `#5b626d` — dim caption
- `#474d57` — dimmest / input placeholder

Accent + semantic:
- `#ffae00` — **amber accent** (primary). Soft fill `rgba(255,174,0,0.12)`; line/border
  `rgba(255,174,0,0.5)`; selection `rgba(255,174,0,0.32)`; top glow
  `radial-gradient(1100px 460px at 78% -12%, rgba(255,174,0,0.045), transparent 60%)`.
- `#2bd17e` — up / positive / risk-on green
- `#ff5247` — down / negative red
- Sparkline fills: green `rgba(43,209,126,0.10)`, red `rgba(255,82,71,0.10)`
- Accent is themeable in the prototype; alternates offered: `#36c2ff` (cyan), `#2bd17e`
  (green), `#ff7a3c` (orange), `#ffae00` (amber, default). Ship amber.

### Typography
- **IBM Plex Mono** (weights 400/500/600/700) — the primary UI face: labels, numbers,
  tickers, nav, buttons, everything default.
- **IBM Plex Sans** (400/500/600/700) — prose only: brief headline/body, chat message
  text, thread titles, strategy descriptions.
- Google Fonts import in the reference `<helmet>`. `font-variant-numeric: tabular-nums`
  on all changing numbers (prices, P/L, clock, KPIs).

Scale (px / weight / letter-spacing):
- Brand 13/700/2.5 · rail label 8/—/1 · section header 10/600/1.8
- Micro labels 8.5–9 / — / 1–1.6
- Body 13/400 (mono) or 13/1.6 (Sans prose) · secondary 11–12
- Clock 15/600/1.5 · KPI value 23/600 · Account value 27/600
- VIX 30/600 (Overview) or 26 (Trading rail) · Brief headline 21/600 (page) or 13/600 (card)
- Futures card: symbol 13/700, price 19/600, change 12/600

### Spacing / radii
- Grid + inter-card gap: **10px** (dense) / **15px** (comfortable). Main padding **12px**
  (dense) / **17px** (comfortable). Ship dense.
- Shell rows: header **48px**, footer **30px**, nav rail **74px**, Trading right rail
  **332px**, X-Agent right rail **340px**, Brain threads rail **230px**.
- Border radius: **4px** panels/cards, **3px** chips/buttons/inputs/inset boxes, **2px**
  small tags, **8px** chat bubbles (with one 2px "tail" corner). Everything is crisp and
  low-radius — this is a terminal, not a consumer app. Fine 1px borders throughout.
- Density is a `--gap` / `--pad` CSS-variable pair in the reference; a body class
  (`.density-dense` / `.density-comfortable`) is the natural port.

---

## Assets

- **Fonts**: IBM Plex Mono + IBM Plex Sans via Google Fonts (already the intended load).
- **No image assets** in the shell — the diamond logo, sparklines, gradient bar, and
  regime chart are all CSS/inline-SVG. Strategy charts remain matplotlib **PNGs** served
  from `/trading/output/{filename}` (existing).
- **Icons**: a few unicode glyphs (▸ ★ ⟳ ↻ ⚙ ↵ 𝕏 ›). No icon font. Emoji tile icons from
  v1 are dropped in 2.0 — the design is glyph + type only.
- No Anthropic brand assets involved.

---

## Files in this bundle

- `War Room 2.0.dc.html` — the hifi design reference (open in a browser; needs
  `support.js` beside it). All exact styles live here as inline styles.
- `support.js` — runtime for the reference file only. **Not** part of the target app;
  do not port it.

### Target codebase files you'll touch (in the user's `War Room/` project)
- `templates/base.html` — becomes the cockpit shell (top bar + nav rail + tape + main block).
- `templates/dashboard.html` → repurpose as the **Overview** screen (was the tile grid).
- `templates/trading.html`, `templates/futures_map.html` → merge into the **Trading** screen.
- `templates/x_agent.html` → **X-Agent** screen restyle.
- `templates/brain.html` (+ `partials/chat_pane.html`, `message.html`, `message_pair.html`,
  `thread_list.html`) → **Brain** screen restyle.
- `templates/partials/*_streaming.html` → restyle the SSE placeholders to match.
- `static/style.css` → full restyle to the tokens above (currently the v1 blue theme).
- `app.py` → add the portfolio route(s) + data; add nav/active-screen context; otherwise
  routes are largely reusable.
- `agents/` → add `robinhood.py` **or** a `data/holdings.json` loader for the portfolio gap.
- `templates/coming_soon.html` — SAP Deals / Vault / Settings tiles are still "coming
  soon"; 2.0 has no nav entries for them yet (nav is OV/TR/XA/BR only). Decide whether to
  surface them via the command bar or a rail overflow later.

---

## Suggested build order

1. **Shell**: rebuild `base.html` (top bar + nav rail + tape footer + main block) and the
   CSS foundation (tokens, fonts, resets). Get the chrome pixel-right with placeholder main.
2. **Trading screen**: highest reuse — wire `futures_map` + `trading` data into the new
   card/grid styling. Feeds the tape + top tickers too.
3. **X-Agent + Brain**: restyle existing pages/partials into the new panels; keep all SSE.
4. **Overview**: the aggregation screen — pulls summaries from the others.
5. **Portfolio**: decide manual-JSON vs live-RH, build the loader/route, fill the KPI
   strip + holdings table + Account panel. This unblocks the parts of Overview/Trading
   that are currently hardcoded.

A developer who wasn't in this conversation can build the whole refresh from this README
plus the reference file. When in doubt about a pixel, open `War Room 2.0.dc.html`.
