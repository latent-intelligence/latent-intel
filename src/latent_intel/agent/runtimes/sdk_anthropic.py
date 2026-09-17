"""The Anthropic Messages protocol again, with the loop run by the SDK.

`custom.py` on a `messages` row and this module speak to the same hosts with the same
credentials and the same options; they differ in who owns the agent loop. There the
`for` loop, the transcript bookkeeping and the tool-result assembly are ours. Here
they are `client.beta.messages.tool_runner`'s, and what stays ours is the part that
should: **tool execution still runs through `turn.dispatch`**, reached by the runner
through `agent/bridge.py`, so every attached source is served by our router and
reported as the same pair of events.

**Why offload a loop we already have.** The line count barely moves. What moves is
everything arriving next: the ceiling, prompt caching, compaction and context editing
are parameters on the runner rather than surgery on a loop, and the first feature we
would otherwise have written by hand is the argument for this module existing.

**The relay is why tool events appear when they do.** The runner executes tools inside
its own `__anext__`, where our generator has no `yield` in scope, so the pair is held
and drained at the top of the next round — see `agent/bridge.py` for the cost and the
trigger for making it live. Order is not affected: `Emitter` stamps `sequence` when the
event is built.

**Caching is on.** A repeated system prompt and a growing transcript are re-read on
every round of every turn, and an ephemeral cache breakpoint is the largest single cost
lever this runtime has. It is a top-level parameter rather than a host-row flag because
both rows are the same API; if Foundry turns out to reject it — the open item on the
Foundry spike — it becomes a row flag and nothing else changes.

**One difference from the custom loop, on purpose.** The runner resolves a tool name
against the tools it was given, so a name the model invented never reaches our router
and no `ToolStarted`/`ToolResult` pair is emitted for it; the model is still told the
tool was not found. `custom.py` reports such a call as a failed pair. Reproducing that
here would mean second-guessing the runner's dispatch, which is the part we chose to
hand over.

The rules `custom.py` states apply here verbatim: the SDK is imported inside the turn
and never at module scope, reasons name variables and never their values, and every
path ends with exactly one `AgentCompleted` or `AgentFailed`.
"""

from __future__ import annotations

import importlib.util
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from ... import events as ev
from ...models import HostStatus, Message, RuntimeUnavailable, ToolSpec
from .. import bridge, hosts, turn
from ..protocols.messages import STOP_DONE, STOP_FAILURES

#: Every host reachable through the Anthropic SDK: the rows of `hosts.HOSTS` that name
#: this SDK. A new one is a row there, not a line here.
HOSTS = hosts.for_sdk("anthropic")

#: Foundry first: the deployment this was written for runs on Azure, and a default that
#: is right for the common case beats one that is right for nobody.
DEFAULT_HOST = "foundry-anthropic"

#: Its own variable, not derived from the kind: a machine may well want the runner
#: against Foundry and the custom loop against the public API while comparing them.
ENV_HOST = "LATENT_INTEL_SDK_ANTHROPIC_HOST"

#: Undated on purpose — see `hosts.HOSTS["foundry-anthropic"].remedy_404`. Taken from
#: the default host's row rather than written again here: a model default is the row's
#: to declare now that it is a field, and two places saying it are two places to
#: disagree. Every row on this protocol declares one — `tests/test_hosts.py` is what
#: holds that — and the check below is what tells mypy so without a second literal;
#: a check rather than an assert, because `python -O` strips asserts and would leave
#: this constant None while typed `str`.
_default_model = HOSTS[DEFAULT_HOST].default_model
if _default_model is None:
    raise RuntimeError(f"host row '{DEFAULT_HOST}' declares no default model")
DEFAULT_MODEL: str = _default_model
del _default_model


class SdkAnthropicRuntime:
    """The Anthropic SDK's tool runner, behind the `Runtime` Protocol."""

    id = "sdk-anthropic"

    def __init__(
        self,
        *,
        model: str | None = None,
        host: str | None = None,
        approval: str = "ask",
        max_tokens: int = turn.DEFAULT_MAX_TOKENS,
        max_tool_rounds: int = turn.DEFAULT_MAX_TOOL_ROUNDS,
        client_factory: Callable[[], Any] | None = None,
        **unknown: Any,
    ) -> None:
        # Rejected, not ignored, for the reason `custom.py` gives: a key nothing reads
        # is a setting the file says is on and no one honours.
        if unknown:
            raise RuntimeUnavailable(
                f"unknown option(s) for runtime '{self.id}': "
                f"{', '.join(sorted(unknown))} — see `runtimes: {self.id}:` in config"
            )
        self.model = model or DEFAULT_MODEL
        self.host = host or os.environ.get(ENV_HOST) or DEFAULT_HOST
        self.approval = approval
        self.max_tokens = int(max_tokens)
        self.max_tool_rounds = int(max_tool_rounds)
        self.client_factory = client_factory

    # -- diagnosis ------------------------------------------------------------

    def available(self) -> bool:
        return self.unavailable_reason() is None

    def unavailable_reason(self) -> str | None:
        """Why this cannot run here, in the order someone would fix it. Same rows, same
        variables and same order as `custom.py` on a `messages` row — the two runtimes
        differ in who drives the loop, not in what a machine has to have."""
        host = HOSTS.get(self.host)
        if host is None:
            return hosts.unknown(self.host, HOSTS)
        if importlib.util.find_spec("anthropic") is None:
            return "the anthropic SDK is not installed — install `latent-intel[api]`"
        return hosts.diagnose(host, name=self.host)

    def host_status(self) -> dict[str, HostStatus]:
        """Every host this runtime declares, and what each still needs. See
        `hosts.status`."""
        return hosts.status(HOSTS)

    def family(self) -> str:
        """A vendor's runner, driven in this process. See `agent/base.Owned`."""
        return "sdk"

    # -- one turn -------------------------------------------------------------

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        *,
        emitter: ev.Emitter,
        **options: Any,
    ) -> AsyncIterator[ev.AgentEvent]:
        reason = self.unavailable_reason()
        if reason is not None:
            yield emitter.emit(
                ev.AgentFailed,
                message=reason,
                kind="runtime_unavailable",
                remedy="`intel doctor` lists what each runtime needs",
            )
            return

        # `find_spec` said the SDK is there; a half-installed one can still fail here,
        # and that failure has to be an event like every other.
        try:
            import anthropic
        except ImportError as exc:
            yield emitter.emit(
                ev.AgentFailed,
                message=f"the anthropic SDK could not be imported: {exc}",
                kind="runtime_unavailable",
                remedy="reinstall `latent-intel[api]`",
            )
            return

        host = HOSTS[self.host]
        try:
            async for event in self._turn(messages, tools, emitter=emitter, **options):
                yield event
        except Exception as exc:  # noqa: BLE001 — a failure is an event, not a crash
            # Cancellation derives from BaseException and is deliberately not caught:
            # a cancelled turn has to close the stream rather than report itself.
            yield emitter.emit(
                ev.AgentFailed,
                **turn.failure(
                    exc,
                    sdk=anthropic,
                    host=host,
                    name=self.host,
                    model=self.model,
                ),
            )

    async def _turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        *,
        emitter: ev.Emitter,
        **options: Any,
    ) -> AsyncIterator[ev.AgentEvent]:
        """The runner's loop, translated. `stream` is only the exception vocabulary.

        Ends with exactly one terminal event on every path it returns from; an SDK
        exception leaves through `stream`, which supplies the terminal event instead.
        """
        # Inside the turn, never at module scope — `available_kinds()` loads every
        # registered runtime, and a base install has no `anthropic` to import.
        from anthropic.lib.tools import BetaAsyncBuiltinFunctionTool, ToolError

        call_tool = options.get("call_tool")
        sources = options.get("sources") or []
        relay = bridge.Relay()

        class _Tool(BetaAsyncBuiltinFunctionTool):
            """One wired tool, as the runner's own tool type.

            `to_dict` is the definition `protocols/messages.py` sends; `call` is our
            router. `ToolError` is how the runner is told a call failed — it becomes a
            `tool_result` with `is_error: True`, which is what makes a failed call
            something the model can respond to rather than a dead turn.
            """

            def __init__(
                self,
                name: str,
                spec: ToolSpec,
                run: Callable[[dict[str, Any]], Awaitable[str]],
            ) -> None:
                self._name = name
                self._spec = spec
                self._run = run

            def to_dict(self) -> Any:
                return {
                    "name": self._name,
                    "description": self._spec.description,
                    "input_schema": turn.input_schema(self._spec),
                }

            async def call(self, input: object) -> str:
                # The API sends an object, and a tool that declares no parameters is
                # sent `{}`; anything else is not arguments and is not passed on.
                arguments = dict(input) if isinstance(input, dict) else {}
                try:
                    return await self._run(arguments)
                except bridge.BridgeError as exc:
                    raise ToolError(str(exc)) from exc

        # Raises on a wire-name collision, before a client exists and before anything
        # is sent; `stream` turns it into the one terminal event this turn gets.
        wired = turn.wired(tools, self.approval)
        definitions = [
            _Tool(
                name,
                spec,
                bridge.bridged(spec, emitter=emitter, call_tool=call_tool, relay=relay),
            )
            for name, spec in wired
        ]
        system = turn.system_prompt(sources)
        # Text only. A prior turn's tool blocks are not replayed: they refer to
        # `tool_use_id`s from a request this one never made.
        #
        # An empty one is dropped rather than sent: the API rejects a non-final
        # assistant message with empty content, so one turn that completed with no text
        # would fail every later question in the session.
        transcript: list[dict[str, Any]] = [
            {"role": message.role, "content": message.text}
            for message in messages
            if message.text.strip()
        ]
        usage = turn.UsageTotals()
        began = time.monotonic()
        answer: list[str] = []
        separate = False
        stop: str | None = None
        rounds = 0

        async with self._client() as client:
            request: dict[str, Any] = {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "messages": transcript,
                "tools": definitions,
                "max_iterations": self.max_tool_rounds,
                "stream": True,
                # The largest cost lever this runtime has — see the module docstring.
                "cache_control": {"type": "ephemeral"},
            }
            if system:
                request["system"] = system

            runner = client.beta.messages.tool_runner(**request)
            if not definitions:
                # `tools=` is required by `tool_runner`, so an empty turn cannot simply
                # omit it the way the messages adapter does — and the API rejects an
                # empty tool list. The SDK accepts the empty list and would send
                # `tools: []`, so the key is dropped from the params the runner uses.
                runner.set_messages_params(
                    lambda params: {k: v for k, v in params.items() if k != "tools"}
                )

            async for round_stream in runner:
                rounds += 1
                # The previous round's tool calls ran inside the runner, after our last
                # yield and before this request opened. This is the first moment their
                # events can leave.
                for held in relay.drain():
                    yield held
                async for event in round_stream:
                    if event.type != "text":
                        continue
                    # Text after a tool round is a new block; run straight on from the
                    # text before the call it reads "…let me check.Here is". A paused
                    # turn resumes mid-sentence and gets no break.
                    if separate and answer:
                        answer.append("\n\n")
                        yield emitter.emit(ev.AssistantToken, text="\n\n")
                    separate = False
                    answer.append(event.text)
                    yield emitter.emit(ev.AssistantToken, text=event.text)
                final = await round_stream.get_final_message()
                usage.add(final.usage)
                stop = final.stop_reason
                separate = stop == "tool_use"

        # The ceiling round's tools run after the runner's last yield, so their events
        # are still held here.
        for held in relay.drain():
            yield held

        elapsed = int((time.monotonic() - began) * 1000)

        # A runner that yielded nothing made no request and read no stop reason, so
        # there is no last message to classify — and an empty answer is not one.
        if rounds == 0:
            yield emitter.emit(
                ev.AgentFailed,
                message="the runner ended without making a request",
                kind="unexpected_stop",
                remedy="ask again",
            )
            return

        if stop in STOP_DONE:
            yield emitter.emit(
                ev.AgentCompleted,
                text="".join(answer),
                streamed=True,
                usage=usage.totals(elapsed),
            )
            return

        # No stop reason at all is a stream that was cut — by a proxy, by a host that
        # ended the response early — and calling it success reports whatever arrived
        # before the cut as the whole answer.
        if stop is None:
            yield emitter.emit(
                ev.AgentFailed,
                message="the stream ended without a stop reason",
                kind="unexpected_stop",
                remedy="ask again",
            )
            return

        # The runner exits silently when `max_iterations` is reached, so a last message
        # it would otherwise have continued from — a tool request, a paused turn, a
        # compaction — is the ceiling and not a vocabulary we failed to read.
        if stop in ("tool_use", "pause_turn", "compaction"):
            yield emitter.emit(
                ev.AgentFailed,
                message=f"stopped after {self.max_tool_rounds} rounds of tool calls",
                kind="tool_rounds",
                remedy="raise `max_tool_rounds` under this runtime, or ask for less",
            )
            return

        message, remedy = STOP_FAILURES.get(
            str(stop), (f"the model stopped: {stop}", "")
        )
        yield emitter.emit(
            ev.AgentFailed, message=message, kind=str(stop), remedy=remedy
        )

    def _client(self) -> Any:
        """The SDK client, as the async context manager the SDK itself returns. Same
        rows and same construction as `custom.py` — see `_client` there."""
        if self.client_factory is not None:
            return self.client_factory()
        import anthropic

        host = HOSTS[self.host]
        return getattr(anthropic, host.client)(
            **host.construct(host, hosts.values(host))
        )


__all__ = ["ENV_HOST", "SdkAnthropicRuntime"]
