"""The Anthropic Messages protocol, in process, as an event source.

The other half of the trade `claude-cli` makes. That runtime borrows the auth a user
already has and gives up our tool router in exchange; this one needs credentials in the
environment and gets the router back — so every attached source is visible to the model,
not only the ones that can be served as an MCP subprocess. A `files` directory and a
`vector` index are tools here and were invisible there.

**A host is a row, not a module.** A host serves this protocol, and the rows are
Foundry and the Anthropic API: they differ in which client class the SDK builds and
which environment variables name the endpoint, and in nothing else about a turn.
Bedrock and Vertex are future rows. A second *protocol* — OpenAI's — is a new runtime
module instead, reusing `agent/turn.py`, which is where the vendor-neutral half of this
loop already lives.

**The SDK is imported inside the turn, never at module scope.** `available_kinds()`
loads every registered runtime, so an import here would make a base install without the
`api` extra lose `claude-cli` as well. `unavailable_reason()` answers with
`importlib.util.find_spec` and `os.environ` and makes no network call — `intel doctor`
calls it for every installed runtime, and a probe that dialled out would make diagnosis
slower than the thing being diagnosed.

**Names of variables, never their values.** The reason a runtime cannot run is printed
by `doctor`, pasted into support threads, and read aloud in screen shares.

**One terminal event on every path.** A tool call that fails, a stop reason we do not
recognise, an SDK exception — each ends the turn with exactly one `AgentCompleted` or
`AgentFailed`. A frontend iterating this over a transport has nowhere to catch an
exception, and a turn that ends with neither hangs a renderer waiting for one.
"""

from __future__ import annotations

import importlib.util
import os
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from ... import events as ev
from ...models import Message, ToolSpec
from .. import turn


@dataclass(frozen=True)
class Host:
    """One endpoint the Anthropic SDK can reach.

    Everything host-specific about a turn is in these five fields, which is the
    claim the design rests on: adding Bedrock is a row here, not a module.
    """

    #: The client class on the `anthropic` module. Resolved by name so the SDK stays
    #: unimported until a turn actually starts.
    client: str
    #: The credential variable. Its *name* is reported when authentication fails.
    key: str
    #: Where the endpoint lives. Any one of these is enough.
    endpoint: tuple[str, ...]
    #: Whether one of `endpoint` must be set for the client to be constructible at all,
    #: or whether the SDK has a default and these are only overrides.
    endpoint_required: bool
    #: What a 404 most likely means here. The two hosts fail differently enough
    #: that one shared sentence would be wrong for both.
    remedy_404: str


#: Every host reachable through the Anthropic SDK. A new one is a row.
HOSTS: dict[str, Host] = {
    "foundry": Host(
        client="AsyncAnthropicFoundry",
        key="ANTHROPIC_FOUNDRY_API_KEY",
        endpoint=("ANTHROPIC_FOUNDRY_RESOURCE", "ANTHROPIC_FOUNDRY_BASE_URL"),
        endpoint_required=True,
        remedy_404=(
            "Foundry resolves deployment names, not dated model ids — try "
            "`claude-sonnet-5` rather than a name with a date on the end, and check "
            "the deployment exists on this resource"
        ),
    ),
    "anthropic": Host(
        client="AsyncAnthropic",
        key="ANTHROPIC_API_KEY",
        endpoint=("ANTHROPIC_BASE_URL",),
        endpoint_required=False,
        remedy_404="check the model id against the ones the API publishes",
    ),
}

#: Foundry first: the deployment this was written for runs on Azure, and a default that
#: is right for the common case beats one that is right for nobody.
DEFAULT_HOST = "foundry"
ENV_HOST = "LATENT_INTEL_ANTHROPIC_HOST"

#: Undated on purpose — see `HOSTS["foundry"].remedy_404`.
DEFAULT_MODEL = "claude-sonnet-5"

DEFAULT_MAX_TOKENS = 16384

#: How many model round-trips one question may take. A bound rather than a budget: a
#: model that loops calling the same tool would otherwise spend until someone noticed.
DEFAULT_MAX_TOOL_ROUNDS = 10

#: Stop reasons that end a turn without an answer, each with what to do about it. A
#: reason absent from here and not in the completing set is reported under its own name
#: rather than guessed at — a vocabulary this build does not know is not a success.
_STOP_FAILURES: dict[str, tuple[str, str]] = {
    "max_tokens": (
        "the model reached its output limit before finishing",
        "raise `max_tokens` under this runtime, or ask a narrower question",
    ),
    "refusal": (
        "the model declined to answer",
        "rephrase the question",
    ),
    "model_context_window_exceeded": (
        "the conversation no longer fits the model's context window",
        "start a new session, or attach fewer sources",
    ),
}

#: Stop reasons that mean the model finished saying what it had to say.
_STOP_DONE = frozenset({"end_turn", "stop_sequence"})


class AnthropicRuntime:
    """The Anthropic SDK, behind the `Runtime` Protocol."""

    id = "anthropic"

    def __init__(
        self,
        *,
        model: str | None = None,
        host: str | None = None,
        approval: str = "ask",
        max_tokens: int = DEFAULT_MAX_TOKENS,
        max_tool_rounds: int = DEFAULT_MAX_TOOL_ROUNDS,
        client_factory: Callable[[], Any] | None = None,
        **_: Any,
    ) -> None:
        self.model = model or DEFAULT_MODEL
        #: Option beats environment beats default. The environment is how one machine
        #: runs against Foundry and another against the public API with the same
        #: project file checked out on both.
        self.host = host or os.environ.get(ENV_HOST) or DEFAULT_HOST
        self.approval = approval
        self.max_tokens = int(max_tokens)
        self.max_tool_rounds = int(max_tool_rounds)
        #: The test seam, and the only one. Returns the SDK client as an async context
        #: manager, exactly as the real constructor does.
        self.client_factory = client_factory

    # -- diagnosis ------------------------------------------------------------

    def available(self) -> bool:
        return self.unavailable_reason() is None

    def unavailable_reason(self) -> str | None:
        """Why this cannot run here, in the order someone would fix it.

        Host first, because an unknown one makes the rest meaningless; then the SDK,
        because no variable helps without it; then the variables, by name. A single
        "cannot run here" sent people into this file to find out which of four things
        was missing.
        """
        host = HOSTS.get(self.host)
        if host is None:
            known = ", ".join(sorted(HOSTS))
            return f"unknown host '{self.host}' — known hosts: {known}"
        if importlib.util.find_spec("anthropic") is None:
            return "the anthropic SDK is not installed — install `latent-intel[api]`"
        missing: list[str] = []
        if not os.environ.get(host.key):
            missing.append(host.key)
        if host.endpoint_required and not any(
            os.environ.get(name) for name in host.endpoint
        ):
            missing.append(" or ".join(host.endpoint))
        if missing:
            return f"set {', '.join(missing)} for host '{self.host}'"
        return None

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
        # Most specific first: authentication, not-found and rate-limit all subclass
        # APIStatusError, so a broader clause above them would swallow all three.
        except anthropic.AuthenticationError:
            yield emitter.emit(
                ev.AgentFailed,
                message=f"the '{self.host}' endpoint rejected the credentials",
                kind="auth",
                remedy=f"check {host.key} — we report its name, never its value",
            )
        except anthropic.NotFoundError:
            yield emitter.emit(
                ev.AgentFailed,
                message=(
                    f"'{self.model}' was not found on the '{self.host}' "
                    f"endpoint"
                ),
                kind="model_not_found",
                remedy=host.remedy_404,
            )
        except anthropic.RateLimitError:
            yield emitter.emit(
                ev.AgentFailed,
                message="the endpoint is rate limiting this key",
                kind="rate_limit",
                remedy="wait and ask again, or raise the deployment's quota",
            )
        except anthropic.APIStatusError as exc:
            yield emitter.emit(
                ev.AgentFailed,
                message=f"the endpoint returned {exc.status_code}",
                kind="api_error",
                remedy="",
            )
        except anthropic.APIConnectionError:
            yield emitter.emit(
                ev.AgentFailed,
                message=f"could not reach the '{self.host}' endpoint",
                kind="connection",
                remedy=f"check {' or '.join(host.endpoint)} and the network",
            )
        except Exception as exc:  # noqa: BLE001 — a failure is an event, not a crash
            # Cancellation derives from BaseException and is deliberately not caught:
            # a cancelled turn has to close the stream rather than report itself.
            yield emitter.emit(
                ev.AgentFailed,
                message=str(exc) or type(exc).__name__,
                kind="runtime_error",
                remedy="",
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

        offered = [
            (turn.wire_name(spec), spec)
            for spec in turn.offered(tools, self.approval)
        ]
        by_name = dict(offered)
        definitions = [
            {
                "name": name,
                "description": spec.description,
                "input_schema": turn.input_schema(spec),
            }
            for name, spec in offered
        ]
        system = turn.system_prompt(sources)
        # Text only. A prior turn's tool blocks are not replayed: they refer to
        # `tool_use_id`s from a request this one never made.
        transcript: list[dict[str, Any]] = [
            {"role": message.role, "content": message.text} for message in messages
        ]
        usage = turn.UsageTotals()
        began = time.monotonic()
        answer: list[str] = []
        separate = False

        async with self._client() as client:
            for _ in range(self.max_tool_rounds):
                request: dict[str, Any] = {
                    "model": self.model,
                    "max_tokens": self.max_tokens,
                    "messages": transcript,
                }
                # Omitted rather than sent empty: the API rejects an empty tool list,
                # and an empty system prompt is a wasted instruction.
                if system:
                    request["system"] = system
                if definitions:
                    request["tools"] = definitions

                async with client.messages.stream(**request) as rounds:
                    async for event in rounds:
                        if event.type != "text":
                            continue
                        # Text after a tool round is a new block; run straight on from
                        # the text before the call it reads "…let me check.Here is".
                        # A paused turn resumes mid-sentence and gets no break.
                        if separate and answer:
                            answer.append("\n\n")
                            yield emitter.emit(ev.AssistantToken, text="\n\n")
                        separate = False
                        answer.append(event.text)
                        yield emitter.emit(ev.AssistantToken, text=event.text)
                    final = await rounds.get_final_message()

                usage.add(final.usage)
                # Verbatim, thinking blocks included: a thinking block returned without
                # its signature is rejected on the next request.
                transcript.append({"role": "assistant", "content": final.content})
                stop = final.stop_reason

                if stop == "tool_use":
                    results: list[dict[str, Any]] = []
                    for block in final.content:
                        if getattr(block, "type", "") != "tool_use":
                            continue
                        spec = by_name.get(str(block.name))
                        started, result = await turn.dispatch(
                            call_tool,
                            spec,
                            source_id=spec.source_id if spec else "",
                            name=spec.name if spec else str(block.name),
                            arguments=dict(block.input or {}),
                            emitter=emitter,
                        )
                        yield started
                        yield result
                        results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                # The model is told what failed; a failed call it
                                # cannot see is one it repeats.
                                "content": result.error or result.output,
                                "is_error": not result.ok,
                            }
                        )
                    transcript.append({"role": "user", "content": results})
                    separate = True
                    continue

                if stop == "pause_turn":
                    continue  # a long-running turn the API asks us to resume

                elapsed = int((time.monotonic() - began) * 1000)
                if stop is None or stop in _STOP_DONE:
                    yield emitter.emit(
                        ev.AgentCompleted,
                        text="".join(answer),
                        streamed=True,
                        usage=usage.totals(elapsed),
                    )
                    return

                message, remedy = _STOP_FAILURES.get(
                    str(stop), (f"the model stopped: {stop}", "")
                )
                yield emitter.emit(
                    ev.AgentFailed, message=message, kind=str(stop), remedy=remedy
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

        Credentials are read by the SDK from the environment and never passed through
        here — which is also why the test seam replaces the whole client rather than
        the key.
        """
        if self.client_factory is not None:
            return self.client_factory()
        import anthropic

        return getattr(anthropic, HOSTS[self.host].client)()


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_MAX_TOOL_ROUNDS",
    "DEFAULT_MODEL",
    "ENV_HOST",
    "HOSTS",
    "AnthropicRuntime",
    "Host",
]
