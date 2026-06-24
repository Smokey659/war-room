"""Anthropic SDK wrapper. Streaming + system prompt + retrieval-context injection.

Builds the message list including:
  - Prior conversation history (multi-turn)
  - Retrieved vault notes as system context
  - The current user message

Returns a streaming response (token-by-token) suitable for SSE.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Iterable

from anthropic import Anthropic

from config import ANTHROPIC_API_KEY, MODEL
from conversations.threads import Message
from retrieval.search import SearchResult


# Single client instance — Anthropic SDK is happy to be reused.
_client = Anthropic(api_key=ANTHROPIC_API_KEY)


SYSTEM_PROMPT = """You are Xander's Second Brain assistant. You help him quickly find and synthesize information from his Obsidian vault during work — particularly for SAP customer calls, deal prep, account research, trading decisions, and ongoing project work.

You have access to vault notes that match the current question (provided below as VAULT CONTEXT). Use them as your primary source of truth. If the answer requires combining information from multiple notes, do so explicitly.

CITATION RULE: When you reference information from a note, cite it inline using the format [[note-name]] (using the note's display name, NOT the file path). The interface renders these as clickable links back to the note in Obsidian.

HONESTY RULES:
- If the vault doesn't contain enough information to answer, say so directly. Don't fabricate.
- If the vault contains conflicting information, surface the conflict.
- Distinguish what you know from the vault vs. what you're inferring.
- If the user asks something the retrieved context doesn't address, tell them — don't pretend the context covered it.

STYLE:
- Keep responses tight. Long-form synthesis is welcome when the question warrants it; don't pad simple questions.
- Match Xander's preference for direct, specific answers over hedging.
- When citing dollar amounts, names, dates from the vault, do so verbatim.
"""


@dataclass
class StreamEvent:
    """One streaming event from Claude. type='text' → token chunk; type='done' → finalize."""
    type: str
    text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


def _build_context_block(results: list[SearchResult]) -> str:
    """Format retrieved notes as a single string for the user message preamble."""
    if not results:
        return "(No vault notes matched this query — answer from general knowledge if helpful, but flag that the vault didn't have direct context.)"
    parts = ["--- VAULT CONTEXT (top matches by BM25) ---", ""]
    for r in results:
        parts.append(f"### [[{r.name}]] (path: {r.path})")
        parts.append(r.content)
        parts.append("")
    return "\n".join(parts)


def _build_anthropic_messages(
    history: Iterable[Message],
    new_user_content: str,
    retrieval: list[SearchResult],
) -> list[dict]:
    """Assemble the messages array for the Anthropic API.

    The retrieved vault context is prepended to the new user message so it scopes
    only the current question. Prior turns keep their original content (no re-injection
    of stale context).
    """
    msgs: list[dict] = []
    for m in history:
        msgs.append({"role": m.role, "content": m.content})

    context = _build_context_block(retrieval)
    user_with_context = (
        f"{context}\n\n"
        f"--- USER QUESTION ---\n\n"
        f"{new_user_content}"
    )
    msgs.append({"role": "user", "content": user_with_context})
    return msgs


def stream_response(
    history: Iterable[Message],
    new_user_content: str,
    retrieval: list[SearchResult],
) -> AsyncIterator[StreamEvent]:
    """Stream Claude's response token-by-token. Yields StreamEvent.

    Implementation note: the Anthropic SDK's streaming API is sync-iterator based.
    We wrap it with an async generator so the FastAPI SSE endpoint can iterate cleanly.
    """
    messages = _build_anthropic_messages(history, new_user_content, retrieval)

    async def gen() -> AsyncIterator[StreamEvent]:
        # Use the SDK's streaming context manager. Iterate text events and forward them.
        with _client.messages.stream(
            model=MODEL,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=messages,
        ) as stream:
            for chunk in stream.text_stream:
                yield StreamEvent(type="text", text=chunk)
            # Final usage data
            final = stream.get_final_message()
            yield StreamEvent(
                type="done",
                input_tokens=final.usage.input_tokens,
                output_tokens=final.usage.output_tokens,
            )

    return gen()
