"""The OpenAI-compatible protocol, in process, as an event source.

The sibling of `anthropic.py`, and the reason `agent/turn.py` exists: the tool gate, the
system prompt, the paired tool events and the usage totals are shared, and what is left
here is one wire format — a transcript of `role`/`content` dicts, tool calls arriving as
fragments to be reassembled, and a `finish_reason` instead of a stop reason.

**A host is a row, not a module.** A host serves this protocol, and the rows are
Foundry's OpenAI-compatible surface, the OpenAI API, OpenRouter, classic Azure OpenAI
and a server on the machine you are sitting at: they differ in which variables name the
credential and the endpoint, how the client is constructed, and what a 404 means — and
in nothing else about a turn. The row *type* is `agent/hosts.py`, shared with the
Anthropic runtime rather than declared twice.

**A host decides its own construction.** Classic Azure OpenAI is reached through a
different client class and named kwargs — `azure_endpoint` and `api_version` rather than
`base_url` — so each row carries the function that turns its variables into constructor
arguments. `_client()` stays one expression, and the next host that spells its endpoint
differently is still a row.

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
from typing import Any

from ... import events as ev
from ...models import HostStatus, Message, RuntimeUnavailable, ToolSpec
from .. import hosts, turn

# Construction, above the table because one of these is a row's field and a module body
# is evaluated when it is read. The annotations are lazy; the values are not.


def _construct_azure(host: hosts.Host, values: hosts.Values) -> dict[str, Any]:
    """The kwargs `AsyncAzureOpenAI` takes instead of a base URL: the resource endpoint
    under its own name, and the dated API version, which this client requires and no
    other does.

    Read by name from the row's own resolved values, so the names a reason prints and
    the names a client is built from cannot drift apart.

    Exactly one credential kwarg, and the Entra ID token where both are set: the SDK
    refuses a client given both, and a token is the deliberate choice while a key is
    what a machine tends to still have exported from before. The other is omitted
    rather than passed as None, because the SDK reads its own environment when a
    credential is absent and a None it was handed is not absent.
    """
    kwargs: dict[str, Any] = {
        "azure_endpoint": values["AZURE_OPENAI_ENDPOINT"],
        "api_version": values["OPENAI_API_VERSION"],
    }
    token = values["AZURE_OPENAI_AD_TOKEN"]
    if token:
        kwargs["azure_ad_token"] = token
    else:
        kwargs["api_key"] = values["AZURE_OPENAI_API_KEY"]
    return kwargs


#: Every host reachable through the OpenAI SDK. A new one is a row.
HOSTS: dict[str, hosts.OpenAIHost] = {
    "openai": hosts.OpenAIHost(
        client="AsyncOpenAI",
        key="OPENAI_API_KEY",
        endpoint=("OPENAI_BASE_URL",),
        required=("OPENAI_API_KEY",),
        remedy_404="check the model id against the ones the API publishes",
    ),
    "foundry": hosts.OpenAIHost(
        client="AsyncOpenAI",
        key="FOUNDRY_API_KEY",
        # Both at once is refused: `base_url` takes the address and would quietly
        # ignore the resource, which is the same ambiguity the Anthropic SDK refuses
        # outright — and one of the two is not doing what whoever set it thinks.
        endpoint=("FOUNDRY_RESOURCE", "FOUNDRY_BASE_URL"),
        required=("FOUNDRY_API_KEY", ("FOUNDRY_RESOURCE", "FOUNDRY_BASE_URL")),
        remedy_404=(
            "Foundry resolves deployment names — check the deployment exists on this "
            "resource and is served on its OpenAI-compatible surface"
        ),
        url="https://{resource}.services.ai.azure.com/openai/v1",
        fallback={
            "FOUNDRY_API_KEY": "ANTHROPIC_FOUNDRY_API_KEY",
            "FOUNDRY_RESOURCE": "ANTHROPIC_FOUNDRY_RESOURCE",
        },
    ),
    "openrouter": hosts.OpenAIHost(
        client="AsyncOpenAI",
        key="OPENROUTER_API_KEY",
        endpoint=("OPENROUTER_BASE_URL",),
        required=("OPENROUTER_API_KEY",),
        remedy_404=(
            "OpenRouter model ids are `<vendor>/<model>` — check the id at "
            "openrouter.ai/models"
        ),
        url="https://openrouter.ai/api/v1",
        tokens_param="max_tokens",
    ),
    "azure-openai": hosts.OpenAIHost(
        client="AsyncAzureOpenAI",
        key="AZURE_OPENAI_API_KEY",
        endpoint=("AZURE_OPENAI_ENDPOINT",),
        # A key *or* an Entra ID token — a resource with key auth disabled is the
        # common enterprise posture, and until the group existed this row demanded a
        # credential such a resource cannot issue. Then the endpoint, then the dated
        # API version this client requires and no other does: no default version, one
        # guessed here goes stale and returns a 400 that names neither this file nor
        # the variable that would fix it.
        required=(
            ("AZURE_OPENAI_API_KEY", "AZURE_OPENAI_AD_TOKEN"),
            "AZURE_OPENAI_ENDPOINT",
            "OPENAI_API_VERSION",
        ),
        remedy_404=(
            "Azure OpenAI resolves deployment names — check the deployment exists on "
            "this resource"
        ),
        # The row's default token parameter, not `max_tokens`: a reasoning deployment
        # here rejects `max_tokens` on every API version, and a version old enough to
        # reject `max_completion_tokens` also rejects the `stream_options` every turn
        # sends — so the override bought nothing and cost the newer deployments. A
        # resource that really does want the older name has `tokens_param:` on the
        # runtime.
        construct=_construct_azure,
    ),
    "local": hosts.OpenAIHost(
        client="AsyncOpenAI",
        # Its own variables, not the `openai` row's: a key for the public API
        # forwarded to a server on the LAN is a credential leaving the machine it was
        # issued for, and one pair shared between the rows means the two hosts cannot
        # both be configured in one `.env`.
        key="LOCAL_OPENAI_API_KEY",
        endpoint=("LOCAL_OPENAI_BASE_URL",),
        # The address only. A server on localhost authenticates nobody, so demanding a
        # credential for it would be a requirement invented here rather than one the
        # endpoint has — and the default below is why something is still sent.
        required=("LOCAL_OPENAI_BASE_URL",),
        remedy_404="check the model name the server reports (e.g. its models list)",
        # The SDK refuses to construct a client with no credential at all, and a
        # recognisable word is what appears in that server's logs.
        defaults={"LOCAL_OPENAI_API_KEY": "local"},
        tokens_param="max_tokens",
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
        tokens_param: str | None = None,
        client_factory: Callable[[], Any] | None = None,
        **unknown: Any,
    ) -> None:
        # Rejected, not ignored: a key this runtime never reads is a setting the file
        # says is on and nothing honours — a typo in `model:` is the common case, and
        # it used to build a runtime that then reported no model was set at all.
        if unknown:
            raise RuntimeUnavailable(
                f"unknown option(s) for runtime '{self.id}': "
                f"{', '.join(sorted(unknown))} — see `runtimes: {self.id}:` in config"
            )
        self.model = model or ""
        #: Option beats environment beats default. The environment is how one machine
        #: runs against Foundry and another against the public API with the same
        #: project file checked out on both.
        self.host = host or os.environ.get(ENV_HOST) or DEFAULT_HOST
        self.approval = approval
        self.max_tokens = int(max_tokens)
        self.max_tool_rounds = int(max_tool_rounds)
        #: The output-token parameter, overriding the host row's. The honest lever for
        #: a resource whose API version accepts only the older name: the alternative is
        #: a row guessing on everyone's behalf, which is what it used to do.
        self.tokens_param = tokens_param
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
            return hosts.unknown(self.host, HOSTS)
        if importlib.util.find_spec("openai") is None:
            return "the openai SDK is not installed — install `latent-intel[api]`"
        if (reason := hosts.diagnose(host, name=self.host)) is not None:
            return reason
        if not self.model:
            return (
                "no model is set — set `model:` under `runtimes: openai:`, because "
                "every host names its models differently and there is no default"
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
                remedy=hosts.auth_remedy(host),
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
                message=turn.status_message(exc),
                kind="api_error",
                remedy="",
            )
        except openai.APIConnectionError:
            yield emitter.emit(
                ev.AgentFailed,
                message=f"could not reach the '{self.host}' endpoint",
                kind="connection",
                remedy=hosts.connection_remedy(host),
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

        # Raises on a wire-name collision, before a client exists and before anything
        # is sent; `stream` turns it into the one terminal event this turn gets.
        wired = turn.wired(tools, self.approval)
        by_name = dict(wired)
        definitions = [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": spec.description,
                    "parameters": turn.input_schema(spec),
                },
            }
            for name, spec in wired
        ]
        system = turn.system_prompt(sources)
        # The system prompt is a message here rather than a parameter, and it is first:
        # a later one is advice the model has already been talking over.
        transcript: list[dict[str, Any]] = []
        if system:
            transcript.append({"role": "system", "content": system})
        # Text only. A prior turn's tool messages are not replayed: they refer to
        # `tool_call_id`s from a request this one never made.
        #
        # An empty one is dropped rather than sent: a turn that completed with no text
        # records an empty assistant message, and a host that rejects one would fail
        # every later question in the session over a turn that already ended.
        transcript += [
            {"role": message.role, "content": message.text}
            for message in messages
            if message.text.strip()
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
                    self.tokens_param or host.tokens_param: self.max_tokens,
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
                # Some local servers stream a call with no id at all. The id is only
                # ever a key tying the `tool` message back to the assistant's call, so
                # one is synthesised from the index and used in both places rather than
                # sending `""` twice and hoping the host matches them.
                for index in sorted(calls):
                    if not calls[index]["id"]:
                        calls[index]["id"] = f"call_{index}"
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

                # Before any dispatch: a call truncated by the output limit arrives as
                # unparseable JSON, and dispatching it would report the model's own
                # broken fragment as a failed tool and burn the round bound retrying.
                if finish is not None and finish in _STOP_FAILURES:
                    kind, message, remedy = _STOP_FAILURES[finish]
                    yield emitter.emit(
                        ev.AgentFailed, message=message, kind=kind, remedy=remedy
                    )
                    return

                # A finish reason asking for tools with nothing reassembled is not a
                # turn to continue: the next round would send an assistant message with
                # neither content nor calls and get the same answer again.
                if finish == "tool_calls" and not calls:
                    yield emitter.emit(
                        ev.AgentFailed,
                        message="the model asked for tools but sent no calls",
                        kind="unexpected_stop",
                        remedy="ask again",
                    )
                    return

                # The calls decide, not the reason: a host that streams tool calls and
                # then says `stop` still asked for them.
                if calls:
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
                if finish in _STOP_DONE:
                    yield emitter.emit(
                        ev.AgentCompleted,
                        text="".join(answer),
                        streamed=True,
                        usage=usage.totals(elapsed),
                    )
                    return

                # No finish reason at all is a stream that was cut — by a proxy, by a
                # host that ended the response early — and calling it success reports
                # whatever arrived before the cut as the whole answer.
                if finish is None:
                    yield emitter.emit(
                        ev.AgentFailed,
                        message="the stream ended without a stop reason",
                        kind="unexpected_stop",
                        remedy="ask again",
                    )
                    return

                yield emitter.emit(
                    ev.AgentFailed,
                    message=f"the model stopped: {finish}",
                    kind="unexpected_stop",
                    remedy="",
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

        The values are read here rather than left to the SDK, because a fallback or a
        default the row declares is ours and the SDK has never heard of either. They
        are passed and never logged — which is also why the test seam replaces the
        whole client rather than the key. Which kwargs those are is the host's to say:
        see `construct`.
        """
        if self.client_factory is not None:
            return self.client_factory()
        import openai

        host = HOSTS[self.host]
        return getattr(openai, host.client)(**host.construct(host, hosts.values(host)))


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
    "OpenAIRuntime",
]
