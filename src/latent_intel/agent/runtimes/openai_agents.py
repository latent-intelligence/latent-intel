"""The OpenAI-compatible protocol again, with the loop run by the Agents SDK.

`openai.py` and this module speak to the same five hosts with the same credentials and
the same options; they differ in who owns the agent loop. There the `for` loop, the
transcript bookkeeping and the reassembly of streamed tool-call fragments are ours.
Here they are `Runner.run_streamed`'s, and what stays ours is the part that should:
**tool execution still runs through `turn.dispatch`**, reached by the runner through
`agent/bridge.py`, so every attached source is served by our router and reported as the
same pair of events.

This is the wider reach of the two SDK runtimes — one runner across the OpenAI API,
OpenRouter, Foundry, classic Azure OpenAI and a server on the machine you are sitting
at, because `OpenAIChatCompletionsModel` takes the exact client `hosts.py` rows already
construct. It is also the framework commitment, which is why the SDK is its own extra:
see `agents` in `pyproject.toml`.

**Tracing is disabled twice, deliberately.** The SDK traces by default and exports
those traces to OpenAI using `OPENAI_API_KEY` — a client's transcript leaving for a
third party because a framework defaulted it, and a paid call nobody asked for. The
global switch is thrown after the import and `RunConfig(tracing_disabled=True)` is
passed per run, so neither an import order nor a later global change can re-enable it
for a turn this build made.

**The relay is why tool events appear when they do.** The runner executes tools inside
its own task, where our generator has no `yield` in scope, so the pair is held and
drained when the run reports the tool's output — see `agent/bridge.py` for the cost and
the trigger for making it live. Order is not affected: `Emitter` stamps `sequence` when
the event is built.

**Differences from the custom loop, on purpose — each a conformance finding.**

- *A finish reason is not reported.* `openai.py` reads `length` and `content_filter`
  off the chat completion and fails the turn with a remedy. The SDK does not surface
  either through its stream, so an answer truncated by the output limit **completes
  here**. The custom runtime is the one that reports it, and that difference belongs in
  the conformance matrix rather than in a guess made here.
- *An invented tool name emits no event pair.* The SDK resolves a tool call against
  the agent's tools, so a name the model invented never reaches our router. Its default
  is to raise and end the turn; this runtime sets `tool_not_found_behavior` to return
  the error to the model instead, so the model is told and the turn carries on — the
  same shape as `sdk_anthropic.py`, and the rule `turn.dispatch` states: one bad call
  must not end a turn. `openai.py` reports such a call as a failed pair.
- *The ceiling is raised, not silent.* The Anthropic runner exits without a signal when
  `max_iterations` is reached, so `sdk_anthropic.py` reads the ceiling off a last
  message still asking for tools. This runner raises `MaxTurnsExceeded` instead, which
  is caught by name — there is no silent stop to reconstruct.

The rules `openai.py` states apply here verbatim: both SDKs are imported inside the turn
and never at module scope, reasons name variables and never their values, and every path
ends with exactly one `AgentCompleted` or `AgentFailed`.
"""

from __future__ import annotations

import importlib.util
import os
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

from ... import events as ev
from ...models import HostStatus, Message, RuntimeUnavailable, ToolSpec
from .. import bridge, hosts, turn
from .openai import (
    DEFAULT_HOST,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MAX_TOOL_ROUNDS,
    HOSTS,
    arguments,
)

#: Its own variable, not derived from the kind: a machine may well want the framework
#: against one host and the custom loop against another while comparing them.
ENV_HOST = "LATENT_INTEL_OPENAI_AGENTS_HOST"


class OpenAIAgentsRuntime:
    """The OpenAI Agents SDK, behind the `Runtime` Protocol."""

    id = "openai-agents"

    def __init__(
        self,
        *,
        model: str | None = None,
        host: str | None = None,
        approval: str = "ask",
        max_tokens: int = DEFAULT_MAX_TOKENS,
        max_tool_rounds: int = DEFAULT_MAX_TOOL_ROUNDS,
        tokens_param: str | None = None,
        client_factory: Callable[[], Any] | None = None,
        runner_factory: Callable[[], Any] | None = None,
        **unknown: Any,
    ) -> None:
        # Rejected, not ignored, for the reason `openai.py` gives: a key nothing reads
        # is a setting the file says is on and no one honours.
        if unknown:
            raise RuntimeUnavailable(
                f"unknown option(s) for runtime '{self.id}': "
                f"{', '.join(sorted(unknown))} — see `runtimes: {self.id}:` in config"
            )
        self.model = model or ""
        self.host = host or os.environ.get(ENV_HOST) or DEFAULT_HOST
        self.approval = approval
        self.max_tokens = int(max_tokens)
        self.max_tool_rounds = int(max_tool_rounds)
        self.tokens_param = tokens_param
        #: The client seam, as in `openai.py`.
        self.client_factory = client_factory
        #: The runner seam, and the reason a test never reaches a network: it returns
        #: the object whose `run_streamed` is called, which in production is
        #: `agents.Runner` itself. Faking the runner rather than the model is what keeps
        #: the tool bridge, the relay and the stream translation under test.
        self.runner_factory = runner_factory

    # -- diagnosis ------------------------------------------------------------

    def available(self) -> bool:
        return self.unavailable_reason() is None

    def unavailable_reason(self) -> str | None:
        """Why this cannot run here, in the order someone would fix it.

        Host first, then the two packages — the client SDK before the framework,
        because the framework's own error for a missing `openai` names neither this
        install nor the extra that fixes it — then the variables, then the model.
        """
        host = HOSTS.get(self.host)
        if host is None:
            return hosts.unknown(self.host, HOSTS)
        if importlib.util.find_spec("openai") is None:
            return "the openai SDK is not installed — install `latent-intel[api]`"
        if importlib.util.find_spec("agents") is None:
            return (
                "the openai-agents SDK is not installed — install "
                "`latent-intel[agents]`"
            )
        if (reason := hosts.diagnose(host, name=self.host)) is not None:
            return reason
        if not self.model:
            return (
                "no model is set — set `model:` under `runtimes: openai-agents:`, "
                "because every host names its models differently and there is no "
                "default"
            )
        return None

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

        # `find_spec` said both are there; a half-installed one can still fail here,
        # and that failure has to be an event like every other. Two imports and two
        # remedies, because they come from two extras.
        try:
            import openai
        except ImportError as exc:
            yield emitter.emit(
                ev.AgentFailed,
                message=f"the openai SDK could not be imported: {exc}",
                kind="runtime_unavailable",
                remedy="reinstall `latent-intel[api]`",
            )
            return
        try:
            import agents
        except ImportError as exc:
            yield emitter.emit(
                ev.AgentFailed,
                message=f"the openai-agents SDK could not be imported: {exc}",
                kind="runtime_unavailable",
                remedy="reinstall `latent-intel[agents]`",
            )
            return

        host = HOSTS[self.host]
        try:
            async for event in self._turn(
                agents, messages, tools, emitter=emitter, **options
            ):
                yield event
        except Exception as exc:  # noqa: BLE001 — a failure is an event, not a crash
            # Cancellation derives from BaseException and is deliberately not caught:
            # a cancelled turn has to close the stream rather than report itself. An
            # `AgentsException` the ladder does not name lands as `runtime_error` with
            # its own message, which is the honest report for a framework fault.
            yield emitter.emit(
                ev.AgentFailed,
                **turn.failure(
                    exc,
                    sdk=openai,
                    host=host,
                    name=self.host,
                    model=self.model,
                ),
            )

    async def _turn(
        self,
        agents: Any,
        messages: list[Message],
        tools: list[ToolSpec],
        *,
        emitter: ev.Emitter,
        **options: Any,
    ) -> AsyncIterator[ev.AgentEvent]:
        """The run's stream, translated. `stream` is only the exception vocabulary.

        Ends with exactly one terminal event on every path it returns from; an SDK
        exception leaves through `stream`, which supplies the terminal event instead.
        """
        # Global and per-run, before anything can trace: see the module docstring.
        agents.set_tracing_disabled(True)

        call_tool = options.get("call_tool")
        sources = options.get("sources") or []
        relay = bridge.Relay()
        host = HOSTS[self.host]

        # Raises on a wire-name collision, before a client exists and before anything
        # is sent; `stream` turns it into the one terminal event this turn gets.
        wired = turn.wired(tools, self.approval)
        definitions = [
            self._tool(
                agents, name, spec, emitter=emitter, relay=relay, router=call_tool
            )
            for name, spec in wired
        ]
        system = turn.system_prompt(sources)
        # Text only. A prior turn's tool items are not replayed: they refer to call ids
        # from a request this one never made.
        #
        # An empty one is dropped rather than sent: a turn that completed with no text
        # records an empty assistant message, and a host that rejects one would fail
        # every later question in the session.
        transcript: list[dict[str, Any]] = [
            {"role": message.role, "content": message.text}
            for message in messages
            if message.text.strip()
        ]
        usage = turn.UsageTotals()
        began = time.monotonic()
        answer: list[str] = []
        separate = False

        async with self._client() as client:
            runner = self.runner_factory() if self.runner_factory else agents.Runner
            result = runner.run_streamed(
                agents.Agent(
                    name="latent-intel",
                    instructions=system or None,
                    tools=definitions,
                    model=agents.OpenAIChatCompletionsModel(
                        model=self.model, openai_client=client
                    ),
                    model_settings=self._settings(agents, host),
                ),
                input=transcript,
                # `current_turn` is incremented and then compared with `>`, so N here
                # permits N model calls — the same bound `max_tool_rounds` is.
                max_turns=self.max_tool_rounds,
                run_config=agents.RunConfig(
                    tracing_disabled=True,
                    # A name the model invented is the model's to fix, and a model told
                    # nothing repeats it. The default raises and ends the turn.
                    tool_not_found_behavior="return_error_to_model",
                ),
            )
            try:
                async for event in result.stream_events():
                    # Switched on by string, never `isinstance` against an SDK class:
                    # a renamed class upstream would otherwise silently stop matching,
                    # and a fake would need the SDK's event types to be testable.
                    kind = getattr(event, "type", "")
                    if kind == "raw_response_event":
                        data = event.data
                        if getattr(data, "type", "") == "response.output_text.delta":
                            # Text after a tool round is a new block; run straight on
                            # from the text before the call it reads "…let me
                            # check.Here is".
                            if separate and answer:
                                answer.append("\n\n")
                                yield emitter.emit(ev.AssistantToken, text="\n\n")
                            separate = False
                            answer.append(data.delta)
                            yield emitter.emit(ev.AssistantToken, text=data.delta)
                        elif getattr(data, "type", "") == "response.completed":
                            self._count(usage, getattr(data.response, "usage", None))
                    elif (
                        kind == "run_item_stream_event"
                        and getattr(event, "name", "") == "tool_output"
                    ):
                        # The tool ran inside the runner, after our last yield. This is
                        # the first moment its events can leave.
                        for held in relay.drain():
                            yield held
                        separate = True
            except agents.MaxTurnsExceeded:
                # The ceiling. The SDK drains the queued events before re-raising, so
                # the last round's tool pair is on the relay and has to come out before
                # the terminal event — a turn that ends with a silent gap in the middle
                # is one nobody can read.
                for held in relay.drain():
                    yield held
                yield emitter.emit(
                    ev.AgentFailed,
                    message=(
                        f"stopped after {self.max_tool_rounds} rounds of tool calls"
                    ),
                    kind="tool_rounds",
                    remedy=(
                        "raise `max_tool_rounds` under this runtime, or ask for less"
                    ),
                )
                return

        for held in relay.drain():
            yield held

        elapsed = int((time.monotonic() - began) * 1000)
        text = "".join(answer)
        # A host that streamed no text deltas still produced an answer the run assembled
        # — reported as the answer it is, and marked unstreamed so a renderer prints it
        # rather than assuming the tokens already went past.
        final = getattr(result, "final_output", None)
        if not text and isinstance(final, str) and final.strip():
            yield emitter.emit(
                ev.AgentCompleted,
                text=final,
                streamed=False,
                usage=usage.totals(elapsed),
            )
            return
        yield emitter.emit(
            ev.AgentCompleted,
            text=text,
            streamed=True,
            usage=usage.totals(elapsed),
        )

    def _tool(
        self,
        agents: Any,
        name: str,
        spec: ToolSpec,
        *,
        emitter: ev.Emitter,
        relay: bridge.Relay,
        router: turn.ToolRouter | None,
    ) -> Any:
        """One wired tool, as the runner's own tool type.

        `strict_json_schema` is off because a connector's schema is whatever the source
        declared, and strict mode rejects most of them outright.

        A failure is *returned*, not raised: a `FunctionTool` built by hand has no
        failure handler, so an exception out of `on_invoke_tool` ends the whole run,
        while a returned string becomes the tool's output verbatim — which is what puts
        `result.error or result.output` in front of the model, the same text
        `openai.py` sends back in its `tool` message.
        """

        async def on_invoke_tool(ctx: Any, raw: str) -> str:
            # The protocol's rule, shared with `openai.py`: arguments that are not a
            # JSON object are the model's mistake to fix and are never routed.
            decoded, per_call = arguments(raw, router)
            try:
                return await bridge.run(
                    spec,
                    emitter=emitter,
                    relay=relay,
                    call_tool=per_call,
                    arguments=decoded,
                )
            except bridge.BridgeError as exc:
                return str(exc)

        return agents.FunctionTool(
            name=name,
            description=spec.description,
            params_json_schema=turn.input_schema(spec),
            on_invoke_tool=on_invoke_tool,
            strict_json_schema=False,
        )

    def _settings(self, agents: Any, host: hosts.OpenAIHost) -> Any:
        """The run's model settings: the output-token limit under the row's own
        parameter name, and usage asked for explicitly.

        The SDK hardcodes `max_tokens`, which reasoning deployments reject — so a row
        whose parameter is `max_completion_tokens` sends it through `extra_args`, which
        the chat model merges last, with `max_tokens` left unset so nothing is sent
        under both names. The `tokens_param` precedent, honoured rather than dropped.

        `include_usage` is not a default anywhere but the official OpenAI base URL: the
        SDK only sends `stream_options` unasked when the client points at
        `api.openai.com`, so every other row — OpenRouter, Foundry, Azure, a local
        server — would report nothing about what a turn cost.
        """
        param = self.tokens_param or host.tokens_param
        if param == "max_tokens":
            return agents.ModelSettings(include_usage=True, max_tokens=self.max_tokens)
        return agents.ModelSettings(
            include_usage=True,
            max_tokens=None,
            extra_args={param: self.max_tokens},
        )

    def _count(self, usage: turn.UsageTotals, reported: Any) -> None:
        """One round's usage, from the Responses-shaped payload the chat stream handler
        synthesises. Counted even when the host reported none, so `num_turns` is the
        number of round-trips rather than the number that answered the question."""
        details = getattr(reported, "input_tokens_details", None)
        usage.add_counts(
            input_tokens=getattr(reported, "input_tokens", None),
            output_tokens=getattr(reported, "output_tokens", None),
            cache_read_input_tokens=getattr(details, "cached_tokens", None),
            cache_creation_input_tokens=getattr(details, "cache_write_tokens", None),
        )

    def _client(self) -> Any:
        """The SDK client, as the async context manager the SDK itself returns. Same
        rows and same construction as `openai.py` — see `_client` there."""
        if self.client_factory is not None:
            return self.client_factory()
        import openai

        host = HOSTS[self.host]
        return getattr(openai, host.client)(**host.construct(host, hosts.values(host)))


__all__ = ["ENV_HOST", "OpenAIAgentsRuntime"]
