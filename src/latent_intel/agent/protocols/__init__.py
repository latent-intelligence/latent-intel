"""What a wire protocol decides about a turn — and nothing else.

An **adapter** is one endpoint shape, as the five things a loop cannot work out for
itself: how a tool definition is spelled, how a transcript is built, what the request
looks like, how the stream reads, and how a tool result is sent back. Everything else
about a turn — which tools may be offered, what the model is told it is looking at, the
round bound, the paragraph rule, usage, dispatch — is vendor-neutral and lives in
`agent/turn.py` and the loop that uses it.

**The loop is in `runtimes/custom.py`, once.** `anthropic.py` and `openai.py` were two
copies of it that differed only in this file's worth of protocol, and the copies had
already drifted: a stop reason handled in one and not the other is a difference nobody
chose. An adapter is handed a client and a request and hands back text and an
`Outcome`; it owns no loop, no bookkeeping and no events.

**A row says which adapter.** `hosts.Host.protocol` is `messages` or `chat`, declared
rather than read off the SDK — an OpenAI-compatible gateway in front of Anthropic
models is reached through the `openai` SDK and speaks `chat`.
"""

from __future__ import annotations

from .base import Adapter, Call, Outcome

__all__ = ["Adapter", "Call", "Outcome"]
