"""Claude Managed Agents — the agent loop on Anthropic's infrastructure.

The fourth family, `managed`. The agent is a versioned definition held by the service,
and each turn is a session on one version of it. That shape is why this runtime exists
here: a hard dollar budget per session, and versions that map one to one onto eval
variants.

**Our tools stay ours.** Every source is offered as a `custom` tool, and a call comes
back over the session's own event stream for our router to run, so no connector needs a
public endpoint. Of Anthropic's built-in toolset only the web tools the scope names are
turned on: no bash, no files, the same "not a coding agent" stance as `claude-cli`.

**The agent is created once, never in the request path.** `intel managed sync` builds
the definition from the resolved deployment and creates the agent, or updates it to a
new immutable version, and one cloud environment beside it. What it returns — ids,
version and the definition's fingerprint — is kept in the user's overlay for the
deployment, which arrives here as `synced`. A deployment is read, never written.

**Stale is refused, not overridden.** A turn computes the fingerprint of what it would
sync; a source attached since, a changed persona or another scope fails the turn with
`kind="managed_stale"` rather than run a version that does not describe what is
attached. `agent_version:` pins an older version, which a turn then runs as it is.

**First-party only.** Managed Agents is served by the Claude API, not by Foundry,
Bedrock or Vertex, so the one host is `anthropic`. Session history, tool output
included, is kept by the service until the session is deleted.

The rules `custom.py` states apply here: the SDK is imported inside a call and never at
module scope, reasons name variables and never their values, and every path of a turn
ends with exactly one `AgentCompleted` or `AgentFailed`.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib.metadata
import json
import logging
import os
import time
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any
from uuid import UUID

import anyio

from ... import events as ev
from ...models import (
    Effect,
    HostStatus,
    Message,
    RuntimeUnavailable,
    Skill,
    ToolSpec,
    WebScope,
)
from .. import bridge, hosts, turn
from ..base import WEB_ID, refuse_web
from ..protocols.messages import STOP_FAILURES

#: The one host Managed Agents is served from.
HOSTS = {"anthropic": hosts.HOSTS["anthropic"]}
HOST = "anthropic"

#: The first `anthropic` release with the Managed Agents API, and the base install's
#: floor. Checked anyway: an environment can hold an older copy than the pin allows.
MIN_SDK = (1, 5)

#: Anthropic's built-in toolset, named by its dated type. Everything in it is off
#: except the web tools a scope turns on.
TOOLSET = "agent_toolset_20260401"
WEB_TOOLS = {"search": ("web_search",), "browse": ("web_search", "web_fetch")}

#: Fingerprinted: what an agent does, not what it is called.
FINGERPRINTED = ("model", "system", "tools")

_default_model = HOSTS[HOST].default_model
if _default_model is None:
    raise RuntimeError(f"host row '{HOST}' declares no default model")
DEFAULT_MODEL: str = _default_model
del _default_model

log = logging.getLogger(__name__)


def fingerprint(definition: dict[str, Any]) -> str:
    """A digest of what an agent would do — model, system prompt and tools — so a turn
    can tell whether the version it would run still describes what is attached."""
    held = {key: definition.get(key) for key in FINGERPRINTED}
    return hashlib.sha256(json.dumps(held, sort_keys=True).encode()).hexdigest()


def _sdk_version() -> tuple[int, ...] | None:
    """The installed `anthropic` release, or None when it is not installed. Read from
    the package metadata, so asking costs no import."""
    try:
        text = importlib.metadata.version("anthropic")
    except importlib.metadata.PackageNotFoundError:
        return None
    parts = []
    for piece in text.split(".")[:2]:
        digits = "".join(c for c in piece if c.isdigit())
        parts.append(int(digits or 0))
    return tuple(parts)


class ClaudeManagedAgentsRuntime:
    """Claude Managed Agents, behind the `Runtime` Protocol."""

    id = "claude-managed-agents"

    def __init__(
        self,
        *,
        model: str | None = None,
        host: str | None = None,
        approval: str = "ask",
        effort: str | None = None,
        max_cost_usd: float | None = None,
        agent_version: int | None = None,
        synced: dict[str, Any] | None = None,
        client_factory: Callable[[], Any] | None = None,
        **unknown: Any,
    ) -> None:
        # Named rather than lumped in with the unknown keys: every other runtime takes
        # it, and the reason it means nothing here is the useful answer.
        if "max_tool_rounds" in unknown:
            raise RuntimeUnavailable(
                f"`max_tool_rounds` means nothing under runtime '{self.id}': the "
                f"service owns its loop — bound a turn with `max_cost_usd` instead"
            )
        if unknown:
            raise RuntimeUnavailable(
                f"unknown option(s) for runtime '{self.id}': "
                f"{', '.join(sorted(unknown))} — see `runtimes: {self.id}:` in config"
            )
        if max_cost_usd is not None and float(max_cost_usd) <= 0:
            raise RuntimeUnavailable(
                f"`max_cost_usd` must be above zero for runtime '{self.id}'"
            )
        self.model = model or DEFAULT_MODEL
        self.host = host or HOST
        self.approval = approval
        self.effort = effort
        self.max_cost_usd = None if max_cost_usd is None else float(max_cost_usd)
        self.agent_version = None if agent_version is None else int(agent_version)
        #: What `intel managed sync` recorded in the user's overlay for this
        #: deployment: `agent_id`, `version`, `environment_id`, `fingerprint`.
        self.synced = dict(synced or {})
        #: The test seam: returns the SDK client as an async context manager.
        self.client_factory = client_factory
        #: Per session: the managed session continued, the history it has answered as
        #: `(role, text)` pairs, and what it had spent in dollars at list price.
        self._sessions: dict[UUID, tuple[str, tuple[tuple[str, str], ...], float]] = {}

    # -- diagnosis ------------------------------------------------------------

    def available(self) -> bool:
        return self.unavailable_reason() is None

    def unavailable_reason(self) -> str | None:
        """Why this cannot run here, in the order someone would fix it."""
        reason = self.provision_reason()
        if reason is not None:
            return reason
        if not self.synced.get("agent_id") or not self.synced.get("environment_id"):
            return (
                "no agent has been created for this deployment — run "
                "`intel managed sync` (`--dry-run` first shows what it would create)"
            )
        return None

    def provision_reason(self) -> str | None:
        """Why `intel managed sync` cannot run here: what a turn needs, less the agent
        it would create."""
        if self.host != HOST:
            return (
                f"runtime '{self.id}' is served by host '{HOST}' only, not "
                f"'{self.host}' — Managed Agents is not on Foundry, Bedrock or Vertex"
            )
        version = _sdk_version()
        if version is None or version < MIN_SDK:
            floor = ".".join(str(n) for n in MIN_SDK)
            return (
                f"Managed Agents needs anthropic>={floor} — reinstall `latent-intel`, "
                f"which requires it"
            )
        return hosts.diagnose(HOSTS[HOST], name=HOST)

    def host_status(self) -> dict[str, HostStatus]:
        """The one host this runtime declares. See `hosts.status`."""
        return hosts.status(HOSTS)

    def family(self) -> str:
        """The loop runs on Anthropic's infrastructure. See `agent/base.Owned`."""
        return "managed"

    def web_reason(self, scope: WebScope) -> str | None:
        """Why this runtime cannot honour `scope`, or None. The toolset takes domain
        lists, and nothing that caps a number of uses."""
        if scope.mode == "off":
            return None
        if scope.max_uses is not None:
            return (
                f"runtime '{self.id}' has no limit on web tool uses — drop "
                f"`max_uses`, or bound the turn with `max_cost_usd`"
            )
        return None

    # -- the agent ------------------------------------------------------------

    def definition(self, tools: Sequence[ToolSpec], **context: Any) -> dict[str, Any]:
        """The agent `intel managed sync` would create, from what a turn is given.

        Pure: makes no request. The system prompt is the one every runtime sends —
        inventory, posture, persona and the skill listing — with every listed skill,
        including those a person runs with `/name`: those travel in the turn's first
        message, so running one does not make the agent stale. Approval filters the
        tools here, as `turn.offered` does per turn.
        """
        wired = turn.wired(tools, self.approval)
        skills: list[Skill] = sorted(
            [*(context.get("skills") or ()), *(context.get("invoked") or ())],
            key=lambda s: s.name,
        )
        system = turn.system_prompt(
            context.get("sources") or [],
            persona=context.get("persona") or "",
            persona_mode=context.get("persona_mode") or "append",
            skills=list({s.name: s for s in skills}.values()),
            loader=turn.loader(wired),
        )
        web: WebScope = context.get("web") or WebScope()
        model: Any = (
            {"id": self.model, "effort": self.effort} if self.effort else self.model
        )
        name = str(context.get("name") or "latent-intel")
        return {
            "name": f"latent-intel · {name}",
            "description": "A latent-intel deployment, served its sources as tools.",
            "model": model,
            "system": system,
            "tools": [
                *(
                    {
                        "type": "custom",
                        "name": wire,
                        "description": spec.description,
                        "input_schema": turn.input_schema(spec),
                    }
                    for wire, spec in wired
                ),
                _toolset(web),
            ],
        }

    async def provision(self, definition: dict[str, Any]) -> dict[str, Any]:
        """Create the agent, or update it to a new version when its fingerprint moved,
        and create its environment once. Returns what the overlay keeps.

        Spends the key: called only by `intel managed sync`, run by hand. An unchanged
        definition makes no request at all.
        """
        if (reason := self.provision_reason()) is not None:
            raise RuntimeUnavailable(reason)
        digest = fingerprint(definition)
        synced = dict(self.synced)
        if synced.get("agent_id") and synced.get("fingerprint") == digest:
            return synced
        async with self._client() as client:
            if synced.get("agent_id"):
                agent = await client.beta.agents.update(
                    synced["agent_id"], version=synced["version"], **definition
                )
            else:
                agent = await client.beta.agents.create(**definition)
            environment_id = synced.get("environment_id")
            if not environment_id:
                # Nothing runs in the container — bash and files are off — so it may
                # reach nothing either.
                environment = await client.beta.environments.create(
                    name=definition["name"],
                    config={"type": "cloud", "networking": {"type": "limited"}},
                )
                environment_id = environment.id
        return {
            "agent_id": agent.id,
            "version": agent.version,
            "environment_id": environment_id,
            "fingerprint": digest,
        }

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
        if refused := refuse_web(self, options.get("web") or WebScope(), emitter):
            yield refused
            return
        try:
            stale = self.agent_version is None and self.stale(tools, **options)
        except turn.ToolNameCollision as exc:
            yield emitter.emit(
                ev.AgentFailed,
                message=str(exc),
                kind="tool_name_collision",
                remedy="attach one of the two sources under another id",
            )
            return
        if stale:
            yield emitter.emit(
                ev.AgentFailed,
                message=(
                    "the managed agent no longer matches this deployment — a source, "
                    "the persona, the skills, the web scope or approval changed since "
                    "it was synced"
                ),
                kind="managed_stale",
                remedy="`intel managed sync`, then ask again",
            )
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None or asyncio.current_task(loop) is None:
            yield emitter.emit(
                ev.AgentFailed,
                message=(
                    f"runtime '{self.id}' needs the asyncio event loop; this one is "
                    f"running under another"
                ),
                kind="runtime_unavailable",
                remedy="",
            )
            return
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover — `unavailable_reason` read it
            yield emitter.emit(
                ev.AgentFailed,
                message=f"the anthropic SDK could not be imported: {exc}",
                kind="runtime_unavailable",
                remedy="reinstall `latent-intel`",
            )
            return

        relay = bridge.Relay()
        try:
            async for event in self._turn(
                loop, messages, tools, emitter=emitter, relay=relay, **options
            ):
                yield event
        except Exception as exc:  # noqa: BLE001 — a failure is an event, not a crash
            for event in [*relay.drain(), *relay.abandon(emitter, "the turn ended")]:
                yield event
            self._sessions.pop(emitter.session_id, None)
            row = HOSTS[HOST]
            yield emitter.emit(
                ev.AgentFailed,
                **turn.failure(
                    exc, sdk=anthropic, host=row, name=HOST, model=self.model
                ),
            )

    async def _turn(
        self,
        loop: asyncio.AbstractEventLoop,
        messages: list[Message],
        tools: list[ToolSpec],
        *,
        emitter: ev.Emitter,
        relay: bridge.Relay,
        **options: Any,
    ) -> AsyncIterator[ev.AgentEvent]:
        """The session's events, translated. `stream` is only the exception vocabulary.

        Two consumers of one session, and only one of them ours. The SDK's
        `tool_runner` answers every call to our tools — it attaches before it
        reconciles, dedupes, and retries the result it posts back, so a call is never
        left unanswered, which would hold the session idle forever. It runs in an
        asyncio task of its own, as an MCP source's connection does, because it holds
        a task group and this generator yields: anyio requires a cancel scope to be
        exited by the task that entered it. This generator reads the stream for
        everything else — text, the web, usage, and the end of the turn.
        """
        wired = turn.wired(tools, self.approval)
        runnable = _runnable(
            wired,
            emitter=emitter,
            call_tool=options.get("call_tool"),
            relay=relay,
        )
        history: list[tuple[str, str]] = [(m.role, m.text) for m in messages]
        prior = self._resumable(emitter.session_id, history)
        question = history[-1][1] if history else ""
        content = [
            *({"type": "text", "text": s.body} for s in options.get("invoked") or ()),
            {"type": "text", "text": question},
        ]

        usage = turn.UsageTotals()
        began = time.monotonic()
        #: Each agent message's text by its id, in order: what has streamed of it, and
        #: then the whole of it once the buffered event arrives. Deltas are best-effort
        #: and may stop early, or be lost to a dropped stream; the event is not.
        parts: dict[str, str] = {}
        opened: dict[str, tuple[ev.ToolStarted, dict[str, Any]]] = {}
        error = ""
        finished = False

        def said(message_id: str, text: str) -> list[ev.AgentEvent]:
            """The tokens that bring message `message_id` up to `text` — a break first
            when it is a new message after another — and `parts` updated to match."""
            if not text:
                return []
            tokens: list[ev.AgentEvent] = []
            if message_id not in parts:
                if any(parts.values()):
                    tokens.append(emitter.emit(ev.AssistantToken, text="\n\n"))
                parts[message_id] = ""
            shown = parts[message_id]
            common = len(os.path.commonprefix([shown, text]))
            if text[common:]:
                tokens.append(emitter.emit(ev.AssistantToken, text=text[common:]))
            parts[message_id] = text
            return tokens

        def answer() -> str:
            return "\n\n".join(text for text in parts.values() if text)

        async with self._client() as client:
            if prior is not None:
                session_id, spent = prior
            else:
                session = await client.beta.sessions.create(**self._session_request())
                session_id, spent = session.id, 0.0
            runner = client.beta.sessions.events.tool_runner(
                session_id, tools=runnable, max_idle=None
            )
            owner = loop.create_task(_drive(runner))
            try:
                async for item in _events(client, session_id, content, owner):
                    for held in relay.drain():
                        yield held
                    kind = getattr(item, "type", "")
                    if kind == "event_delta":
                        so_far = parts.get(item.event_id, "") + _delta_text(item)
                        for token in said(item.event_id, so_far):
                            yield token
                    elif kind == "agent.message":
                        for token in said(item.id, _message_text(item)):
                            yield token
                    elif kind == "agent.tool_use" and item.name in WEB_NAMES:
                        arguments = dict(item.input or {})
                        started = emitter.emit(
                            ev.ToolStarted,
                            tool=item.name,
                            source_id=WEB_ID,
                            arguments=arguments,
                            effect=str(Effect.EXTERNAL_READ),
                        )
                        opened[item.id] = (started, arguments)
                        yield started
                    elif kind == "agent.tool_result" and item.tool_use_id in opened:
                        started, arguments = opened.pop(item.tool_use_id)
                        output, refs = _web_result(started.tool, arguments, item)
                        failed = bool(item.is_error)
                        yield emitter.nested(started).emit(
                            ev.ToolResult,
                            tool=started.tool,
                            source_id=WEB_ID,
                            ok=not failed,
                            output="" if failed else output,
                            error=output if failed else "",
                            refs=[] if failed else refs,
                        )
                    elif kind == "span.model_request_end":
                        model_usage = item.model_usage
                        usage.add_counts(
                            input_tokens=model_usage.input_tokens,
                            output_tokens=model_usage.output_tokens,
                            cache_creation_input_tokens=(
                                model_usage.cache_creation_input_tokens
                            ),
                            cache_read_input_tokens=model_usage.cache_read_input_tokens,
                        )
                    elif kind == "session.error":
                        error = str(getattr(item.error, "message", "") or "")
                    elif kind == "session.status_idle":
                        stop = getattr(item.stop_reason, "type", "")
                        if stop == "requires_action":
                            continue
                        finished = True
                        for held in [
                            *relay.drain(),
                            *relay.abandon(emitter, "the turn ended"),
                        ]:
                            yield held
                        total = await _spent(client, session_id)
                        text = answer()
                        yield self._ending(
                            stop,
                            emitter,
                            details=getattr(item, "stop_details", None),
                            text=text,
                            usage=usage.totals(int((time.monotonic() - began) * 1000)),
                            cost=None if total is None else total - spent,
                            error=error,
                        )
                        # Only a finished answer leaves the session to continue: after a
                        # budget stop it has no budget left, and after a failure the
                        # history no longer matches what it holds.
                        if stop == "end_turn" and text:
                            self._sessions[emitter.session_id] = (
                                session_id,
                                (*history, ("assistant", text)),
                                spent if total is None else total,
                            )
                        else:
                            self._sessions.pop(emitter.session_id, None)
                        return
                    elif kind in ("session.status_terminated", "session.deleted"):
                        finished = True
                        for held in [
                            *relay.drain(),
                            *relay.abandon(emitter, "the session ended"),
                        ]:
                            yield held
                        self._sessions.pop(emitter.session_id, None)
                        yield emitter.emit(
                            ev.AgentFailed,
                            message=error or "the managed session was terminated",
                            kind="managed_terminated",
                            remedy="ask again; a new session starts",
                        )
                        return
                raise RuntimeError("the session's event stream ended mid-turn")
            finally:
                with anyio.CancelScope(shield=True):
                    if not finished:
                        # Stop the agent spending on a turn nobody is reading, whatever
                        # ended it — a raise, or a cancel from the frontend — and
                        # before waiting on a tool the runner may still be running.
                        with contextlib.suppress(Exception):
                            await client.beta.sessions.events.send(
                                session_id, events=[{"type": "user.interrupt"}]
                            )
                    owner.cancel()
                    # Bounded: the runner runs a tool in a shielded scope, and a turn
                    # that has ended does not wait out its timeout.
                    await asyncio.wait({owner}, timeout=OWNER_GRACE)

    def _ending(
        self,
        stop: str,
        emitter: ev.Emitter,
        *,
        details: Any = None,
        text: str,
        usage: dict[str, int],
        cost: float | None,
        error: str,
    ) -> ev.AgentEvent:
        """The one terminal event for an idle session, by its stop reason."""
        if stop == "end_turn" or (stop == "budget_reached" and text.strip()):
            done: ev.AgentCompleted = emitter.emit(
                ev.AgentCompleted,
                text=text,
                streamed=True,
                usage=usage,
                cost_usd=cost,
                stopped="cost" if stop == "budget_reached" else None,
            )
            return done
        if stop == "budget_reached":
            fields = {
                "message": f"stopped at the cost ceiling of ${self.max_cost_usd}",
                "kind": "cost",
                "remedy": "raise `max_cost_usd` under this runtime, or ask for less",
            }
        elif stop == "refusal":
            # As every runtime words a refusal, with the service's reason where given.
            message, remedy = STOP_FAILURES["refusal"]
            why = str(getattr(details, "explanation", "") or "")
            fields = {
                "message": f"{message} — {why}" if why else message,
                "kind": "refusal",
                "remedy": remedy,
            }
        elif stop == "retries_exhausted":
            fields = {
                "message": error or "the session gave up after retrying the model",
                "kind": "api_error",
                "remedy": "ask again",
            }
        else:
            fields = {
                "message": f"the session stopped: {stop or 'no reason given'}",
                "kind": "unexpected_stop",
                "remedy": "ask again",
            }
        failed: ev.AgentFailed = emitter.emit(ev.AgentFailed, **fields)
        return failed

    def _session_request(self) -> dict[str, Any]:
        """A new session on the synced agent, at a pinned version where one is set,
        with `max_cost_usd` as its budget, which is fixed when a session is created."""
        request: dict[str, Any] = {
            "agent": {
                "type": "agent",
                "id": self.synced["agent_id"],
                "version": self.agent_version or self.synced["version"],
            },
            "environment_id": self.synced["environment_id"],
        }
        if self.max_cost_usd is not None:
            request["budget"] = {
                "type": "limit",
                "max_list_cost": {
                    "amount": str(round(self.max_cost_usd * 100)),
                    "currency": "USD",
                },
            }
        return request

    def _resumable(
        self, session: UUID, history: list[tuple[str, str]]
    ) -> tuple[str, float] | None:
        """The managed session to continue, and what it had spent, or None for a new
        one. The rule `claude-agent-sdk` follows: only when the history continues the
        one recorded, followed by nothing but questions."""
        held = self._sessions.get(session)
        if held is None or not history or history[-1][0] != "user":
            return None
        session_id, seen, spent = held
        prior = history[:-1]
        if tuple(prior[: len(seen)]) != seen:
            return None
        if any(role != "user" for role, _ in prior[len(seen) :]):
            return None
        return session_id, spent

    def stale(self, tools: Sequence[ToolSpec], **options: Any) -> bool:
        """Whether the synced version no longer describes what a turn would send."""
        return fingerprint(self.definition(tools, **options)) != self.synced.get(
            "fingerprint"
        )

    def _client(self) -> Any:
        """The SDK client, as the async context manager the SDK itself returns."""
        if self.client_factory is not None:
            return self.client_factory()
        import anthropic

        host = HOSTS[HOST]
        return anthropic.AsyncAnthropic(**host.construct(host, hosts.values(host)))


def _toolset(web: WebScope) -> dict[str, Any]:
    """Anthropic's built-in toolset with everything off but the web tools `web`
    names, each carrying the scope's domain list."""
    domains: dict[str, Any] = {}
    if web.allowed_domains:
        domains["allowed_domains"] = list(web.allowed_domains)
    if web.blocked_domains:
        domains["blocked_domains"] = list(web.blocked_domains)
    return {
        "type": TOOLSET,
        "default_config": {"enabled": False},
        "configs": [
            {"name": name, "enabled": True, **domains}
            for name in WEB_TOOLS.get(web.mode, ())
        ],
    }


#: The built-in tools a scope can turn on, reported as the web's like on every runtime.
WEB_NAMES = frozenset(name for names in WEB_TOOLS.values() for name in names)

#: How many reopens in a row may deliver nothing new before the turn gives up, and
#: the backoff between them, in seconds.
RECONNECTS = 3
BACKOFF = 0.25
BACKOFF_CAP = 4.0

#: How long a turn that has ended waits for the tool runner to stop, in seconds.
OWNER_GRACE = 1.0


async def _drive(runner: Any) -> None:
    """The SDK's tool runner, run to the end — in its own task; see `_turn`.

    A result it gave up posting — a 4xx, or its retry window spent — leaves the
    session waiting on a call nobody will answer, so it ends the turn instead. A call
    it does not own, or one denied, has no result and is not that.
    """
    async for call in runner:
        if getattr(call, "result", None) is not None and not getattr(
            call, "posted", True
        ):
            raise RuntimeError(
                f"the result of '{call.name}' could not be posted back to the session"
            )


async def _events(
    client: Any, session_id: str, content: list[dict[str, Any]], owner: Any
) -> AsyncIterator[Any]:
    """This turn's events, in order and once each, across a dropped stream.

    Attached before the question is sent, so nothing the agent does is missed. A stream
    that drops, or ends, is reopened and the session's history read again from the
    question on, so what arrived in the gap is delivered and what came before is not.
    Reconnects are counted only while they deliver nothing new.
    """
    events = client.beta.sessions.events
    seen: set[str] = set()
    asked: str | None = None
    sent = False
    idle = 0
    while True:
        if _failed(owner):
            raise owner.exception()
        progressed = False
        failure: BaseException | None = None
        try:
            async with await events.stream(
                session_id, event_deltas=["agent.message"]
            ) as live:
                # A runner that dies leaves a tool call unanswered, and the session
                # waits on it with nothing more to send: close the stream so the turn
                # ends with the runner's failure instead of waiting forever.
                def stop(task: Any, live: Any = live) -> None:
                    if not task.cancelled() and task.exception() is not None:
                        task.get_loop().create_task(live.close())

                owner.add_done_callback(stop)
                try:
                    if not sent:
                        reply = await events.send(
                            session_id,
                            events=[{"type": "user.message", "content": content}],
                        )
                        sent = True
                        asked = next(
                            (
                                e.id
                                for e in (getattr(reply, "data", None) or ())
                                if e.type == "user.message"
                            ),
                            None,
                        )
                    else:
                        history = [
                            past
                            async for past in events.list(
                                session_id, order="asc", limit=1000
                            )
                        ]
                        for past in _since(history, asked):
                            key = getattr(past, "id", None)
                            if key is not None and key not in seen:
                                seen.add(key)
                                progressed = True
                                yield past
                    async for item in live:
                        key = getattr(item, "id", None)
                        if key is not None:
                            if key in seen:
                                continue
                            seen.add(key)
                        progressed = True
                        yield item
                finally:
                    owner.remove_done_callback(stop)
        except Exception as exc:  # noqa: BLE001 — a dropped stream is retried below
            if _failed(owner):
                raise owner.exception() from exc
            if not _transient(exc):
                raise
            failure = exc
        idle = 0 if progressed else idle + 1
        if idle > RECONNECTS:
            if failure is not None:
                raise failure
            return
        await anyio.sleep(min(BACKOFF * 2**idle, BACKOFF_CAP))


def _since(history: list[Any], asked: str | None) -> list[Any]:
    """The events from this turn's question on: from its id when the send reported
    one, else from the last question in the history, which is this turn's."""

    def marks(match: Callable[[Any], bool]) -> list[int]:
        return [i for i, past in enumerate(history) if match(past)]

    found = marks(lambda past: getattr(past, "id", None) == asked) if asked else []
    found = found or marks(lambda past: getattr(past, "type", "") == "user.message")
    return history[found[-1] :] if found else history


async def _spent(client: Any, session_id: str) -> float | None:
    """What the session has cost so far, at list price, read from the session: the
    event stream does not carry its usage. None when it cannot be read — the answer
    stands without a figure rather than failing for want of one."""
    try:
        session = await client.beta.sessions.retrieve(session_id)
    except Exception:  # noqa: BLE001
        log.warning("could not read the cost of managed session %s", session_id)
        return None
    return _dollars(getattr(getattr(session, "usage", None), "list_cost", None))


def _failed(owner: Any) -> bool:
    """Whether the runner's task ended with an exception of its own."""
    return bool(owner.done() and not owner.cancelled() and owner.exception())


def _transient(exc: BaseException) -> bool:
    """A dropped connection, worth reopening; anything else is the turn's failure.

    What the SDK's own runners retry: a transport error — which a stream being read
    raises unwrapped — a connection error, and any status but a 4xx no retry fixes.
    """
    import anthropic
    import httpx2

    if isinstance(exc, anthropic.APIStatusError):
        return exc.status_code >= 500 or exc.status_code in (408, 409, 429)
    return isinstance(
        exc, (httpx2.HTTPError, anthropic.APIConnectionError, ConnectionError)
    )


def _delta_text(item: Any) -> str:
    delta = getattr(item, "delta", None)
    block = getattr(delta, "content", None)
    return str(getattr(block, "text", "") or "")


def _message_text(item: Any) -> str:
    return "".join(
        str(block.text)
        for block in item.content
        if getattr(block, "type", "") == "text"
    )


def _web_result(
    tool: str, arguments: dict[str, Any], item: Any
) -> tuple[str, list[str]]:
    """A web call's output as titles and URLs, never page text, and its refs: each
    result a search found, or the page a fetch read."""
    lines: list[str] = []
    refs: list[str] = []
    for block in item.content or ():
        kind = getattr(block, "type", "")
        if kind == "search_result":
            lines.append(f"{block.title} — {block.source}")
            refs.append(f"web:{block.source}")
        elif kind == "text" and item.is_error:
            lines.append(str(block.text))
    url = arguments.get("url")
    if tool == "web_fetch" and isinstance(url, str) and url:
        lines = lines or [url]
        refs = [f"web:{url}"]
    return "\n".join(lines), list(dict.fromkeys(refs))


def _dollars(amount: Any) -> float | None:
    """A monetary amount in US dollars. The API counts cents, as a string."""
    if amount is None or getattr(amount, "currency", "USD") != "USD":
        return None
    try:
        return float(str(amount.amount)) / 100
    except ValueError:
        return None


def _runnable(
    wired: Sequence[tuple[str, ToolSpec]],
    *,
    emitter: ev.Emitter,
    call_tool: Any,
    relay: bridge.Relay,
) -> list[Any]:
    """Each wired tool as the SDK runner's own tool type, its body our router — the
    pattern `anthropic-sdk` follows. `ToolError` is how the runner is told a call
    failed, which it posts back as an error result the model can respond to."""
    from anthropic.lib.tools import BetaAsyncBuiltinFunctionTool, ToolError

    class _Tool(BetaAsyncBuiltinFunctionTool):
        def __init__(self, name: str, spec: ToolSpec) -> None:
            self._name = name
            self._spec = spec
            self._run = bridge.bridged(
                spec, emitter=emitter, call_tool=call_tool, relay=relay
            )

        def to_dict(self) -> Any:
            return {
                "name": self._name,
                "description": self._spec.description,
                "input_schema": turn.input_schema(self._spec),
            }

        async def call(self, input: object) -> str:
            arguments = dict(input) if isinstance(input, dict) else {}
            try:
                return await self._run(arguments)
            except bridge.BridgeError as exc:
                raise ToolError(str(exc)) from exc

    return [_Tool(name, spec) for name, spec in wired]


__all__ = ["ClaudeManagedAgentsRuntime", "fingerprint"]
