# War Room — v1

Browser-based dashboard that lets Xander chat with his Obsidian vault from any device on his Tailnet (specifically: the SAP work laptop, where Claude Code can't run).

This is **v1, the wedge**: one capability, deployed end-to-end. Future versions add transcript intake (v2) and full agent orchestration / project status / trading widgets (v3+).

Vault home: see `projects/project-cockpit-dashboard.md` in the Second Brain for the full project doc, decisions, and architecture rationale.

---

## Stack

| Layer | Choice | Why |
|---|---|---|
| Backend | FastAPI (Python 3.13) | Matches the rest of Xander's Python stack |
| Frontend | HTMX + Jinja templates + vanilla CSS | Server-rendered, no React build step, scales to v3 |
| Streaming | Server-Sent Events via `htmx-ext-sse` | Native chat-app UX |
| Retrieval | SQLite FTS5 + BM25 | Sufficient for a small vault (75 notes); swap for vector DB when vault grows |
| LLM | Anthropic Claude (default: `claude-sonnet-4-6`) | Per Xander 2026-05-01 |
| Persistence | SQLite (one file: `data/war_room.db`) | Threads + messages + cost log + FTS5 index, all in one |
| Hosting | localhost on MacBook, exposed via Tailscale | Free, validates workflow before any infra investment |
| Auth | Tailscale network-level only | Single-user, single-tailnet, no app password |

---

## Setup (one-time)

### 1. Venv (outside iCloud — same trap that's bitten the X agent + gap_trader)

```bash
python3.13 -m venv ~/.venvs/war_room
~/.venvs/war_room/bin/pip install -r "/Users/xandernostrand/Desktop/War Room/requirements.txt"
```

### 2. Anthropic API key

```bash
mkdir -p ~/.config/war_room
echo 'ANTHROPIC_API_KEY=sk-ant-YOUR_KEY_HERE' > ~/.config/war_room/.env
chmod 600 ~/.config/war_room/.env
```

The dashboard refuses to start without this.

### 3. (Optional) override defaults via the same `.env`

```bash
VAULT_PATH=/Users/xandernostrand/Desktop/Second Brain    # vault location
WAR_ROOM_MODEL=claude-sonnet-4-6                          # model id
WAR_ROOM_HOST=0.0.0.0                                     # bind addr (0.0.0.0 for Tailscale)
WAR_ROOM_PORT=8765                                        # default port
WAR_ROOM_TOP_N=5                                          # top vault notes per query
WAR_ROOM_PER_NOTE_CAP=4000                                # per-note context char cap
INPUT_PRICE_PER_M_USD=3.00                                # for cost logging
OUTPUT_PRICE_PER_M_USD=15.00
```

---

## Run

```bash
cd "/Users/xandernostrand/Desktop/War Room"
~/.venvs/war_room/bin/python app.py
```

First startup auto-indexes the vault (~1-2 sec for 75 notes). Subsequent starts skip re-indexing — use the **Reindex** button in the sidebar after edits.

Dashboard is at `http://localhost:8765` from the host machine.

### From the SAP work laptop via Tailscale

1. Install Tailscale on both the MacBook (host) and the SAP laptop ([download](https://tailscale.com/download)).
2. Sign in to the same tailnet on both.
3. Find the MacBook's tailnet name/IP from `tailscale status` (something like `xanders-macbook.tail-scale.ts.net` or `100.x.x.x`).
4. From the SAP laptop browser: `http://<macbook-tailnet-name>:8765`.

The MacBook must be awake (lid open or external monitor connected). Lid-closed = dashboard unreachable.

---

## Architecture (one diagram, in words)

```
SAP browser
    │ HTTP/SSE
    ▼
[FastAPI on MacBook :8765]
    │
    ├── routes/  ── HTMX templates (Jinja) ── static/style.css
    │
    ├── retrieval/   ── BM25 search over SQLite FTS5
    │                  │
    │                  └── indexer walks Obsidian vault → FTS5 table
    │
    ├── llm/         ── Anthropic SDK, streaming text events
    │
    └── conversations/  ── threads + messages + cost log (SQLite)
                            │
                            └── all in data/war_room.db
```

---

## What v1 does

- **Chat with the vault.** Ask "what did I conclude about my trend-following backtests?" — get an answer that cites which vault notes it's drawing from.
- **Conversational, not one-shot.** Multi-turn threads persist in SQLite. Sidebar shows recent threads, sorted by last-updated. "+ New chat" button starts fresh.
- **Source citations.** Every assistant answer includes clickable links back to the vault notes used as context (opens the note in Obsidian via `obsidian://`).
- **Streaming responses.** Token-by-token via SSE, like the Claude.ai UX.
- **Cost logged per message.** Token counts + $ stored in the messages table for later review.
- **On-demand reindex.** Sidebar button drops + rebuilds the FTS5 index after vault edits.

## What v1 deliberately does NOT do

- No vault writes from the dashboard. Read-only during work hours; reconcile in Obsidian on the personal MacBook.
- No agent orchestration (running `trending_main.py`, scans, etc.). Coming in v3.
- No project status cards / trading widgets / transcript intake. Coming in v2/v3.
- No app-level auth — Tailscale handles network-level access control.
- No mobile-optimized UI. Works in mobile Safari but not designed for it.

---

## Future work (per project file)

- v2: transcript intake (paste a customer call → summary, action items, exportable markdown).
- v3+: agent launcher, project status cards, trading widgets, multi-source intake. Designed from real v1+v2 usage data, not speculatively.

---

## Code layout

```
War Room/
├── app.py                    # FastAPI app + all routes
├── config.py                 # env vars, paths, model choice, pricing
├── requirements.txt
├── README.md (this file)
├── .gitignore
├── retrieval/
│   ├── indexer.py            # Walk vault → FTS5 table
│   └── search.py             # BM25 query interface
├── llm/
│   └── claude_client.py      # Anthropic SDK wrapper + system prompt + streaming
├── conversations/
│   ├── db.py                 # SQLite schema + connection
│   └── threads.py            # Thread + message CRUD + cost computation
├── templates/
│   ├── base.html             # Main shell (sidebar + main pane)
│   └── partials/
│       ├── welcome.html      # Empty-state main pane
│       ├── chat_pane.html    # Thread view (messages + composer)
│       ├── thread_list.html  # Sidebar thread list
│       ├── message.html      # Single message render
│       └── message_pair.html # User + assistant-placeholder after form post
├── static/
│   └── style.css
└── data/                     # Gitignored
    └── war_room.db
```

Future modules (v2/v3): `intake/`, `agents/`, `widgets/`. Project structure already accommodates them.
