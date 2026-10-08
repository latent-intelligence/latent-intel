"""Claude Code's harness, driven through the Claude Agent SDK, our tools served to it.

`claude-cli` already launches the same binary and only listens to it, so our sources
reach it as MCP servers it starts itself — the servable ones, never an engine tool — and
tool calls happen out of our sight. The SDK keeps a two-way channel open instead, and
that is the whole reason this module exists beside that one: **our tools are served
in-process**, as one SDK MCP server whose handlers are `turn.dispatch` reached through
`agent/bridge.py`. Every attached source is visible, the paired events are ours and the
effect gate is ours, exactly as under `anthropic-sdk`. What differs is only what the
harness owns — the loop, context handling, its system prompt, and what it adds later.

The two Anthropic runtimes are named as Anthropic names them: `anthropic-sdk` is the
`anthropic` SDK's tool runner, and this is the Claude Agent SDK.

**Isolation is on every turn, not a setting.** Left to its defaults the binary reads the
operator's `~/.claude` — settings, `CLAUDE.md`, auto-memory, skills, MCP servers — and
answers from context this session never granted, which `claude-cli` already learned
with `--strict-mcp-config`. So: no built-in tools, no settings sources, none of Claude
Code's own skills (the project's arrive through `load_skill`, served like any other
tool), strict MCP config, `dontAsk` so anything off the allow-list is refused rather
than prompted for, and prompts delivered verbatim so an `@/path` in a question cannot
make the binary read a file. The one opening is the session's `web:` scope, which turns
on the binary's two web-reading tools and nothing else — mapped and refused exactly as
under `claude-cli`, whose `WEB_TOOLS` declares their effects — and their pairs are built
from its messages since our router never sees them. The `builtin_tools:` option that
used to open them is refused by name: two settings that could disagree about whether a
turn read the web would make a recording's header a guess.

**The environment is chosen, not inherited.** The child process gets ours merged with
what is passed here, so a variable is taken away only by setting it empty. The chosen
host row's variables and its provider switch go through; every other row's variables,
the other providers' switches, the ambient bearer and OAuth tokens and the global
subagent-model override are blanked. The binary therefore authenticates with what
`doctor` reported, and never falls back to a claude.ai login — which Anthropic does not
permit for a product built on the SDK. Claude Code's model aliases
(`ANTHROPIC_DEFAULT_*_MODEL`) pass through: a Foundry deployment needs them.

**Resume is the harness's own.** A follow-up question resumes the binary's session, so
the harness keeps its full context, tool results included, rather than a text replay.
It resumes only when the history it is handed continues the one it recorded; a rebuilt
runtime or a different history starts fresh and sends only the last question, as
`claude-cli` does. Resuming needs the binary's session files under `~/.claude/projects`.

**Subagents are the harness's own.** A project's `agents/` files become the SDK's agent
definitions, their tools renamed from `source.tool` to what the binary calls them, and
the binary's `Agent` tool is offered only when there is one — and allowed per agent,
`Agent(<name>)`, so Claude Code's own built-in subagents stay refused. A hand-off
renders as a pair whose result is the subagent's report; the calls the subagent makes
to our tools are reported like any other. This runtime is the one that declares
`Delegating`.

**Differences from the custom loop, on purpose.** A tool name the model invented, and
arguments that fail the tool's schema, are refused by the SDK before our router sees
them, so no event pair is emitted for either; the model is still told. The ceiling is
the binary's `max_turns`. The cost is the binary's own estimate.

The rules `custom.py` states apply here verbatim: the SDK is imported inside the turn
and never at module scope, reasons name variables and never their values, and every path
ends with exactly one `AgentCompleted` or `AgentFailed`.
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import shutil
import time
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Sequence
from typing import Any
from uuid import UUID

from ... import events as ev
from ...models import (
    Effect,
    HostStatus,
    Message,
    RuntimeUnavailable,
    ToolSpec,
    WebScope,
)
from ...subagents import Subagent
from .. import bridge, hosts, turn
from ..base import WEB_ID, refuse_web
from . import anthropic_sdk, claude_cli

#: How the binary is told to use each row. Its own table rather than a field on `Host`:
#: this is how one runtime dials a row, and a row added to `hosts.HOSTS` should not be
#: dialled here until someone has decided how — `tests/` holds the two sets equal.
_SWITCHES: dict[str, dict[str, str]] = {
    "anthropic": {},
    "foundry-anthropic": {"CLAUDE_CODE_USE_FOUNDRY": "1"},
}

#: Every host this runtime reaches: the rows of `hosts.HOSTS` that name the anthropic
#: SDK and that the binary knows how to be pointed at.
HOSTS = {
    name: row for name, row in hosts.for_sdk("anthropic").items() if name in _SWITCHES
}

#: The tool runner's defaults, taken rather than restated: switching `runtime:` between
#: the two Anthropic runtimes should change the harness and nothing else.
DEFAULT_HOST = anthropic_sdk.DEFAULT_HOST
DEFAULT_MODEL = anthropic_sdk.DEFAULT_MODEL

#: Its own variable, as the other SDK runtimes have theirs.
ENV_HOST = "LATENT_INTEL_CLAUDE_AGENT_SDK_HOST"

#: The in-process MCP server our tools are served from, and the prefix the binary puts
#: on each of their names.
SERVER = "latent"
PREFIX = f"mcp__{SERVER}__"

#: A tool name on the wire, prefix included. `turn.wire_name` caps the bare name at the
#: same length, so the prefix has to come out of it here.
_MAX_NAME = 64

#: Provider switches other than the chosen row's, blanked so an exported one cannot
#: send the binary somewhere `doctor` did not look.
_PROVIDERS = (
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)

#: Credentials the binary prefers over the row's key when present — a bearer token, a
#: Foundry token, an OAuth token for a subscription. Blanked for the same reason.
_AMBIENT = (
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_FOUNDRY_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
)

#: The binary's global override for every subagent's model, blanked: a project's
#: agent file names its own `model:`, and an export on the operator's shell would win
#: over it silently. The `ANTHROPIC_DEFAULT_*_MODEL` aliases pass through on purpose —
#: a Foundry deployment names its haiku, sonnet and opus deployments with them.
_OVERRIDES = ("CLAUDE_CODE_SUBAGENT_MODEL",)

#: Set on every turn. Auto-memory is not covered by `setting_sources` and would load the
#: operator's notes for whatever repository the process runs in; the rest is traffic no
#: turn asked for, the same posture as tracing off under `openai-agents`.
_QUIET = {
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
}

#: How much of the binary's stderr to keep for a failure message.
_STDERR_LINES = 20

#: The binary's tool for handing a task to a subagent. Offered only when the project
#: declares one. It changes nothing itself — what the subagent does is reported as the
#: calls it makes — so its effect is `none`.
AGENT_TOOL = "Agent"


def environment(name: str) -> dict[str, str]:
    """What the binary is given for host `name`: its row's values and switch, with
    everything that could point it elsewhere blanked. Values only ever go to the child
    process; nothing here is printed."""
    row = HOSTS[name]
    env = {
        variable: ""
        for other, other_row in HOSTS.items()
        if other != name
        for variable in hosts.names(other_row)
    }
    env.update(dict.fromkeys((*_PROVIDERS, *_AMBIENT, *_OVERRIDES), ""))
    env.update({key: value for key, value in hosts.values(row).items() if value})
    env.update(_SWITCHES[name])
    env.update(_QUIET)
    return env


class ClaudeAgentSdkRuntime:
    """The Claude Agent SDK, behind the `Runtime` Protocol."""

    id = "claude-agent-sdk"

    def __init__(
        self,
        *,
        model: str | None = None,
        host: str | None = None,
        approval: str = "ask",
        max_tool_rounds: int = turn.DEFAULT_MAX_TOOL_ROUNDS,
        max_budget_usd: float | None = None,
        cwd: str | None = None,
        cli_path: str | None = None,
        query_factory: Callable[[], Any] | None = None,
        **unknown: Any,
    ) -> None:
        # Named rather than lumped in with the unknown keys: it worked once, and a
        # config still carrying it needs to be told where its meaning went.
        if "builtin_tools" in unknown:
            raise RuntimeUnavailable(
                f"`builtin_tools` under runtime '{self.id}' was replaced by `web:` — "
                f"set `defaults.web: browse` (or `search`) in the project, or `web:` "
                f"in config, and remove `builtin_tools`"
            )
        # Rejected, not ignored, for the reason `custom.py` gives: a key nothing reads
        # is a setting the file says is on and no one honours.
        if unknown:
            raise RuntimeUnavailable(
                f"unknown option(s) for runtime '{self.id}': "
                f"{', '.join(sorted(unknown))} — see `runtimes: {self.id}:` in config"
            )
        # The SDK drops a zero rather than passing it, which would lift the ceiling
        # entirely — the opposite of what a zero was written to mean.
        if int(max_tool_rounds) < 1:
            raise RuntimeUnavailable(
                f"`max_tool_rounds` must be at least 1 for runtime '{self.id}'"
            )
        if max_budget_usd is not None and float(max_budget_usd) <= 0:
            raise RuntimeUnavailable(
                f"`max_budget_usd` must be above zero for runtime '{self.id}'"
            )
        self.model = model or DEFAULT_MODEL
        self.host = host or os.environ.get(ENV_HOST) or DEFAULT_HOST
        self.approval = approval
        self.max_tool_rounds = int(max_tool_rounds)
        self.max_budget_usd = None if max_budget_usd is None else float(max_budget_usd)
        self.cwd = cwd
        self.cli_path = cli_path
        #: The test seam: returns the callable used in place of the SDK's `query`, so
        #: no test spawns the binary.
        self.query_factory = query_factory
        #: Per session: the binary's session id and the history it has answered, as
        #: `(role, text)` pairs. See the module docstring on resume.
        self._threads: dict[UUID, tuple[str, tuple[tuple[str, str], ...]]] = {}

    # -- diagnosis ------------------------------------------------------------

    def available(self) -> bool:
        return self.unavailable_reason() is None

    def unavailable_reason(self) -> str | None:
        """Why this cannot run here, in the order someone would fix it."""
        host = HOSTS.get(self.host)
        if host is None:
            return hosts.unknown(self.host, HOSTS)
        if importlib.util.find_spec("claude_agent_sdk") is None:
            return (
                "the Claude Agent SDK is not installed — install "
                "`latent-intel[claude-agent-sdk]`"
            )
        if self.cli_path and shutil.which(self.cli_path) is None:
            return (
                f"cannot find `{self.cli_path}` — fix `cli_path:` under "
                f"`runtimes: {self.id}:`, or remove it to use the bundled binary"
            )
        return hosts.diagnose(host, name=self.host)

    def host_status(self) -> dict[str, HostStatus]:
        """Every host this runtime declares, and what each still needs. See
        `hosts.status`."""
        return hosts.status(HOSTS)

    def family(self) -> str:
        """A vendor's runner, driven from this process, with our tools executed by us.
        See `agent/base.Owned`."""
        return "sdk"

    def subagent_tools(
        self, tools: Sequence[ToolSpec], web: WebScope | None = None
    ) -> frozenset[str]:
        """What a subagent's `tools:` may name here: our tools this turn would offer, as
        `source.tool`, and the web tools `web` turns on. See `agent/base.Delegating`."""
        offered = {spec.qualified for spec in turn.offered(tools, self.approval)}
        return frozenset({*offered, *claude_cli.WEB_TOOLS[(web or WebScope()).mode]})

    def web_reason(self, scope: WebScope) -> str | None:
        """The same binary as `claude-cli`, so the same answer. See
        `claude_cli.web_reason` and `agent/base.WebScoped`."""
        return claude_cli.web_reason(self.id, scope)

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
            import claude_agent_sdk as sdk
        except ImportError as exc:
            yield emitter.emit(
                ev.AgentFailed,
                message=f"the Claude Agent SDK could not be imported: {exc}",
                kind="runtime_unavailable",
                remedy="reinstall `latent-intel[claude-agent-sdk]`",
            )
            return

        # Before anything is spawned — see `refuse_web`.
        if refused := refuse_web(self, options.get("web") or WebScope(), emitter):
            yield refused
            return

        stderr: deque[str] = deque(maxlen=_STDERR_LINES)
        try:
            # `aclosing` because `async for` does not close what it iterates when this
            # generator is closed early — and the SDK must be closed in this task.
            async with contextlib.aclosing(
                self._turn(
                    sdk, messages, tools, emitter=emitter, stderr=stderr, **options
                )
            ) as events:
                async for event in events:
                    yield event
        except Exception as exc:  # noqa: BLE001 — a failure is an event, not a crash
            # Cancellation derives from BaseException and is deliberately not caught:
            # a cancelled turn has to close the stream rather than report itself. The
            # thread is kept: `_turn` drops it only when a resume itself was refused.
            yield emitter.emit(ev.AgentFailed, **self._failure(sdk, exc, stderr))

    async def _turn(
        self,
        sdk: Any,
        messages: list[Message],
        tools: list[ToolSpec],
        *,
        emitter: ev.Emitter,
        stderr: deque[str],
        **options: Any,
    ) -> AsyncGenerator[ev.AgentEvent, None]:
        """The binary's messages, translated. `stream` is only the exception vocabulary.

        Ends with exactly one terminal event on every path it returns from; an SDK
        exception before the result leaves through `stream` instead.
        """
        call_tool = options.get("call_tool")
        sources = options.get("sources") or []
        relay = bridge.Relay()

        # Raises on a wire-name collision, before anything is spawned; `stream` turns it
        # into the one terminal event this turn gets. The cap leaves room for the prefix
        # the binary puts on every name it serves.
        wired = turn.wired(tools, self.approval, limit=_MAX_NAME - len(PREFIX))
        # `stream` refused any scope this runtime cannot honour.
        builtins = list(claude_cli.WEB_TOOLS[(options.get("web") or WebScope()).mode])
        delegates: Sequence[Subagent] = options.get("agents") or []
        #: The binary's own tools this turn offers, with their declared effects.
        harness = dict.fromkeys(builtins, Effect.EXTERNAL_READ)
        if delegates:
            harness[AGENT_TOOL] = Effect.NONE
        #: The binary runs these itself, so their pairs are built from its messages
        #: rather than by our router: tool-use id → the start and when it was seen.
        opened: dict[str, tuple[ev.ToolStarted, float]] = {}
        server = sdk.create_sdk_mcp_server(
            name=SERVER,
            tools=[
                self._tool(
                    sdk, name, spec, emitter=emitter, relay=relay, router=call_tool
                )
                for name, spec in wired
            ],
        )
        system = turn.system_prompt(
            sources,
            persona=options.get("persona") or "",
            persona_mode=options.get("persona_mode") or "append",
            skills=options.get("skills") or (),
            # The name the binary offers the tool under, prefix and all: a prompt
            # naming the bare wire name sends the model after a tool it does not have.
            loader=turn.loader([(PREFIX + name, spec) for name, spec in wired]),
            invoked=options.get("invoked") or (),
        )
        history: list[tuple[str, str]] = [
            (message.role, message.text) for message in messages
        ]
        resume = self._resumable(emitter.session_id, history)
        query = self.query_factory() if self.query_factory else sdk.query
        request = sdk.ClaudeAgentOptions(
            **self._options(
                server if wired else None,
                tools=list(harness),
                allowed=[
                    *(PREFIX + name for name, _ in wired),
                    *builtins,
                    # Each project agent by name, never the bare tool: that would also
                    # admit Claude Code's own subagents, which the project never
                    # declared and whose default model a host may not serve.
                    *(f"{AGENT_TOOL}({delegate.name})" for delegate in delegates),
                ],
                agents=self._agents(sdk, delegates, wired, builtins),
                system=system,
                resume=resume,
                stderr=stderr,
            )
        )

        began = time.monotonic()
        answer: list[str] = []
        separate = False
        result: Any = None
        try:
            # `aclosing`, so the SDK's generator — and with it the binary — is closed
            # in this task on every exit, not left for the garbage collector to
            # finalize somewhere else: the hazard `bridge.py` records.
            async with contextlib.aclosing(
                query(prompt=history[-1][1] if history else "", options=request)
            ) as incoming:
                async for message in incoming:
                    # What the relay holds was stamped before anything this message
                    # produces, and nothing is awaited between this drain and the last
                    # emit below — so what leaves is in `sequence` order, however many
                    # tools the binary runs side by side.
                    out: list[ev.AgentEvent] = relay.drain()
                    if isinstance(message, sdk.StreamEvent):
                        text = _text_delta(message)
                        if text:
                            # Text after a tool round is a new block; run straight on
                            # from the text before the call it reads "…let me
                            # check.Here is".
                            if separate and answer:
                                answer.append("\n\n")
                                out.append(emitter.emit(ev.AssistantToken, text="\n\n"))
                            separate = False
                            answer.append(text)
                            out.append(emitter.emit(ev.AssistantToken, text=text))
                    elif isinstance(message, sdk.AssistantMessage):
                        # A text block repeats what the deltas already streamed, so
                        # only a tool call is read here: the break it implies, and the
                        # start of a pair when the binary runs the tool itself.
                        for block in message.content:
                            if not isinstance(block, sdk.ToolUseBlock):
                                continue
                            if message.parent_tool_use_id is None:
                                separate = True
                            if block.name in harness:
                                started = emitter.emit(
                                    ev.ToolStarted,
                                    tool=block.name,
                                    # A web call is the web's, as on every runtime;
                                    # a hand-off is this harness's own.
                                    source_id=self.id
                                    if block.name == AGENT_TOOL
                                    else WEB_ID,
                                    arguments=dict(block.input or {}),
                                    effect=str(harness[block.name]),
                                )
                                opened[block.id] = (started, time.monotonic())
                                out.append(started)
                    elif isinstance(message, sdk.UserMessage):
                        for block in _blocks(message.content):
                            if not isinstance(block, sdk.ToolResultBlock):
                                continue
                            pending = opened.pop(block.tool_use_id, None)
                            if pending is None:
                                continue  # ours: the bridge reported it
                            started, at = pending
                            text = turn.result_text(block.content)
                            failed = bool(block.is_error)
                            out.append(
                                emitter.nested(started).emit(
                                    ev.ToolResult,
                                    tool=started.tool,
                                    source_id=started.source_id,
                                    ok=not failed,
                                    output="" if failed else text,
                                    error=text if failed else "",
                                    duration_ms=int((time.monotonic() - at) * 1000),
                                    refs=[]
                                    if failed
                                    else claude_cli.web_refs(
                                        started.tool, started.arguments
                                    ),
                                )
                            )
                    elif isinstance(message, sdk.ResultMessage):
                        result = message
                    for event in out:
                        yield event
        except Exception as exc:
            # The binary exits non-zero after an error result, and the SDK raises for it
            # after yielding that result: the result has already said what happened.
            # Anything else ends the turn here — but only after everything that ran is
            # reported and no start is left open for a renderer to wait on.
            if result is None or not isinstance(exc, sdk.ClaudeSDKError):
                for event in _closing(relay, opened, emitter):
                    yield event
                if resume is not None and _refused(sdk, exc):
                    self._threads.pop(emitter.session_id, None)
                raise

        for event in _closing(relay, opened, emitter):
            yield event

        if result is None:
            if resume is not None:
                self._threads.pop(emitter.session_id, None)
            yield emitter.emit(
                ev.AgentFailed,
                message="the agent ended without reporting a result",
                kind="unexpected_stop",
                remedy="ask again",
            )
            return

        if result.is_error:  # never `subtype` alone — see `claude_cli.py`
            if (
                resume is not None
                and not result.num_turns
                and result.api_error_status is None
            ):
                # The resume itself was refused — nothing ran and no endpoint answered.
                # A rate limit or a 5xx also runs nothing, but that session is still
                # there to resume, and dropping it would forget the conversation.
                self._threads.pop(emitter.session_id, None)
            yield emitter.emit(
                ev.AgentFailed,
                **self._classify(
                    result.subtype,
                    result.api_error_status,
                    result.result or "; ".join(result.errors or ()),
                ),
            )
            return

        text = "".join(answer)
        streamed = bool(text)
        if not streamed:
            text = result.result or ""
        self._threads[emitter.session_id] = (
            result.session_id,
            (*history, ("assistant", text)),
        )
        yield emitter.emit(
            ev.AgentCompleted,
            text=text,
            streamed=streamed,
            usage=_usage(result, int((time.monotonic() - began) * 1000)),
            cost_usd=result.total_cost_usd,
        )

    def _options(
        self,
        server: Any | None,
        *,
        tools: list[str],
        allowed: list[str],
        agents: dict[str, Any] | None,
        system: str,
        resume: str | None,
        stderr: deque[str],
    ) -> dict[str, Any]:
        """The `ClaudeAgentOptions` fields, as a plain mapping a test can read."""
        prompt: dict[str, Any] = {"type": "preset", "preset": "claude_code"}
        if system:
            prompt["append"] = system
        return {
            "model": self.model,
            "system_prompt": prompt,
            "tools": tools,
            "allowed_tools": allowed,
            "agents": agents,
            "mcp_servers": {SERVER: server} if server is not None else {},
            "strict_mcp_config": True,
            "setting_sources": [],
            "skills": [],
            "permission_mode": "dontAsk",
            "verbatim_prompts": True,
            "include_partial_messages": True,
            "max_turns": self.max_tool_rounds,
            "max_budget_usd": self.max_budget_usd,
            "resume": resume,
            "cwd": self.cwd,
            "cli_path": self.cli_path,
            "env": environment(self.host),
            "stderr": stderr.append,
        }

    def _agents(
        self,
        sdk: Any,
        delegates: Sequence[Subagent],
        wired: Sequence[tuple[str, ToolSpec]],
        builtins: Sequence[str],
    ) -> dict[str, Any] | None:
        """The project's subagents as the SDK's definitions, their tools renamed to
        what the binary calls them.

        A name that resolves to nothing is dropped, and `doctor` names it. An agent
        whose every name was dropped gets an empty list rather than none: `None` would
        hand it every tool the main agent has, the opposite of what it asked for.
        `disallowedTools` is renamed the same way, and a name that is not ours passes
        through as it is — Claude Code knows its own tools, and denying one it lacks is
        harmless.
        """
        if not delegates:
            return None
        names = {spec.qualified: PREFIX + name for name, spec in wired}
        names.update({name: name for name in builtins})
        return {
            delegate.name: sdk.AgentDefinition(
                description=delegate.description,
                prompt=delegate.prompt,
                tools=None
                if delegate.tools is None
                else [names[tool] for tool in delegate.tools if tool in names],
                disallowedTools=[names.get(tool, tool) for tool in delegate.disallowed]
                or None,
                model=delegate.model,
            )
            for delegate in delegates
        }

    def _resumable(self, session: UUID, history: list[tuple[str, str]]) -> str | None:
        """The binary's session to resume, or None to start a fresh one.

        Only when the history continues the one recorded: everything answered then, in
        order, followed by nothing but questions — the last one this turn's, any before
        it from turns that failed, which the session appended without an answer.
        """
        thread = self._threads.get(session)
        if thread is None or not history or history[-1][0] != "user":
            return None
        session_id, seen = thread
        prior = history[:-1]
        if tuple(prior[: len(seen)]) != seen:
            return None
        if any(role != "user" for role, _ in prior[len(seen) :]):
            return None
        return session_id

    def _tool(
        self,
        sdk: Any,
        name: str,
        spec: ToolSpec,
        *,
        emitter: ev.Emitter,
        relay: bridge.Relay,
        router: turn.ToolRouter | None,
    ) -> Any:
        """One wired tool, as the SDK's in-process MCP tool.

        A failure is returned with `is_error` rather than raised: that is the MCP shape
        for a tool error, and the connector's own message is what the model needs to
        correct the call.
        """

        async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
            try:
                output = await bridge.run(
                    spec,
                    emitter=emitter,
                    relay=relay,
                    call_tool=router,
                    arguments=dict(arguments),
                )
            except bridge.BridgeError as exc:
                return {
                    "content": [{"type": "text", "text": str(exc)}],
                    "is_error": True,
                }
            return {"content": [{"type": "text", "text": output}]}

        return sdk.SdkMcpTool(
            name=name,
            description=spec.description,
            input_schema=turn.input_schema(spec),
            handler=handler,
        )

    def _classify(
        self, subtype: str | None, status: int | None, text: str
    ) -> dict[str, str]:
        """An error result — or the exception carrying one — as `AgentFailed` fields."""
        row = HOSTS[self.host]
        if subtype == "error_max_turns":
            return {
                "message": f"stopped after {self.max_tool_rounds} rounds of tool calls",
                "kind": "tool_rounds",
                "remedy": "raise `max_tool_rounds` under this runtime, or ask for less",
            }
        if subtype == "error_max_budget_usd":
            return {
                "message": f"stopped at the budget of ${self.max_budget_usd}",
                "kind": "budget",
                "remedy": "raise `max_budget_usd` under this runtime, or ask for less",
            }
        fields = turn.status_failure(status, host=row, name=self.host, model=self.model)
        if fields is not None:
            return fields
        if status is not None:
            return {
                "message": f"the endpoint returned {status}: {text}"
                if text
                else f"the endpoint returned {status}",
                "kind": "api_error",
                "remedy": "",
            }
        return {
            "message": text or "the agent reported a failure",
            "kind": subtype or "agent_error",
            "remedy": "",
        }

    def _failure(
        self, sdk: Any, exc: BaseException, stderr: deque[str]
    ) -> dict[str, str]:
        """The `AgentFailed` fields for an exception a turn raised. Its own ladder:
        `turn.failure` reads the HTTP client SDKs' classes, and these are the SDK's
        process and protocol errors instead."""
        if isinstance(exc, sdk.CLINotFoundError):
            return {
                "message": str(exc) or "the Claude Code binary was not found",
                "kind": "runtime_unavailable",
                "remedy": (
                    "reinstall `latent-intel[claude-agent-sdk]`, or fix `cli_path:`"
                ),
            }
        if isinstance(exc, sdk.CLIConnectionError):
            return {
                "message": str(exc) or "could not start the Claude Code binary",
                "kind": "connection",
                "remedy": "check `cwd:` and `cli_path:` under this runtime",
            }
        result_error = getattr(sdk, "ResultError", None)
        if result_error is not None and isinstance(exc, result_error):
            return self._classify(
                getattr(exc, "subtype", None),
                getattr(exc, "api_error_status", None),
                str(getattr(exc, "result", None) or exc),
            )
        if isinstance(exc, sdk.ProcessError):
            tail = "\n".join(stderr)
            return {
                "message": tail or str(exc) or "the Claude Code binary failed",
                "kind": "runtime_error",
                "remedy": "",
            }
        return {
            "message": str(exc) or type(exc).__name__,
            "kind": "runtime_error",
            "remedy": "",
        }


def _blocks(content: Any) -> list[Any]:
    """A user message's content is a string or a list of blocks; only the list can
    carry a tool result."""
    return content if isinstance(content, list) else []


def _closing(
    relay: bridge.Relay,
    opened: dict[str, tuple[ev.ToolStarted, float]],
    emitter: ev.Emitter,
) -> list[ev.AgentEvent]:
    """What the relay still holds, then a failed result for every tool that started and
    never answered — ours and the binary's alike — so a turn that ends early leaves no
    start open for a renderer to wait on."""
    reason = "the turn ended before this tool reported a result"
    out = [*relay.drain(), *relay.abandon(emitter, reason)]
    for started, at in opened.values():
        out.append(
            emitter.nested(started).emit(
                ev.ToolResult,
                tool=started.tool,
                source_id=started.source_id,
                ok=False,
                error=reason,
                duration_ms=int((time.monotonic() - at) * 1000),
            )
        )
    opened.clear()
    return out


def _refused(sdk: Any, exc: BaseException) -> bool:
    """Whether a resumed turn's exception looks like the resume itself failing: the
    binary gave up without an endpoint answering. A status means the endpoint was
    reached, and the session is still there to resume."""
    return isinstance(exc, sdk.ProcessError) and (
        getattr(exc, "api_error_status", None) is None
    )


def _text_delta(message: Any) -> str:
    """The text a stream event adds to the answer, or "".

    The main agent's only: a subagent's stream carries its parent's tool-use id, and its
    words are its report to that agent, not the answer.
    """
    if message.parent_tool_use_id is not None:
        return ""
    event = message.event or {}
    if event.get("type") != "content_block_delta":
        return ""
    delta = event.get("delta") or {}
    if delta.get("type") != "text_delta":
        return ""
    return str(delta.get("text") or "")


def _usage(result: Any, elapsed_ms: int) -> dict[str, int]:
    """Integer usage only — `AgentCompleted.usage` is `dict[str, int]`, and the cost is
    a float carried on its own field."""
    raw = result.usage or {}
    out = {
        key: int(raw[key]) for key in turn.USAGE_KEYS if isinstance(raw.get(key), int)
    }
    out["duration_ms"] = int(result.duration_ms or elapsed_ms)
    out["num_turns"] = int(result.num_turns or 0)
    return out


__all__ = [
    "AGENT_TOOL",
    "DEFAULT_HOST",
    "DEFAULT_MODEL",
    "ENV_HOST",
    "HOSTS",
    "PREFIX",
    "SERVER",
    "ClaudeAgentSdkRuntime",
    "environment",
]
