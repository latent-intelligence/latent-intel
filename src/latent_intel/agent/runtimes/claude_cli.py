"""`claude -p`, streaming JSON, as an event source.

The runtime that needs no API key: it borrows the auth the user already has. In exchange
it owns its own agent loop, so our tool router never runs — our sources reach it as
`--mcp-config` servers and it decides when to call them. Both halves of that trade stay
inside this file; what comes out is the event stream every other runtime produces.

Four things were verified against the real binary rather than assumed, and each is a
trap:

- **`--output-format stream-json` requires `--verbose`.** Without it the CLI prints an
  error to *stdout* and exits **0**, which reads as a successful empty turn.
- **`is_error` is the truth; `subtype` is not.** A model-not-found run emits
  `{"is_error": true, "subtype": "success", "api_error_status": 404}`. Branching on
  `subtype` would report a failed turn as a good one.
- **stdout carries non-JSON lines.** `[claude-code:unrecognized_model] {...}` arrives
  between JSON objects. Unparseable lines are skipped, never fatal.
- **`usage` is not our shape.** `events.AgentCompleted.usage` is `dict[str, int]`;
  claude's has nested dicts and a float cost, which pydantic rejects.

**Built-in tools are off.** `claude -p` defaults to a full coding agent — `Bash`,
`Edit`, `Write`. `latent-intel` is an access layer over context; a runtime that can
shell out on your machine is a far larger promise than `intel ask` makes, and enabling
it by omission is not a decision anyone took.

**There is no interactive approval under `--print`.** Nobody is there to answer, so the
allow-list has to be decided up front. It is derived from each `ToolSpec`'s declared
`Effect`: under `approval: ask` or `never` only non-writing tools are admitted, under
`auto` writes are too. `ask` therefore means the cautious end until this package grows a
real approval channel — stated here because silently treating it as `never` would be a
lie about a setting the user chose.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
from collections import deque
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from subprocess import PIPE
from typing import Any

import anyio
from anyio.abc import Process

from ... import events as ev
from ...models import Effect, Message, ToolSpec
from .. import turn

#: Usage keys that are integers in claude's payload. Everything else there is a nested
#: dict or a float, and `AgentCompleted.usage` is `dict[str, int]`.
_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)

#: Effects that change something. `DESTRUCTIVE` belongs here for the obvious reason, and
#: leaving it out would have admitted the worst tools under the cautious setting.
_WRITES = frozenset({Effect.LOCAL_WRITE, Effect.EXTERNAL_WRITE, Effect.DESTRUCTIVE})

#: How much stderr to keep for a failure message. The tail is what matters; a runaway
#: server logging megabytes must not be held in memory.
_STDERR_LINES = 20

#: A single JSON line can carry a whole tool result. Generous, but bounded.
_MAX_LINE = 8 * 1024 * 1024

#: The one line of an npm `cmd-shim` that matters: the program it runs, named relative
#: to the shim's own directory (`%dp0%`). Current builds point at `bin\claude.exe`;
#: older ones at a `cli.js` run through `node`.
_SHIM_TARGET = re.compile(r'"%dp0%\\([^"]+)"')


def resolve_command(command: Sequence[str]) -> list[str] | None:
    """A launchable argv prefix for `command`, or None when its head cannot be found.

    `available()` and `stream()` both use this, so they cannot disagree — and on
    Windows they did. `shutil.which` honours PATHEXT and finds the npm shim
    `claude.cmd`, while `CreateProcess` given the bare name looks for `claude.exe` and
    raises `WinError 2`. The check said yes; the spawn said no.

    Launching the resolved shim would fix the crash and nothing else. A `.cmd` runs
    through `cmd.exe`, which caps the command line at 8191 characters, re-parses every
    argument — the JSON in `--mcp-config` included — and, when terminated, exits
    leaving the program it started running. So a shim is read and its target launched
    directly: an `.exe` as is, a script through `node`. No `cmd.exe` in the chain.
    """
    if not command:
        return None
    head, *rest = command
    found = shutil.which(head)
    if found is None:
        return None
    return [*_unwrap_shim(Path(found)), *rest]


def _unwrap_shim(path: Path) -> list[str]:
    """What a Windows batch shim actually runs, or the shim itself when unclear."""
    if path.suffix.lower() not in (".cmd", ".bat"):
        return [str(path)]
    try:
        body = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [str(path)]
    match = _SHIM_TARGET.search(body)
    if match is None:
        return [str(path)]
    target = path.parent.joinpath(*match.group(1).split("\\"))
    if not target.is_file():
        return [str(path)]
    suffix = target.suffix.lower()
    if suffix == ".exe":
        return [str(target)]
    if suffix in (".js", ".cjs", ".mjs") and (node := shutil.which("node")):
        return [node, str(target)]
    return [str(path)]


def sanitise(name: str) -> str:
    """Fold a source id the way claude folds an MCP server name.

    Observed live: a server named `claude.ai Hugging Face` becomes
    `mcp__claude_ai_Hugging_Face__hf_whoami`. Not invertible, so callers build a lookup
    rather than trying to reverse it.
    """
    return re.sub(r"[^0-9A-Za-z]", "_", name)


def allowed_tools(
    tools: Sequence[ToolSpec], servers: dict[str, Any], approval: str
) -> list[str]:
    """Which qualified tool names the agent may call.

    This is where `ToolSpec.effect` finally does work — it has been declared since the
    connector seam was written and consumed by nothing.
    """
    permitted: list[str] = []
    for spec in tools:
        if spec.source_id not in servers:
            continue  # the agent cannot reach this source at all
        writes = spec.effect in _WRITES
        if writes and approval != "auto":
            continue
        permitted.append(f"mcp__{sanitise(spec.source_id)}__{spec.name}")
    return permitted


def build_argv(
    command: Sequence[str],
    *,
    model: str | None = None,
    servers: dict[str, Any] | None = None,
    allow: Sequence[str] = (),
    system_prompt: str = "",
) -> list[str]:
    """The command line, as a pure function.

    Separated because it is the part most likely to be wrong and the cheapest to test:
    the flag matrix is covered without spawning anything.
    """
    argv = [
        *command,
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",  # required with stream-json; without it, an error and exit 0
        "--include-partial-messages",  # what makes AssistantToken real, not one lump
        "--tools",
        "",  # no Bash/Edit/Read — this is not a coding agent
    ]
    if model:
        argv += ["--model", model]
    # Always, even with no servers. `--strict-mcp-config` is what stops the agent
    # inheriting whatever MCP servers the user has configured globally, and gating it on
    # `servers` inverted it exactly when it mattered: a session that attached nothing
    # servable fell back to the ambient config and answered from tools this session
    # never granted. Observed — with no wiki server the agent reported the operator's
    # own unrelated MCP tools as its context. No servers must mean no tools.
    argv += ["--mcp-config", json.dumps({"mcpServers": servers or {}})]
    argv += ["--strict-mcp-config"]
    if allow:
        argv += ["--allowedTools", *allow]
    if system_prompt:
        argv += ["--append-system-prompt", system_prompt]
    return argv


def _usage(payload: dict[str, Any]) -> dict[str, int]:
    """Integer usage only — see the module docstring."""
    raw = payload.get("usage") or {}
    out = {k: int(raw[k]) for k in _USAGE_KEYS if isinstance(raw.get(k), int)}
    for key in ("duration_ms", "num_turns"):
        if isinstance(payload.get(key), int):
            out[key] = int(payload[key])
    return out


class ClaudeCliRuntime:
    """The `claude` binary, behind the `Runtime` Protocol."""

    id = "claude-cli"

    def __init__(
        self,
        *,
        model: str | None = None,
        command: str | Sequence[str] = "claude",
        approval: str = "ask",
        cwd: str | None = None,
        **_: Any,
    ) -> None:
        self.model = model
        #: A string is split; a sequence is taken as-is, so a test can point at a fake
        #: interpreter without touching PATH.
        self.command: list[str] = (
            shlex.split(command) if isinstance(command, str) else list(command)
        )
        self.approval = approval
        self.cwd = cwd

    def available(self) -> bool:
        return resolve_command(self.command) is not None

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        *,
        emitter: ev.Emitter,
        **options: Any,
    ) -> AsyncIterator[ev.AgentEvent]:
        servers: dict[str, Any] = options.get("mcp_servers") or {}
        sources = options.get("sources") or []
        prompt = messages[-1].text if messages else ""

        resolved = resolve_command(self.command)
        if resolved is None:
            yield emitter.emit(
                ev.AgentFailed,
                message=f"cannot find `{self.command[0] if self.command else ''}`",
                kind="runtime_error",
                remedy="install Claude Code, or set `command:` under this runtime",
            )
            return
        argv = build_argv(
            resolved,
            model=self.model,
            servers=servers,
            allow=allowed_tools(tools, servers, self.approval),
            system_prompt=turn.system_prompt(sources),
        )
        declared = {(t.source_id, t.name): t for t in tools}
        by_server = {sanitise(s): s for s in servers}
        started: dict[str, ev.ToolStarted] = {}
        stderr_tail: deque[str] = deque(maxlen=_STDERR_LINES)
        saw_result = False

        # Failures inside `run()` are events: a frontend iterating over a transport has
        # nowhere to catch an exception, and a spawn that fails must say so in-band.
        try:
            opened = await anyio.open_process(
                argv, stdin=PIPE, stdout=PIPE, stderr=PIPE, cwd=self.cwd
            )
        except OSError as exc:
            yield emitter.emit(
                ev.AgentFailed,
                message=f"could not start {argv[0]}: {exc}",
                kind="runtime_error",
                remedy="run `claude doctor` to check the CLI itself",
            )
            return

        async with opened as process:
            try:
                async with anyio.create_task_group() as tasks:
                    tasks.start_soon(self._feed, process, prompt)
                    tasks.start_soon(self._drain, process, stderr_tail)
                    async for line in _lines(process):
                        try:
                            payload = json.loads(line)
                        except json.JSONDecodeError:
                            stderr_tail.append(line[:200])
                            continue  # claude prints non-JSON here; never fatal
                        if not isinstance(payload, dict):
                            continue
                        for event in self._map(
                            payload, emitter, declared, by_server, started
                        ):
                            if isinstance(event, ev.AgentCompleted | ev.AgentFailed):
                                saw_result = True
                            yield event
            finally:
                # An orphaned `claude` after Ctrl-C is the same bug this package already
                # recorded for MCP servers inheriting a pipeline's stdout.
                with anyio.CancelScope(shield=True):
                    if process.returncode is None:
                        process.terminate()

        if not saw_result:
            code = process.returncode
            yield emitter.emit(
                ev.AgentFailed,
                message="\n".join(stderr_tail)
                or f"claude exited {code} with no result",
                kind="runtime_error",
                remedy="run `claude doctor` to check the CLI itself",
            )

    # -- internals ------------------------------------------------------------

    async def _feed(self, process: Process, prompt: str) -> None:
        """The question goes on stdin — it dodges ARG_MAX and keeps the text out of
        `ps` on a shared machine."""
        if process.stdin is None:
            return
        async with process.stdin:
            await process.stdin.send(prompt.encode())

    async def _drain(self, process: Process, tail: deque[str]) -> None:
        if process.stderr is None:
            return
        async for chunk in process.stderr:
            for line in chunk.decode(errors="replace").splitlines():
                if line.strip():
                    tail.append(line)

    def _map(
        self,
        payload: dict[str, Any],
        emitter: ev.Emitter,
        declared: dict[tuple[str, str], ToolSpec],
        by_server: dict[str, str],
        started: dict[str, ev.ToolStarted],
    ) -> list[ev.AgentEvent]:
        kind = payload.get("type")

        if kind == "stream_event":
            delta = (payload.get("event") or {}).get("delta") or {}
            if delta.get("type") == "text_delta" and delta.get("text"):
                return [emitter.emit(ev.AssistantToken, text=str(delta["text"]))]
            return []

        if kind == "assistant":
            out: list[ev.AgentEvent] = []
            for block in (payload.get("message") or {}).get("content") or []:
                # A `text` block repeats what the deltas already streamed. Ignoring it
                # is the de-duplication rule, and why --include-partial-messages is not
                # optional.
                if block.get("type") != "tool_use":
                    continue
                tool = str(block.get("name") or "")
                source_id, bare = self._split(tool, by_server)
                spec = declared.get((source_id, bare))
                event = emitter.emit(
                    ev.ToolStarted,
                    tool=bare,
                    source_id=source_id,
                    arguments=dict(block.get("input") or {}),
                    effect=str(spec.effect) if spec else str(Effect.EXTERNAL_WRITE),
                )
                started[str(block.get("id") or "")] = event
                out.append(event)
            return out

        if kind == "user":
            out = []
            for block in (payload.get("message") or {}).get("content") or []:
                if block.get("type") != "tool_result":
                    continue
                parent = started.get(
                    str(block.get("use_id") or block.get("tool_use_id") or "")
                )
                child = emitter.nested(parent) if parent else emitter
                text = _text_of(block.get("content"))
                failed = bool(block.get("is_error"))
                out.append(
                    child.emit(
                        ev.ToolResult,
                        tool=parent.tool if parent else "",
                        source_id=parent.source_id if parent else "claude-cli",
                        ok=not failed,
                        output="" if failed else text,
                        error=text if failed else "",
                    )
                )
            return out

        if kind == "result":
            text = str(payload.get("result") or "")
            if payload.get("is_error"):  # never `subtype` — see the module docstring
                return [
                    emitter.emit(
                        ev.AgentFailed,
                        message=text or "the agent reported a failure",
                        kind=str(payload.get("terminal_reason") or "agent_error"),
                        remedy="",
                    )
                ]
            return [
                emitter.emit(
                    ev.AgentCompleted,
                    text=text,
                    usage=_usage(payload),
                    streamed=True,
                )
            ]

        return []  # system/init, system/status, rate_limit_event

    @staticmethod
    def _split(tool: str, by_server: dict[str, str]) -> tuple[str, str]:
        """`mcp__server__tool` back to (source_id, tool)."""
        if tool.startswith("mcp__"):
            _, _, rest = tool.partition("mcp__")
            server, _, bare = rest.partition("__")
            return by_server.get(server, server), bare or tool
        return "claude-cli", tool


def _text_of(content: Any) -> str:
    """Tool result content is a string or a list of blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(b.get("text", "")) for b in content if isinstance(b, dict)
        ).strip()
    return ""


async def _lines(process: Process) -> AsyncIterator[str]:
    """Split stdout into lines. `anyio` has no line reader, and a tool result can be
    large enough that an unbounded buffer is a denial of service on ourselves."""
    if process.stdout is None:
        return
    buffer = b""
    async for chunk in process.stdout:
        buffer += chunk
        while b"\n" in buffer:
            line, _, buffer = buffer.partition(b"\n")
            if line.strip():
                yield line.decode(errors="replace")
        if len(buffer) > _MAX_LINE:
            buffer = b""
    if buffer.strip():
        yield buffer.decode(errors="replace")


__all__ = ["ClaudeCliRuntime", "allowed_tools", "build_argv", "sanitise"]
