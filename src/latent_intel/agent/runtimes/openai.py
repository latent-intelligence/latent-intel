"""The OpenAI-compatible protocol, in process, as an event source.

The sibling of `anthropic.py`, and the reason `agent/turn.py` exists: the tool gate, the
system prompt, the paired tool events and the usage totals are shared, and what is left
here is one wire format — a transcript of `role`/`content` dicts, tool calls arriving as
fragments to be reassembled, and a `finish_reason` instead of a stop reason.

**A host is a row, not a module.** A host serves this protocol, and the rows are
Foundry's OpenAI-compatible surface and the OpenAI API: they differ in which variables
name the credential and the endpoint, how a base URL is built, and what a 404 means —
and in nothing else about a turn. OpenRouter, classic Azure OpenAI and a local server
are future rows here rather than future modules.

**One Foundry resource has one key.** A deployment that already reaches Foundry over the
Anthropic protocol has `ANTHROPIC_FOUNDRY_API_KEY` and `ANTHROPIC_FOUNDRY_RESOURCE` set,
and the same values are what this surface needs — so they are read as a fallback rather
than asked for twice under a second name. `ANTHROPIC_FOUNDRY_BASE_URL` is deliberately
not among them: it points at the other surface.

**No default model.** Every host names its models differently — an id on one, a
deployment name someone chose on another — so a default would be right on at most one
of them and would fail at the first question everywhere else. It is a config value, and
`unavailable_reason()` says so offline rather than letting `doctor` report a runtime as
ready that cannot answer.

**The SDK is imported inside the turn, never at module scope.** `available_kinds()`
loads every registered runtime, so an import here would make a base install without the
`api` extra lose `claude-cli` as well. `unavailable_reason()` answers with
`importlib.util.find_spec` and `os.environ` and makes no network call — `intel doctor`
calls it for every installed runtime, and a probe that dialled out would make diagnosis
slower than the thing being diagnosed.

**Names of variables, never their values.** The reason a runtime cannot run is printed
by `doctor`, pasted into support threads, and read aloud in screen shares.

**One terminal event on every path.** A tool call that fails, a finish reason we do not
recognise, an SDK exception — each ends the turn with exactly one `AgentCompleted` or
`AgentFailed`. A frontend iterating this over a transport has nowhere to catch an
exception, and a turn that ends with neither hangs a renderer waiting for one.
"""

from __future__ import annotations

import importlib.util
import json
import os
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from ... import events as ev
from ...models import Message, ToolSpec
from .. import turn


@dataclass(frozen=True)
class Host:
    """One endpoint the OpenAI SDK can reach.

    Everything host-specific about a turn is in these fields, which is the claim the
    design rests on: adding OpenRouter is a row here, not a module.
    """

    #: The client class on the `openai` module. Resolved by name so the SDK stays
    #: unimported until a turn actually starts.
    client: str
    #: The credential variable. Its *name* is reported when authentication fails.
    key: str
    #: Where the endpoint lives, resource first and full base URL last. Any one of
    #: these is enough, and `resource_url` says how the first becomes the second.
    endpoint: tuple[str, ...]
    #: Whether one of `endpoint` must be set for the client to be constructible at all,
    #: or whether the SDK has a default and these are only overrides.
    endpoint_required: bool
    #: What a 404 most likely means here. The hosts fail differently enough that one
    #: shared sentence would be wrong for one of them.
    remedy_404: str
    #: How the first `endpoint` variable becomes a URL, or None where that variable is
    #: already one. A resource name is what a portal shows; a URL is what it implies.
    resource_url: str | None = None
    #: Our variable name → a variable already set for another surface of the same
    #: platform that carries the same value. Read when ours is unset, and named in the
    #: reason so nobody has to know the mapping to fix it.
    fallback: dict[str, str] = field(default_factory=dict)
    #: The output-token parameter this host accepts. `max_tokens` is deprecated on the
    #: OpenAI API and rejected by reasoning models; an older compatible host may still
    #: want it, which is why it is a row rather than a constant.
    tokens_param: str = "max_completion_tokens"


#: Every host reachable through the OpenAI SDK. A new one is a row.
HOSTS: dict[str, Host] = {
    "openai": Host(
        client="AsyncOpenAI",
        key="OPENAI_API_KEY",
        endpoint=("OPENAI_BASE_URL",),
        endpoint_required=False,
        remedy_404="check the model id against the ones the API publishes",
    ),
    "foundry": Host(
        client="AsyncOpenAI",
        key="FOUNDRY_API_KEY",
        endpoint=("FOUNDRY_RESOURCE", "FOUNDRY_BASE_URL"),
        endpoint_required=True,
        remedy_404=(
            "Foundry resolves deployment names — check the deployment exists on this "
            "resource and is served on its OpenAI-compatible surface"
        ),
        resource_url="https://{resource}.services.ai.azure.com/openai/v1",
        fallback={
            "FOUNDRY_API_KEY": "ANTHROPIC_FOUNDRY_API_KEY",
            "FOUNDRY_RESOURCE": "ANTHROPIC_FOUNDRY_RESOURCE",
        },
    ),
}

#: The reference row: the API this protocol is named after, reachable with one variable.
#: Foundry is a deployment's choice, made in its config rather than inherited here.
DEFAULT_HOST = "openai"
ENV_HOST = "LATENT_INTEL_OPENAI_HOST"

DEFAULT_MAX_TOKENS = 16384

#: How many model round-trips one question may take. A bound rather than a budget: a
#: model that loops calling the same tool would otherwise spend until someone noticed.
DEFAULT_MAX_TOOL_ROUNDS = 10

#: Finish reasons that end a turn without an answer, each with the kind it is reported
#: under and what to do about it. The wire names are the protocol's; the kinds match
#: `anthropic.py`, so a frontend switching on one does not need two vocabularies.
_STOP_FAILURES: dict[str, tuple[str, str, str]] = {
    "length": (
        "max_tokens",
        "the model reached its output limit before finishing",
        "raise `max_tokens` under this runtime, or ask a narrower question",
    ),
    "content_filter": (
        "content_filter",
        "the endpoint's content filter stopped the response",
        "rephrase the question",
    ),
}

#: The one finish reason that means the model finished saying what it had to say. A
#: reason absent from here and from `_STOP_FAILURES` is reported rather than guessed at:
#: treating it as success would report a truncated answer as a complete one.
_STOP_DONE = frozenset({"stop"})


def _value(name: str, host: Host) -> str | None:
    """One variable's value, or the value of what this host accepts in its place."""
    return os.environ.get(name) or os.environ.get(host.fallback.get(name, ""))


def _named(name: str, host: Host) -> str:
    """One variable, as it is reported when it is missing — with the variable that
    would also do, because a deployment that has set that one is already finished."""
    other = host.fallback.get(name)
    return f"{name} (or {other})" if other else name


def base_url(host: Host) -> str | None:
    """Where the client points, or None to let the SDK use its own default.

    A resource name and a full base URL are two ways of saying the same thing, so the
    resource wins where it is set and the URL is built from it rather than asked for a
    second time. Nothing here is logged: a base URL is not a credential, but it is read
    from the same place one is.
    """
    if host.resource_url is not None:
        resource = _value(host.endpoint[0], host)
        if resource:
            return host.resource_url.format(resource=resource)
    return _value(host.endpoint[-1], host)


class OpenAIRuntime:
    """The OpenAI SDK, behind the `Runtime` Protocol."""

    id = "openai"

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
        self.model = model or ""
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
        because no variable helps without it; then the variables, by name; then the
        model, which is the one thing no environment can supply for this runtime.
        """
        host = HOSTS.get(self.host)
        if host is None:
            known = ", ".join(sorted(HOSTS))
            return f"unknown host '{self.host}' — known hosts: {known}"
        if importlib.util.find_spec("openai") is None:
            return "the openai SDK is not installed — install `latent-intel[api]`"
        missing: list[str] = []
        if not _value(host.key, host):
            missing.append(_named(host.key, host))
        if host.endpoint_required and not any(
            _value(name, host) for name in host.endpoint
        ):
            missing.append(" or ".join(_named(name, host) for name in host.endpoint))
        if missing:
            return f"set {', '.join(missing)} for host '{self.host}'"
        if not self.model:
            return (
                "no model is set — set `model:` under `runtimes: openai:`, because "
                "every host names its models differently and there is no default"
            )
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
            import openai
        except ImportError as exc:
            yield emitter.emit(
                ev.AgentFailed,
                message=f"the openai SDK could not be imported: {exc}",
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
        except openai.AuthenticationError:
            yield emitter.emit(
                ev.AgentFailed,
                message=f"the '{self.host}' endpoint rejected the credentials",
                kind="auth",
                remedy=(
                    f"check {_named(host.key, host)} — we report the name, never "
                    "the value"
                ),
            )
        except openai.NotFoundError:
            yield emitter.emit(
                ev.AgentFailed,
                message=f"'{self.model}' was not found on the '{self.host}' endpoint",
                kind="model_not_found",
                remedy=host.remedy_404,
            )
        except openai.RateLimitError:
            yield emitter.emit(
                ev.AgentFailed,
                message="the endpoint is rate limiting this key",
                kind="rate_limit",
                remedy="wait and ask again, or raise the deployment's quota",
            )
        except openai.APIStatusError as exc:
            yield emitter.emit(
                ev.AgentFailed,
                message=f"the endpoint returned {exc.status_code}",
                kind="api_error",
                remedy="",
            )
        except openai.APIConnectionError:
            yield emitter.emit(
                ev.AgentFailed,
                message=f"could not reach the '{self.host}' endpoint",
                kind="connection",
                remedy=(
                    f"check {' or '.join(_named(n, host) for n in host.endpoint)} "
                    "and the network"
                ),
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
        host = HOSTS[self.host]

        offered = [
            (turn.wire_name(spec), spec)
            for spec in turn.offered(tools, self.approval)
        ]
        by_name = dict(offered)
        definitions = [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": spec.description,
                    "parameters": turn.input_schema(spec),
                },
            }
            for name, spec in offered
        ]
        system = turn.system_prompt(sources)
        # The system prompt is a message here rather than a parameter, and it is first:
        # a later one is advice the model has already been talking over.
        transcript: list[dict[str, Any]] = []
        if system:
            transcript.append({"role": "system", "content": system})
        # Text only. A prior turn's tool messages are not replayed: they refer to
        # `tool_call_id`s from a request this one never made.
        transcript += [
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
                    "messages": transcript,
                    "stream": True,
                    # Without this the final chunk carries no usage at all, and a
                    # tool-heavy turn would report nothing about what it cost.
                    "stream_options": {"include_usage": True},
                    host.tokens_param: self.max_tokens,
                }
                # Omitted rather than sent empty: the API rejects an empty tool list.
                if definitions:
                    request["tools"] = definitions

                said: list[str] = []
                calls: dict[int, dict[str, str]] = {}
                counts: dict[str, int | None] = {}
                finish: str | None = None

                chunks = await client.chat.completions.create(**request)
                async for chunk in chunks:
                    if getattr(chunk, "usage", None) is not None:
                        details = getattr(chunk.usage, "prompt_tokens_details", None)
                        # The same four counters `anthropic.py` reports, so a frontend
                        # totalling cost reads one vocabulary.
                        counts = {
                            "input_tokens": chunk.usage.prompt_tokens,
                            "output_tokens": chunk.usage.completion_tokens,
                            "cache_read_input_tokens": getattr(
                                details, "cached_tokens", None
                            ),
                            "cache_creation_input_tokens": getattr(
                                details, "cache_write_tokens", None
                            ),
                        }
                    # The usage chunk carries no choice, and a host may send others
                    # that do not either.
                    if not chunk.choices:
                        continue
                    choice = chunk.choices[0]
                    delta = choice.delta
                    piece = getattr(delta, "content", None)
                    if piece:
                        # Text after a tool round is a new block; run straight on from
                        # the text before the call it reads "…let me check.Here is".
                        if separate and answer:
                            answer.append("\n\n")
                            yield emitter.emit(ev.AssistantToken, text="\n\n")
                        separate = False
                        said.append(piece)
                        answer.append(piece)
                        yield emitter.emit(ev.AssistantToken, text=piece)
                    # A call arrives in pieces keyed by `index`: the id and name once,
                    # the arguments as string fragments to concatenate in order.
                    for fragment in getattr(delta, "tool_calls", None) or []:
                        call = calls.setdefault(
                            fragment.index, {"id": "", "name": "", "arguments": ""}
                        )
                        if getattr(fragment, "id", None):
                            call["id"] = fragment.id
                        function = getattr(fragment, "function", None)
                        if function is None:
                            continue
                        if getattr(function, "name", None):
                            call["name"] = function.name
                        if getattr(function, "arguments", None):
                            call["arguments"] += function.arguments
                    if choice.finish_reason:
                        finish = choice.finish_reason

                # Counted after the stream rather than on the usage chunk, so a host
                # that sends none still contributes a round to `num_turns`.
                usage.add_counts(**counts)
                assistant: dict[str, Any] = {
                    "role": "assistant",
                    "content": "".join(said) or None,
                }
                if calls:
                    assistant["tool_calls"] = [
                        {
                            "id": calls[index]["id"],
                            "type": "function",
                            "function": {
                                "name": calls[index]["name"],
                                "arguments": calls[index]["arguments"],
                            },
                        }
                        for index in sorted(calls)
                    ]
                transcript.append(assistant)

                # The reason is checked as well as the calls: a host that streams tool
                # calls and then says `stop` still asked for them.
                if calls or finish == "tool_calls":
                    for index in sorted(calls):
                        call = calls[index]
                        spec = by_name.get(call["name"])
                        arguments, router = _arguments(call["arguments"], call_tool)
                        started, result = await turn.dispatch(
                            router,
                            spec,
                            source_id=spec.source_id if spec else "",
                            name=spec.name if spec else call["name"],
                            arguments=arguments,
                            emitter=emitter,
                        )
                        yield started
                        yield result
                        transcript.append(
                            {
                                "role": "tool",
                                "tool_call_id": call["id"],
                                # The model is told what failed; a failed call it
                                # cannot see is one it repeats.
                                "content": result.error or result.output,
                            }
                        )
                    separate = True
                    continue

                elapsed = int((time.monotonic() - began) * 1000)
                if finish is None or finish in _STOP_DONE:
                    yield emitter.emit(
                        ev.AgentCompleted,
                        text="".join(answer),
                        streamed=True,
                        usage=usage.totals(elapsed),
                    )
                    return

                kind, message, remedy = _STOP_FAILURES.get(
                    finish, ("unexpected_stop", f"the model stopped: {finish}", "")
                )
                yield emitter.emit(
                    ev.AgentFailed, message=message, kind=kind, remedy=remedy
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

        The key is read here rather than left to the SDK, because the fallback variable
        is ours and the SDK has never heard of it. It is passed and never logged —
        which is also why the test seam replaces the whole client rather than the key.
        """
        if self.client_factory is not None:
            return self.client_factory()
        import openai

        host = HOSTS[self.host]
        return getattr(openai, host.client)(
            api_key=_value(host.key, host), base_url=base_url(host)
        )


async def _invalid_arguments(
    source_id: str, name: str, arguments: dict[str, Any]
) -> str:
    """The router a call with unparseable arguments is dispatched through.

    Raising inside `dispatch` is how the failure becomes a `ToolResult` the model is
    shown, rather than a dead turn: a model that emitted broken JSON can emit it again
    correctly, and one told nothing repeats the call.
    """
    raise ValueError("tool arguments were not valid JSON")


def _arguments(
    raw: str, call_tool: turn.ToolRouter | None
) -> tuple[dict[str, Any], turn.ToolRouter | None]:
    """One call's arguments, with the router it should be dispatched through.

    Anything that is not a JSON object — a truncated fragment, a bare string — is the
    same failure, and it is the model's to fix.
    """
    try:
        decoded = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}, _invalid_arguments
    if not isinstance(decoded, dict):
        return {}, _invalid_arguments
    return decoded, call_tool


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_MAX_TOOL_ROUNDS",
    "ENV_HOST",
    "HOSTS",
    "Host",
    "OpenAIRuntime",
    "base_url",
]
