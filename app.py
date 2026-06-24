"""War Room — FastAPI app with HTMX + SSE streaming.

Routes:
  GET  /                                  — chat shell (HTML)
  GET  /threads                            — sidebar thread list (HTMX partial)
  POST /threads                            — create new thread, redirect to it
  GET  /threads/{tid}                      — chat pane for a thread (HTMX partial)
  POST /threads/{tid}/messages             — send user message; create assistant placeholder; return both as HTML
  GET  /messages/{mid}/stream              — SSE stream that fills the assistant placeholder token-by-token
  POST /reindex                            — drop + rebuild the vault FTS5 index
  GET  /health                             — basic health check

Auth model: none at app layer. Tailscale provides network-level auth (only your
devices reach the server). Single-user, single-machine deployment.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt

from agents import futures, trading, x_agent
from config import HOST, PORT, VAULT_PATH
from conversations import threads as t
from conversations.db import init_db
from conversations.threads import Source, derive_title_from_first_message
from llm.claude_client import stream_response
from retrieval.indexer import index_size, reindex_vault
from retrieval.search import search


# Real markdown renderer. CommonMark + breaks (treat single newlines as <br>) +
# linkify (auto-detect URLs). HTML disabled to prevent injection from Claude responses.
_md = MarkdownIt("commonmark", {"breaks": True, "html": False, "linkify": True})

# Cache-busting version string. Set at app startup; appended to static URLs as ?v=<this>.
# Each restart busts the browser cache for CSS/JS so layout updates aren't held back by
# stale cached files (the bug that made the v1 tile dashboard render as a flat link list).
import time as _time
_CACHE_V = str(int(_time.time()))


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(title="War Room")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
# Make cache-buster available in every template as `cache_v`.
templates.env.globals["cache_v"] = _CACHE_V
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")


# In-memory store of in-flight assistant streams keyed by message id.
# Each value is the accumulated text + usage data, updated as streaming progresses.
# This gets persisted to SQLite when the stream completes.
_inflight: dict[str, dict] = {}


# Markdown-style wiki-link → HTML anchor. Used in the rendered assistant message.
_WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup() -> None:
    init_db()
    # If the vault index is empty, build it on first run. Otherwise leave alone —
    # user can hit the Reindex button manually.
    if index_size() == 0:
        print("[startup] vault index empty — running initial reindex…")
        stats = reindex_vault()
        print(f"[startup] indexed {stats['indexed']} notes in {stats['elapsed_seconds']}s")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _render_message_html(msg: t.Message) -> str:
    """Render a single message as the HTML the chat pane uses.

    Used both on initial page render and when finalizing a streamed assistant message
    (the streamed placeholder gets replaced by this on the 'done' SSE event).
    """
    return templates.env.get_template("partials/message.html").render(
        msg=msg,
        rendered_content=_render_markdown_lite(msg.content),
        obsidian_url=_obsidian_url,
    )


def _render_markdown_lite(text: str) -> str:
    """Real CommonMark rendering + post-process wiki-links to clickable obsidian:// anchors.

    Strategy: render markdown first (markdown-it escapes HTML in text content, so wiki-links
    `[[name]]` survive as plain text). Then post-process the rendered HTML to convert
    `[[name]]` → `<a href="obsidian://...">name</a>`.

    HTML is disabled in the markdown config so Claude can't inject script tags or similar.
    """
    rendered = _md.render(text)

    def linkify(match: re.Match) -> str:
        target = match.group(1).strip()
        label = (match.group(2) or target).strip()
        url = _obsidian_url_for_name(target)
        return f'<a href="{url}" class="wikilink" target="_blank">{html.escape(label)}</a>'

    return _WIKILINK_RE.sub(linkify, rendered)


def _obsidian_url(path: str) -> str:
    """Build an obsidian:// URL for a vault-relative path."""
    vault_name = VAULT_PATH.name
    return f"obsidian://open?vault={quote(vault_name)}&file={quote(path)}"


def _obsidian_url_for_name(note_name: str) -> str:
    """obsidian://open by note name (no extension). Obsidian resolves it across the vault."""
    vault_name = VAULT_PATH.name
    return f"obsidian://open?vault={quote(vault_name)}&file={quote(note_name)}"


# ---------------------------------------------------------------------------
# Tile registry — drives the dashboard grid
# ---------------------------------------------------------------------------

TILES: list[dict] = [
    {
        "slug": "brain",
        "icon": "🧠",
        "title": "Brain",
        "description": "Chat with the vault. Ask questions; get answers with source citations.",
        "href": "/brain",
        "enabled": True,
        "status": None,  # rendered dynamically (recent thread count)
        "status_class": "status-live",
    },
    {
        "slug": "x-agent",
        "icon": "𝕏",
        "title": "X Agent",
        "description": "Generate today's trending brief. Reply scraper + account growth scans on the v2 list.",
        "href": "/x-agent",
        "enabled": True,
        "status": None,  # rendered dynamically (see dashboard route)
        "status_class": "status-live",
    },
    {
        "slug": "trading",
        "icon": "📈",
        "title": "Trading",
        "description": "Visual regime widgets — VIX/SPY EMA today, more strategies as wired in.",
        "href": "/trading",
        "enabled": True,
        "status": None,  # rendered dynamically (see dashboard route)
        "status_class": "status-live",
    },
    {
        "slug": "sap-deals",
        "icon": "🤝",
        "title": "SAP Deals",
        "description": "Active pipeline, campaign tracker, mentor-session prep.",
        "href": "/coming-soon/sap-deals",
        "enabled": False,
        "status": "Coming soon",
        "status_class": "status-soon",
    },
    {
        "slug": "vault",
        "icon": "📚",
        "title": "Vault",
        "description": "Browse recent notes, full-text search, on-demand reindex.",
        "href": "/coming-soon/vault",
        "enabled": False,
        "status": "Coming soon",
        "status_class": "status-soon",
    },
    {
        "slug": "settings",
        "icon": "⚙️",
        "title": "Settings",
        "description": "Cost dashboard, model picker, env config.",
        "href": "/coming-soon/settings",
        "enabled": False,
        "status": "Coming soon",
        "status_class": "status-soon",
    },
]


COMING_SOON_DETAILS: dict[str, dict] = {
    "sap-deals": {
        "icon": "🤝",
        "title": "Deals",
        "description": "Sales pipeline ops cockpit.",
        "planned": [
            "Active deals table sourced from notes",
            "Campaign tracker (accounts + response status)",
            "Per-deal hub with one-click brief",
            "Mentor-session prep using notes context",
            "Account research workflows",
        ],
    },
    "vault": {
        "icon": "📚",
        "title": "Vault",
        "description": "Direct vault navigation without going through chat.",
        "planned": [
            "Full-text search across vault notes",
            "Recent notes feed",
            "Folder browser",
            "On-demand reindex (today this lives in the Brain sidebar)",
            "Note preview (read-only)",
        ],
    },
    "settings": {
        "icon": "⚙️",
        "title": "Settings",
        "description": "Configuration + observability for the dashboard itself.",
        "planned": [
            "Running cost dashboard (per-day, per-thread, per-model)",
            "Model picker (Sonnet vs Opus vs Haiku)",
            "Env var override interface",
            "Vault path config",
            "Request log viewer",
        ],
    },
}


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Tile dashboard — the cockpit landing page.

    v1 has one functional tile (Brain) and several "coming soon" placeholders that
    establish the eventual shape of the cockpit. Each placeholder tile activates as
    real workflows demand it.
    """
    # Compute live status for the Brain tile
    thread_count = len(t.list_threads(limit=1000))
    brain_status = (
        f"{thread_count} thread{'s' if thread_count != 1 else ''}"
        if thread_count > 0
        else "Ready"
    )

    tiles_with_status = []
    for tile in TILES:
        tile_copy = dict(tile)
        if tile["slug"] == "brain":
            tile_copy["status"] = brain_status
        elif tile["slug"] == "x-agent":
            tile_copy["status"] = x_agent.tile_status()
        elif tile["slug"] == "trading":
            tile_copy["status"] = trading.tile_status()
        tiles_with_status.append(tile_copy)

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "tiles": tiles_with_status,
            "vault_path": str(VAULT_PATH),
            "indexed_notes": index_size(),
        },
    )


@app.get("/brain", response_class=HTMLResponse)
async def brain(request: Request, thread: str | None = None):
    """Chat shell. Optional ?thread=<id> pre-loads that thread into the main pane.

    Sidebar loads threads via HTMX; main pane is either welcome screen or the chat pane.
    """
    thread_obj = t.get_thread(thread) if thread else None
    rendered_messages = []
    if thread_obj:
        msgs = t.list_messages(thread_obj.id)
        rendered_messages = [
            {"msg": m, "html": _render_message_html(m)} for m in msgs
        ]
    return templates.TemplateResponse(
        request,
        "brain.html",
        {
            "thread": thread_obj,
            "rendered_messages": rendered_messages,
            "show_welcome": thread_obj is None,
        },
    )


# ---------------------------------------------------------------------------
# X Agent tile
# ---------------------------------------------------------------------------


@app.get("/x-agent", response_class=HTMLResponse)
async def x_agent_page(request: Request, view: str | None = None):
    """X Agent drill-down page. Two cards: brief (cached or generate) and reply bot.

    Query param `?view=YYYY-MM-DD` displays a past brief instead of today's.
    """
    today = x_agent.today_brief_date()
    selected_date = view or today
    selected_brief_md = x_agent.read_brief(selected_date)
    selected_brief_html = (
        _render_markdown_lite(selected_brief_md) if selected_brief_md else None
    )

    return templates.TemplateResponse(
        request,
        "x_agent.html",
        {
            "today": today,
            "selected_date": selected_date,
            "is_today": selected_date == today,
            "today_exists": x_agent.today_brief_exists(),
            "selected_brief_html": selected_brief_html,
            "past_briefs": x_agent.list_briefs(),
            "is_running": x_agent.is_run_in_progress(),
            "accounts": x_agent.ACCOUNTS,
        },
    )


@app.post("/x-agent/brief/generate", response_class=HTMLResponse)
async def x_agent_generate_brief(request: Request):
    """Kick off a brief-generation subprocess. Returns the streaming-output placeholder.

    Semantics:
      - If a generation is already running, returns the same placeholder pointing
        at the existing stream (no second subprocess spawn).
      - Otherwise, clears any prior run state so the SSE endpoint actually runs
        a fresh subprocess (rather than the auto-reconnect guard short-circuiting
        against a previous done state).
    """
    if not x_agent.is_brief_running():
        x_agent.clear_active_run()
    return templates.TemplateResponse(
        request,
        "partials/x_agent_brief_streaming.html",
        {"today": x_agent.today_brief_date()},
    )


@app.get("/x-agent/brief/stream")
async def x_agent_stream_brief():
    """SSE: streams brief subprocess stdout to the placeholder.

    On 'done', sends the rendered final brief HTML for inline display.
    """
    async def event_gen():
        # Accumulate output for the inline display.
        accumulated = ""
        try:
            existing = x_agent.get_active_run()

            # Case 1: a previous run already finished successfully.
            # This happens when SSE auto-reconnects after the server closed the response.
            # Without this guard, the subprocess would loop forever — re-running the brief
            # (and re-spending API + scrape time) on every reconnect.
            # Don't re-run; just resend the final cached brief and tell the client to close.
            if existing and existing.status in ("done", "error", "timeout"):
                final_md = x_agent.read_brief(x_agent.today_brief_date())
                if final_md:
                    rendered = _render_markdown_lite(final_md)
                    final_block = (
                        '<div class="brief-output">'
                        '<details><summary class="muted">Subprocess log</summary>'
                        f'<pre class="subprocess-output">{html.escape(existing.output_buffer)}</pre>'
                        '</details>'
                        f'<div class="brief-rendered message-content">{rendered}</div>'
                        '</div>'
                    )
                else:
                    final_block = (
                        '<div class="brief-output">'
                        f'<div class="error">Last run finished ({existing.status}) but no brief file was written.</div>'
                        f'<pre class="subprocess-output">{html.escape(existing.output_buffer)}</pre>'
                        '</div>'
                    )
                yield f"event: done\ndata: {_sse_payload(final_block)}\n\n"
                return

            # Case 2: a run is currently in progress (user reloaded mid-stream).
            # We can't easily resume an already-yielding generator. Best-effort:
            # show what's accumulated so far and tell the user to wait.
            if existing and existing.status == "running":
                payload = _sse_payload(
                    f'<pre class="subprocess-output">{html.escape(existing.output_buffer)}'
                    "\n[run already in progress — refresh after it completes to see the final brief]</pre>"
                )
                yield f"event: message\ndata: {payload}\n\n"
                yield f"event: done\ndata: {payload}\n\n"
                return

            # Case 3: no prior run state. Actually run the subprocess.
            try:
                async for chunk in x_agent.stream_brief_generation():
                    accumulated += chunk
                    payload = _sse_payload(
                        f'<pre class="subprocess-output">{html.escape(accumulated)}</pre>'
                    )
                    yield f"event: message\ndata: {payload}\n\n"
            except RuntimeError as exc:
                # Race: another request grabbed the lock between the check and the call.
                err = f'<div class="error">{html.escape(str(exc))}</div>'
                yield f"event: done\ndata: {_sse_payload(err)}\n\n"
                return

            # On success, render the final brief markdown inline (if it was written).
            final_md = x_agent.read_brief(x_agent.today_brief_date())
            if final_md:
                rendered = _render_markdown_lite(final_md)
                final_block = (
                    '<div class="brief-output">'
                    '<details open><summary class="muted">Subprocess log</summary>'
                    f'<pre class="subprocess-output">{html.escape(accumulated)}</pre>'
                    '</details>'
                    f'<div class="brief-rendered message-content">{rendered}</div>'
                    '</div>'
                )
            else:
                final_block = (
                    '<div class="brief-output">'
                    '<div class="error">Subprocess finished but no brief file was written. '
                    'Check the log above.</div>'
                    f'<pre class="subprocess-output">{html.escape(accumulated)}</pre>'
                    '</div>'
                )
            yield f"event: done\ndata: {_sse_payload(final_block)}\n\n"

        except Exception as exc:
            err_html = f'<div class="error">Stream failed: {html.escape(str(exc))}</div>'
            yield f"event: done\ndata: {_sse_payload(err_html)}\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# X Agent — Reply bot (scrape FYP + generate drafts for the selected account)
# ---------------------------------------------------------------------------


@app.post("/x-agent/replies/generate", response_class=HTMLResponse)
async def x_agent_run_reply_bot(request: Request, account: str = Form(...)):
    """Kick off a reply-scrape subprocess for the chosen account.

    Returns the SSE-streaming placeholder. Account picker on the page passes
    `account=<key>` form field — must match an entry in x_agent.ACCOUNTS.
    """
    selected = x_agent.get_account(account)
    if selected is None:
        raise HTTPException(400, f"Unknown account: {account!r}")

    if not x_agent.is_run_in_progress():
        x_agent.clear_active_run()

    return templates.TemplateResponse(
        request,
        "partials/x_agent_replies_streaming.html",
        {"account": selected},
    )


@app.get("/x-agent/replies/stream")
async def x_agent_stream_replies(account: str):
    """SSE: stream the reply-scrape subprocess stdout to the placeholder.

    Same auto-reconnect-safe pattern as the brief stream — on done, send the
    final accumulated output and close the connection. State guard prevents
    re-running on auto-reconnect after a successful run.
    """
    selected = x_agent.get_account(account)
    if selected is None:
        raise HTTPException(400, f"Unknown account: {account!r}")

    async def event_gen():
        accumulated = ""
        try:
            existing = x_agent.get_active_run()

            # Case 1: a previous run already finished. Don't re-spawn — just send
            # the cached output and close. (Defends against SSE auto-reconnect loops.)
            if existing and existing.status in ("done", "error", "timeout"):
                final_block = (
                    '<div class="brief-output">'
                    f'<pre class="subprocess-output">{html.escape(existing.output_buffer)}</pre>'
                    '</div>'
                )
                yield f"event: done\ndata: {_sse_payload(final_block)}\n\n"
                return

            # Case 2: a different run is already in-flight (brief, or replies on a
            # different account). Tell the user to wait.
            if existing and existing.status == "running":
                payload = _sse_payload(
                    f'<pre class="subprocess-output">{html.escape(existing.output_buffer)}'
                    f"\n[a {existing.kind} run is already in progress — refresh after it completes]</pre>"
                )
                yield f"event: message\ndata: {payload}\n\n"
                yield f"event: done\ndata: {payload}\n\n"
                return

            # Case 3: no prior run state. Run the scrape subprocess.
            try:
                async for chunk in x_agent.stream_reply_scrape(account):
                    accumulated += chunk
                    payload = _sse_payload(
                        f'<pre class="subprocess-output">{html.escape(accumulated)}</pre>'
                    )
                    yield f"event: message\ndata: {payload}\n\n"
            except RuntimeError as exc:
                err = f'<div class="error">{html.escape(str(exc))}</div>'
                yield f"event: done\ndata: {_sse_payload(err)}\n\n"
                return
            except ValueError as exc:
                err = f'<div class="error">{html.escape(str(exc))}</div>'
                yield f"event: done\ndata: {_sse_payload(err)}\n\n"
                return

            # Final block — show subprocess log + a small "what happens next" pointer
            # to the batch step (which the dashboard doesn't run for v1; user runs in terminal).
            final_block = (
                '<div class="brief-output">'
                f'<pre class="subprocess-output">{html.escape(accumulated)}</pre>'
                '<div class="muted" style="margin-top:12px;font-size:13px;">'
                'Drafts saved to the X agent\'s replies log. To open them in Chrome '
                f'tabs for review, run from terminal: '
                f'<code>~/.venvs/x_agent/bin/python "$HOME/Desktop/X Agent/main.py" batch'
                + (f' --account {selected["account_arg"]}' if selected["account_arg"] else "")
                + '</code>'
                '</div></div>'
            )
            yield f"event: done\ndata: {_sse_payload(final_block)}\n\n"

        except Exception as exc:
            err_html = f'<div class="error">Stream failed: {html.escape(str(exc))}</div>'
            yield f"event: done\ndata: {_sse_payload(err_html)}\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Trading tile — visual regime / strategy widgets
# ---------------------------------------------------------------------------


@app.get("/trading", response_class=HTMLResponse)
async def trading_page(request: Request):
    """Trading drill-down page. One card per registered strategy.

    v1: just VIX/SPY Regime. Pattern is general — adding a strategy is one entry
    in agents/trading.py STRATEGIES, no template or route changes required.
    """
    cards = []
    for s in trading.STRATEGIES:
        latest = trading.latest_output(s["slug"])
        age_seconds = trading.latest_output_age_seconds(s["slug"])
        cards.append({
            **s,
            "latest_output": latest,
            "latest_age_seconds": age_seconds,
            "is_running": trading.is_strategy_running(s["slug"]),
        })
    return templates.TemplateResponse(
        request,
        "trading.html",
        {"strategies": cards},
    )


@app.post("/trading/strategy/{slug}/run", response_class=HTMLResponse)
async def trading_run_strategy(request: Request, slug: str):
    """Kick off a strategy subprocess. Returns the SSE-streaming placeholder.

    Form fields are passed straight through as user_args (resolved into --<name>
    flags by the streaming function). Empty values are skipped, letting the
    strategy use its own defaults.
    """
    strategy = trading.get_strategy(slug)
    if strategy is None:
        raise HTTPException(404, f"Unknown strategy: {slug!r}")

    if not trading.is_strategy_running(slug):
        trading.clear_active_run(slug)

    # Form data comes in as form-encoded fields; collect them all.
    form_data = await request.form()
    user_args = {key: str(value) for key, value in form_data.items()}

    # Build query string for the SSE GET (form values can't be POSTed to SSE).
    from urllib.parse import urlencode
    qs = urlencode({k: v for k, v in user_args.items() if v.strip()})

    return templates.TemplateResponse(
        request,
        "partials/trading_strategy_streaming.html",
        {
            "slug": slug,
            "strategy": strategy,
            "qs": qs,
        },
    )


@app.get("/trading/strategy/{slug}/stream")
async def trading_stream_strategy(request: Request, slug: str):
    """SSE: stream the strategy subprocess stdout to the placeholder.

    On 'done', sends a final HTML block that includes the rendered chart image.
    State guard prevents re-running on SSE auto-reconnect (same anti-loop pattern
    as the X Agent stream endpoints).
    """
    strategy = trading.get_strategy(slug)
    if strategy is None:
        raise HTTPException(404, f"Unknown strategy: {slug!r}")

    # Pull user args back out of the query string (they were posted, then encoded
    # into the SSE-connect URL by the placeholder template).
    user_args = dict(request.query_params)

    async def event_gen():
        accumulated = ""
        try:
            existing = trading.get_active_run(slug)

            # Case 1: previous run already finished. Don't re-spawn — send cached image.
            if existing and existing.status in ("done", "error", "timeout"):
                final_block = _render_strategy_final(strategy, existing.output_buffer, existing.output_image, existing.status)
                yield f"event: done\ndata: {_sse_payload(final_block)}\n\n"
                return

            # Case 2: same strategy still running (user reloaded mid-stream).
            if existing and existing.status == "running":
                payload = _sse_payload(
                    f'<pre class="subprocess-output">{html.escape(existing.output_buffer)}'
                    f"\n[run already in progress — refresh after it completes]</pre>"
                )
                yield f"event: message\ndata: {payload}\n\n"
                yield f"event: done\ndata: {payload}\n\n"
                return

            # Case 3: no prior state — actually run.
            try:
                async for chunk in trading.stream_strategy_run(slug, user_args):
                    accumulated += chunk
                    payload = _sse_payload(
                        f'<pre class="subprocess-output">{html.escape(accumulated)}</pre>'
                    )
                    yield f"event: message\ndata: {payload}\n\n"
            except (RuntimeError, ValueError) as exc:
                err = f'<div class="error">{html.escape(str(exc))}</div>'
                yield f"event: done\ndata: {_sse_payload(err)}\n\n"
                return

            run = trading.get_active_run(slug)
            output_image = run.output_image if run else None
            status = run.status if run else "error"
            final_block = _render_strategy_final(strategy, accumulated, output_image, status)
            yield f"event: done\ndata: {_sse_payload(final_block)}\n\n"

        except Exception as exc:
            err_html = f'<div class="error">Stream failed: {html.escape(str(exc))}</div>'
            yield f"event: done\ndata: {_sse_payload(err_html)}\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream")


def _render_strategy_final(strategy: dict, log_text: str, output_image: str | None, status: str) -> str:
    """Build the 'done' HTML block: subprocess log (collapsed) + chart image (or error)."""
    log_block = (
        '<details><summary class="muted">Subprocess log</summary>'
        f'<pre class="subprocess-output">{html.escape(log_text)}</pre>'
        '</details>'
    )
    if output_image and status == "done":
        # Cache-bust the image URL so a re-run with same filename pattern still
        # forces the browser to fetch the new file.
        img_block = (
            '<div class="strategy-chart-wrapper">'
            f'<img src="/trading/output/{output_image}?t={int(_time.time())}" '
            f'alt="{html.escape(strategy["display_name"])} chart" '
            f'class="strategy-chart"/>'
            '</div>'
        )
        return f'<div class="strategy-output">{img_block}{log_block}</div>'
    else:
        err_block = (
            '<div class="error">Run finished without producing a chart. '
            f'Status: <code>{html.escape(status)}</code>. See log for details.</div>'
        )
        return f'<div class="strategy-output">{err_block}{log_block}</div>'


# ---------------------------------------------------------------------------
# Trading — Futures Map (Robinhood-supported micros + live quotes + sparklines)
# ---------------------------------------------------------------------------


@app.get("/trading/futures", response_class=HTMLResponse)
async def trading_futures_page(request: Request):
    """Futures Map page: grid of Robinhood-supported micros grouped by sector.

    Each card shows ticker, name, last price, day % change (colored), and a
    5-day SVG sparkline. Active positions (per data/active_positions.json) get
    a ★ + border highlight. Quote fetch is yfinance, cached for 30s.
    """
    # Run blocking yfinance call in a thread so we don't block the event loop
    sectors = await asyncio.to_thread(futures.quotes_by_sector)
    return templates.TemplateResponse(
        request,
        "futures_map.html",
        {
            "sectors": sectors,
            "cache_age": futures.cache_age_seconds(),
        },
    )


@app.get("/trading/futures/grid", response_class=HTMLResponse)
async def trading_futures_grid(request: Request, refresh: int = 0):
    """HTMX partial: just the grid + status bar, used by the Refresh button.

    `?refresh=1` busts the 30-second quote cache and forces a fresh yfinance pull.
    """
    sectors = await asyncio.to_thread(futures.quotes_by_sector, bool(refresh))
    return templates.TemplateResponse(
        request,
        "partials/futures_grid.html",
        {
            "sectors": sectors,
            "cache_age": futures.cache_age_seconds(),
        },
    )


@app.get("/trading/output/{filename}")
async def trading_output_image(filename: str):
    """Serve a generated chart PNG. Filenames generated by agents/trading.py are
    timestamped + slug-prefixed so this is bounded to known files only."""
    from config import TRADING_OUTPUT_DIR
    # Defense in depth: only serve files inside TRADING_OUTPUT_DIR; reject any path
    # traversal attempt (filename containing slash or ..).
    if "/" in filename or ".." in filename:
        raise HTTPException(400, "invalid filename")
    file_path = TRADING_OUTPUT_DIR / filename
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(404, "image not found")
    return FileResponse(file_path, media_type="image/png")


# ---------------------------------------------------------------------------
# JSON API — surface for the war-room MCP server (and any other JSON consumer)
# ---------------------------------------------------------------------------
# These endpoints return structured JSON instead of rendered HTML. The browser
# UI continues to use the existing HTML routes (/trading/futures, /brain, etc.).
# The MCP server at mcp_server.py wraps these endpoints so other Claude
# sessions (work mac, mobile, Claude Desktop) can query the dashboard's state.
#
# Design rule: read-only by default. The one write-adjacent endpoint
# (POST /api/strategy/{slug}/run) triggers a subprocess but doesn't change
# persistent state beyond the chart cache the dashboard already maintains.


@app.get("/api/futures")
async def api_futures(refresh: int = 0):
    """JSON: all 11 RH-supported futures (last price + day change + sector + active flag).

    Mirrors the data behind /trading/futures (the HTML Futures Map) but without
    sparkline SVGs — those are presentation-only and the JSON consumer can
    compute its own. Cached for 30s same as the HTML route.
    """
    sectors = await asyncio.to_thread(futures.quotes_by_sector, bool(refresh))
    return {
        "cache_age_seconds": futures.cache_age_seconds(),
        "sectors": [
            {
                "name": sector_name,
                "contracts": [
                    {
                        "micro": q.micro,
                        "full_symbol": q.full,
                        "display_name": q.display,
                        "sector": q.sector,
                        "last": q.last,
                        "change": q.change,
                        "change_pct": q.change_pct,
                        "is_active": q.is_active,
                        "error": q.error,
                    }
                    for q in quotes
                ],
            }
            for sector_name, quotes in sectors
        ],
    }


@app.get("/api/active-positions")
async def api_active_positions():
    """JSON: current active futures positions from data/active_positions.json."""
    active = futures.load_active_positions()
    # Enrich with display name + sector by joining against the universe
    universe = {f["micro"]: f for f in futures.FUTURES_UNIVERSE}
    return {
        "active": [
            {
                "micro": m,
                "display_name": universe.get(m, {}).get("display", m),
                "sector": universe.get(m, {}).get("sector"),
                "full_symbol": universe.get(m, {}).get("full"),
            }
            for m in sorted(active)
        ],
    }


@app.get("/api/vault/search")
async def api_vault_search(q: str, n: int = 5, snippet_chars: int = 400):
    """JSON: FTS5 vault search wrapped for MCP consumers.

    Returns top N matches with path, name, rank, snippet (first `snippet_chars`
    of content), and truncated flag. The MCP tool description tells the caller
    that this is for *finding* notes — full content of a specific note is a
    follow-up filesystem read, not part of this response.
    """
    if not q.strip():
        raise HTTPException(400, "empty query")
    if n < 1 or n > 50:
        raise HTTPException(400, "n must be between 1 and 50")
    results = await asyncio.to_thread(search, q, n)
    return {
        "query": q,
        "count": len(results),
        "results": [
            {
                "name": r.name,
                "path": r.path,
                "rank": r.rank,
                "snippet": r.content[:snippet_chars],
                "truncated": r.truncated or len(r.content) > snippet_chars,
                "obsidian_url": _obsidian_url(r.path),
            }
            for r in results
        ],
    }


@app.get("/api/strategies")
async def api_list_strategies():
    """JSON: registered trading strategies with metadata."""
    cards = []
    for s in trading.STRATEGIES:
        latest = trading.latest_output(s["slug"])
        age = trading.latest_output_age_seconds(s["slug"])
        cards.append({
            "slug": s["slug"],
            "display_name": s["display_name"],
            "description": s["description"],
            "args": [{"name": a["name"], "label": a["label"], "default": a["default"]} for a in s.get("args", [])],
            "latest_output": latest,
            "latest_age_seconds": age,
        })
    return {"strategies": cards}


def _parse_strategy_log(log: str) -> dict:
    """Best-effort extraction of score / regime / state from a strategy's stdout.

    Strategies print in different formats; this regex-grabs what's common.
    Returns dict with as many keys as it can pull. MCP consumers get a
    consistent JSON shape with None for missing fields rather than parse errors.
    """
    summary: dict[str, str | float | None] = {
        "last_bar": None,
        "close": None,
        "score": None,
        "state": None,
        "adx": None,
        "gate_status": None,
    }
    m = re.search(r"Last valid bar\s+(\d{4}-\d{2}-\d{2}):\s+Close=([\d.]+)\s+Score=([\d.]+)\s*\((.+?)\)", log)
    if m:
        summary["last_bar"] = m.group(1)
        summary["close"] = float(m.group(2))
        summary["score"] = float(m.group(3))
        summary["state"] = m.group(4).strip()
    m = re.search(r"ADX value:\s+([\d.]+)\s+\(Threshold=([\d.]+)\)\s*->\s*Gate:\s*(\w+)", log)
    if m:
        summary["adx"] = float(m.group(1))
        summary["gate_status"] = m.group(3)
    return summary


@app.post("/api/strategy/{slug}/run")
async def api_run_strategy(slug: str, request: Request):
    """JSON: synchronously run a strategy and return log + chart filename + parsed summary.

    Blocks until the subprocess finishes (bounded by TRADING_RUN_TIMEOUT_SECONDS).
    Body is JSON {arg_name: value, ...} (e.g. {"ticker": "SPY", "donchian-weight": "0.4"}).
    Empty body = use strategy defaults.

    Same locking + state machinery as the browser SSE route — if another run is
    in progress for this slug, returns 409.
    """
    strategy = trading.get_strategy(slug)
    if strategy is None:
        raise HTTPException(404, f"unknown strategy: {slug!r}")

    if trading.is_strategy_running(slug):
        raise HTTPException(409, f"strategy {slug!r} already running")
    trading.clear_active_run(slug)

    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("body must be a JSON object")
        user_args = {str(k): str(v) for k, v in body.items()}
    except Exception:
        user_args = {}

    accumulated = ""
    try:
        async for chunk in trading.stream_strategy_run(slug, user_args):
            accumulated += chunk
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(500, str(exc))

    run = trading.get_active_run(slug)
    output_image = run.output_image if run else None
    status = run.status if run else "error"

    return {
        "slug": slug,
        "display_name": strategy["display_name"],
        "status": status,
        "user_args": user_args,
        "log": accumulated,
        "summary": _parse_strategy_log(accumulated),
        "chart_filename": output_image,
        "chart_url": f"/trading/output/{output_image}" if output_image else None,
    }


# ---------------------------------------------------------------------------
# Coming-soon placeholders (remaining tiles)
# ---------------------------------------------------------------------------


@app.get("/coming-soon/{slug}", response_class=HTMLResponse)
async def coming_soon(request: Request, slug: str):
    """Placeholder page for tiles that haven't been built yet.

    Shows the planned-features list so future-Xander knows the scope when he reaches
    for the tile and finds it not yet wired up.
    """
    details = COMING_SOON_DETAILS.get(slug)
    if details is None:
        raise HTTPException(404, f"unknown tile: {slug}")
    return templates.TemplateResponse(
        request,
        "coming_soon.html",
        {
            "icon": details["icon"],
            "title": details["title"],
            "description": details["description"],
            "planned": details["planned"],
        },
    )


@app.get("/threads", response_class=HTMLResponse)
async def list_threads_partial(request: Request):
    """Sidebar partial: list of threads."""
    threads = t.list_threads()
    return templates.TemplateResponse(
        request, "partials/thread_list.html", {"threads": threads}
    )


@app.post("/threads", response_class=HTMLResponse)
async def create_thread(request: Request):
    """Create a new thread and redirect to its chat pane (under /brain)."""
    new_thread = t.create_thread(title="New chat")
    # HTMX: HX-Redirect tells the browser to do a full page-load redirect.
    response = HTMLResponse(content="", status_code=200)
    response.headers["HX-Redirect"] = f"/brain?thread={new_thread.id}"
    return response


@app.get("/threads/{thread_id}", response_class=HTMLResponse)
async def get_thread(request: Request, thread_id: str):
    """Chat pane for a thread. Used by HTMX when sidebar items are clicked."""
    thread = t.get_thread(thread_id)
    if thread is None:
        raise HTTPException(404, "thread not found")
    messages = t.list_messages(thread_id)
    rendered_messages = [
        {"msg": m, "html": _render_message_html(m)} for m in messages
    ]
    return templates.TemplateResponse(
        request,
        "partials/chat_pane.html",
        {"thread": thread, "rendered_messages": rendered_messages},
    )


@app.post("/threads/{thread_id}/messages", response_class=HTMLResponse)
async def send_message(request: Request, thread_id: str, content: str = Form(...)):
    """Save the user message, create an assistant placeholder, return HTML for both.

    The assistant placeholder includes an SSE-connect attribute pointing at the
    /messages/{mid}/stream endpoint — HTMX-SSE picks it up and streams tokens in.
    """
    thread = t.get_thread(thread_id)
    if thread is None:
        raise HTTPException(404, "thread not found")

    content = content.strip()
    if not content:
        return HTMLResponse(content="", status_code=204)

    # Persist user message
    user_msg = t.append_message(thread_id, "user", content)

    # If this is the thread's first message, derive its title from the content
    existing = t.list_messages(thread_id)
    if len(existing) == 1:  # the user message we just inserted is the only one
        t.update_thread_title(thread_id, derive_title_from_first_message(content))

    # Retrieval — done synchronously so we can pass to the streaming task
    retrieval_results = search(content)

    # Create assistant placeholder. Content empty; will be filled by the streaming task.
    asst_sources = [Source(path=r.path, name=r.name) for r in retrieval_results]
    asst_msg = t.append_message(thread_id, "assistant", "", sources=asst_sources)

    # Register inflight stream context — picked up by the SSE endpoint
    history = [m for m in t.list_messages(thread_id) if m.id != asst_msg.id]
    _inflight[asst_msg.id] = {
        "history": history[:-1],  # all messages BEFORE the new user message
        "new_user_content": content,
        "retrieval": retrieval_results,
        "thread_id": thread_id,
        "buffer": "",
        "input_tokens": 0,
        "output_tokens": 0,
        "done": False,
    }

    # Render: user message (final) + assistant placeholder (will be filled by SSE)
    return templates.TemplateResponse(
        request,
        "partials/message_pair.html",
        {
            "user_msg": user_msg,
            "user_html": _render_message_html(user_msg),
            "asst_msg": asst_msg,
            "asst_sources": asst_sources,
            "obsidian_url": _obsidian_url,
        },
    )


@app.get("/messages/{message_id}/stream")
async def stream_message(message_id: str):
    """SSE endpoint. Streams accumulated assistant text as HTML fragments.

    Each event sends the FULL accumulated content so far (HTMX-SSE swaps the
    target's content on each event, so deltas would replace previous text).
    On 'done', persists final state to SQLite and closes the stream.
    """
    ctx = _inflight.get(message_id)
    if ctx is None:
        raise HTTPException(404, "no inflight stream for this message")

    async def event_gen():
        try:
            stream = stream_response(
                history=ctx["history"],
                new_user_content=ctx["new_user_content"],
                retrieval=ctx["retrieval"],
            )
            async for evt in stream:
                if evt.type == "text":
                    ctx["buffer"] += evt.text
                    payload = _sse_payload(_render_markdown_lite(ctx["buffer"]))
                    yield f"event: message\ndata: {payload}\n\n"
                elif evt.type == "done":
                    ctx["input_tokens"] = evt.input_tokens
                    ctx["output_tokens"] = evt.output_tokens
                    # Persist to SQLite
                    asst_sources = [
                        Source(path=r.path, name=r.name) for r in ctx["retrieval"]
                    ]
                    t.update_message_content(
                        message_id=message_id,
                        content=ctx["buffer"],
                        sources=asst_sources,
                        input_tokens=evt.input_tokens,
                        output_tokens=evt.output_tokens,
                    )
                    # Send the final rendered text. Replaces inner content of message-content.
                    # Cost/meta will appear on next page load (via canonical render).
                    final_payload = _sse_payload(_render_markdown_lite(ctx["buffer"]))
                    yield f"event: done\ndata: {final_payload}\n\n"
                    ctx["done"] = True
                    break
        except Exception as exc:  # surface errors inline rather than dropping silently
            err_html = (
                f'<div class="error">Stream failed: '
                f"{html.escape(str(exc))}</div>"
            )
            yield f"event: done\ndata: {_sse_payload(err_html)}\n\n"
        finally:
            _inflight.pop(message_id, None)

    return StreamingResponse(event_gen(), media_type="text/event-stream")


def _sse_payload(html_str: str) -> str:
    """SSE data: lines must not contain raw newlines. Replace \\n with empty string for inline data.
    HTMX will swap exactly this string into the target element."""
    # Replace newlines (we already converted them to <br> in markdown-lite)
    return html_str.replace("\n", "").replace("\r", "")


@app.post("/reindex", response_class=HTMLResponse)
async def reindex(request: Request):
    """Drop + rebuild the vault FTS5 index. Returns a small HTML status block."""
    stats = await asyncio.to_thread(reindex_vault)
    return HTMLResponse(
        content=(
            f'<div class="reindex-status">'
            f"Indexed <strong>{stats['indexed']}</strong> notes in "
            f"{stats['elapsed_seconds']}s."
            f"</div>"
        )
    )


@app.get("/health")
async def health():
    return {"status": "ok", "indexed_notes": index_size(), "vault": str(VAULT_PATH)}


# ---------------------------------------------------------------------------
# Entrypoint hint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host=HOST, port=PORT, reload=False)
