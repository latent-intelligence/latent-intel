"""Our agent loop, over any host in the table.

A runtime names **who owns the agent loop**. `anthropic` and `openai` broke that rule:
they named a wire protocol, which is a property of the endpoint rather than of the
loop. Both ran this loop and differed only in transcript shape, stream reading and
stop-reason vocabulary — so the flat list read as four loop owners where there were
two, and the two copies had already drifted. They were folded into this module on
2026-09-17 and removed rather than aliased.

**The protocol is on the host row.** `hosts.Host.protocol` picks the adapter, so
reaching a new endpoint is a row in `agent/hosts.py` and nothing else: no module, no
branch here, and no second table. A future canonical loop — a research loop, say —
joins the `custom` family and reuses both the rows and the adapters.

**What an adapter owns, and what this owns.** The adapter spells a tool definition,
builds the transcript, builds the request, reads the stream and sends a result back.
Everything else is here or in `agent/turn.py`: the round bound, the paragraph rule,
tool dispatch, usage, and the one terminal event. See `agent/protocols/`.

**No default host.** Seven rows across two protocols make any default right for at most
one deployment, so an unset host is reported by name the way an unset model is.
`LATENT_INTEL_CUSTOM_HOST` is the override, and `intel hosts` is the list.

The rules the two folded modules stated apply here verbatim:

- **The SDK is imported inside the turn, never at module scope.** `available_kinds()`
  loads every registered runtime, so an import here would make a base install without
  the `api` extra lose `claude-cli` as well. `unavailable_reason()` answers with
  `importlib.util.find_spec` and `os.environ` and makes no network call — `intel
  doctor` calls it for every installed runtime, and a probe that dialled out would make
  diagnosis slower than the thing being diagnosed.
- **Names of variables, never their values.** The reason a runtime cannot run is
  printed by `doctor`, pasted into support threads, and read aloud in screen shares.
- **One terminal event on every path.** A tool call that fails, a stop reason we do not
  recognise, an SDK exception — each ends the turn with exactly one `AgentCompleted` or
  `AgentFailed`. A frontend iterating this over a transport has nowhere to catch an
  exception, and a turn that ends with neither hangs a renderer waiting for one.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

from ... import events as ev
from ...models import HostStatus, Message, RuntimeUnavailable, ToolSpec
from .. import hosts, turn
from ..protocols import Adapter, Call, Outcome
from ..protocols.chat import ChatAdapter
from ..protocols.messages import MessagesAdapter

#: Every endpoint this build can reach. This runtime speaks both protocols, so it sees
#: the whole table rather than one SDK's subset.
HOSTS = hosts.HOSTS

#: No default: see the module docstring.
ENV_HOST = "LATENT_INTEL_CUSTOM_HOST"

#: One stateless instance per protocol, chosen by the row. Nothing about a round
#: survives the `Outcome` an adapter returns, so there is nothing to build per turn.
ADAPTERS: dict[str, Adapter] = {
    "messages": MessagesAdapter(),
    "chat": ChatAdapter(),
}


class CustomRuntime:
    """Our loop, behind the `Runtime` Protocol."""

    id = "custom"

    def __init__(
        self,
        *,
        model: str | None = None,
        host: str | None = None,
        approval: str = "ask",
        max_tokens: int = turn.DEFAULT_MAX_TOKENS,
        max_tool_rounds: int = turn.DEFAULT_MAX_TOOL_ROUNDS,
        tokens_param: str | None = None,
        client_factory: Callable[[], Any] | None = None,
        **unknown: Any,
    ) -> None:
        # Rejected, not ignored: a key this runtime never reads is a setting the file
        # says is on and nothing honours — a typo in `model:` is the common case, and
        # it used to build a runtime silently running on the default.
        if unknown:
            raise RuntimeUnavailable(
                f"unknown option(s) for runtime '{self.id}': "
                f"{', '.join(sorted(unknown))} — see `runtimes: {self.id}:` in config"
            )
        #: Option beats environment, and there is no default. The environment is how one
        #: machine runs against Foundry and another against the public API with the same
        #: project file checked out on both.
        self.host = host or os.environ.get(ENV_HOST) or ""
        row = HOSTS.get(self.host)
        #: Resolved here rather than at request time, so what a frontend reports is what
        #: answers: `Session.runtime_setting` reads this attribute, and a bare `/model`
        #: in the shell printing "unset" beside a row that has one is a report that
        #: contradicts the runtime. A chat row declares no default and leaves this
        #: empty, which `unavailable_reason` then says offline.
        self.model = model or (row.default_model if row else None) or ""
        self.approval = approval
        self.max_tokens = int(max_tokens)
        self.max_tool_rounds = int(max_tool_rounds)
        #: The output-token parameter, overriding the row's. The honest lever for a
        #: resource whose API version accepts only the older name — and refused rather
        #: than dropped on a row whose protocol has one name for it.
        self.tokens_param = tokens_param
        #: The test seam, and the only one. Returns the SDK client as an async context
        #: manager, exactly as the real constructor does.
        self.client_factory = client_factory

    # -- diagnosis ------------------------------------------------------------

    def available(self) -> bool:
        return self.unavailable_reason() is None

    def family(self) -> str:
        """Our own loop, in this process. See `agent/base.Owned`."""
        return "custom"

    def unavailable_reason(self) -> str | None:
        """Why this cannot run here, in the order someone would fix it.

        Host first, because without one there is no row and the rest is meaningless;
        then the SDK that row is reached through, because no variable helps without it;
        then the variables, by name; then the model, which is the one thing no
        environment can supply; then a setting the chosen host does not take.
        """
        if not self.host:
            return (
                "no host is set — set `host:` under `runtimes: custom:`; "
                "`intel hosts` lists them"
            )
        row = HOSTS.get(self.host)
        if row is None:
            return hosts.unknown(self.host, HOSTS)
        if importlib.util.find_spec(row.sdk) is None:
            return f"the {row.sdk} SDK is not installed — install `latent-intel[api]`"
        if (reason := hosts.diagnose(row, name=self.host)) is not None:
            return reason
        # The row's own default was applied in the constructor, so an empty model here
        # is a row that declares none and a deployment that named none either.
        if not self.model:
            return (
                "no model is set — set `model:` under `runtimes: custom:`, because "
                "every host names its models differently and there is no default"
            )
        # Refused rather than silently dropped: a setting the file says is on and
        # nothing honours is worse than one that is missing.
        if self.tokens_param and row.protocol == "messages":
            return (
                f"`tokens_param` is set, but host '{self.host}' speaks the Anthropic "
                f"Messages protocol and does not take it — remove it, or choose a "
                f"chat host"
            )
        return None

    def host_status(self) -> dict[str, HostStatus]:
        """Every host this runtime declares, and what each still needs.

        The whole table, not the configured row: `intel hosts` asks this of a machine
        that has not chosen yet. See `hosts.status`.
        """
        return hosts.status(HOSTS)

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

        row = HOSTS[self.host]
        # `find_spec` said the SDK is there; a half-installed one can still fail here,
        # and that failure has to be an event like every other. By name, because which
        # SDK this is is the row's to say.
        try:
            sdk = importlib.import_module(row.sdk)
        except ImportError as exc:
            yield emitter.emit(
                ev.AgentFailed,
                message=f"the {row.sdk} SDK could not be imported: {exc}",
                kind="runtime_unavailable",
                remedy="reinstall `latent-intel[api]`",
            )
            return

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
                    sdk=sdk,
                    host=row,
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
        """The loop itself, so `stream` is only the exception vocabulary.

        Ends with exactly one terminal event on every path it returns from; an SDK
        exception leaves through `stream`, which supplies the terminal event instead.
        """
        call_tool = options.get("call_tool")
        sources = options.get("sources") or []
        row = HOSTS[self.host]
        adapter = ADAPTERS[row.protocol]

        # Raises on a wire-name collision, before a client exists and before anything
        # is sent; `stream` turns it into the one terminal event this turn gets.
        wired = turn.wired(tools, self.approval)
        by_name = dict(wired)
        definitions = adapter.definitions(wired)
        system = turn.system_prompt(sources)
        transcript = adapter.transcript(messages, system)
        # The runtime's override, then the row's, then whatever the adapter defaults
        # to. Read here rather than in the adapter, which owns no rows.
        tokens_param = self.tokens_param or getattr(row, "tokens_param", None)
        usage = turn.UsageTotals()
        began = time.monotonic()
        answer: list[str] = []
        separate = False

        async with self._client() as client:
            for _ in range(self.max_tool_rounds):
                request = adapter.request(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    tokens_param=tokens_param,
                    transcript=transcript,
                    definitions=definitions,
                    system=system,
                )

                outcome: Outcome | None = None
                async for item in adapter.round(client, request):
                    if isinstance(item, Outcome):
                        outcome = item
                        continue
                    # Text after a tool round is a new block; run straight on from the
                    # text before the call it reads "…let me check.Here is". A paused
                    # turn resumes mid-sentence and gets no break.
                    if separate and answer:
                        answer.append("\n\n")
                        yield emitter.emit(ev.AssistantToken, text="\n\n")
                    separate = False
                    answer.append(item)
                    yield emitter.emit(ev.AssistantToken, text=item)
                # Exactly one per round is the Protocol; an adapter that breaks it
                # leaves through `stream` as the turn's one failure.
                assert outcome is not None, f"the {row.protocol} adapter yielded none"

                usage.add_counts(**outcome.counts)
                transcript.append(outcome.assistant)

                if outcome.status == "failed":
                    yield emitter.emit(
                        ev.AgentFailed,
                        message=outcome.message,
                        kind=outcome.kind,
                        remedy=outcome.remedy,
                    )
                    return

                if outcome.status == "continue":
                    continue

                if outcome.status == "tools":
                    ran: list[tuple[Call, ev.ToolResult]] = []
                    for call in outcome.calls:
                        spec = by_name.get(call.name)
                        # Arguments that are not a JSON object are the model's mistake
                        # to fix and are never routed: the raising router turns that
                        # into a result the model is shown rather than a dead turn.
                        router = (
                            call_tool
                            if call.arguments is not None
                            else turn.invalid_arguments
                        )
                        started, result = await turn.dispatch(
                            router,
                            spec,
                            source_id=spec.source_id if spec else "",
                            name=spec.name if spec else call.name,
                            arguments=call.arguments or {},
                            emitter=emitter,
                        )
                        yield started
                        yield result
                        ran.append((call, result))
                    transcript += adapter.results(ran)
                    separate = True
                    continue

                elapsed = int((time.monotonic() - began) * 1000)
                yield emitter.emit(
                    ev.AgentCompleted,
                    text="".join(answer),
                    streamed=True,
                    usage=usage.totals(elapsed),
                )
                return

        yield emitter.emit(
            ev.AgentFailed,
            message=f"stopped after {self.max_tool_rounds} rounds of tool calls",
            kind="tool_rounds",
            remedy="raise `max_tool_rounds` under this runtime, or ask for less",
        )

    def _client(self) -> Any:
        """The SDK client, as the async context manager the SDK itself returns.

        The values are resolved here rather than left to the SDK to read, so a fallback
        or a default the row declares is honoured by the client and not only by the
        reason. They are passed and never logged — which is also why the test seam
        replaces the whole client rather than the key. Which SDK, which class and which
        kwargs are all the row's to say: see `sdk`, `client` and `construct`.
        """
        if self.client_factory is not None:
            return self.client_factory()
        row = HOSTS[self.host]
        sdk = importlib.import_module(row.sdk)
        return getattr(sdk, row.client)(**row.construct(row, hosts.values(row)))


__all__ = ["ADAPTERS", "ENV_HOST", "HOSTS", "CustomRuntime"]
