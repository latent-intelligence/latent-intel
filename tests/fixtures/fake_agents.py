"""A scripted Agents SDK runner, shaped exactly like the one the SDK returns.

**The runner is faked, never the network.** `OpenAIAgentsRuntime.runner_factory` returns
the object whose `run_streamed` is called, so this stands in for `agents.Runner` and
nothing below it: no model, no client, no HTTP. Faking the model instead would leave the
part that matters most — that **the runner calls our tools**, between the model
response and the next request — entirely untested, and the bridge, the relay and the
ordering they produce with it.

The event shapes are `SimpleNamespace`s rather than SDK classes, on purpose: the runtime
switches on the string `type` and `name` fields, so a fake that needed the real classes
would be testing a coupling the adapter deliberately does not have.

The order one round produces is the SDK's own, from `_run_single_turn_streamed`: the raw
response events stream first and end with `response.completed`, and only then are the
tools executed and their `tool_called`/`tool_output` items emitted.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from agents.exceptions import MaxTurnsExceeded, ModelBehaviorError


@dataclass
class InputTokensDetails:
    cached_tokens: int | None = None
    cache_write_tokens: int | None = None


@dataclass
class Usage:
    """The Responses-shaped usage the chat stream handler synthesises."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    input_tokens_details: InputTokensDetails | None = None


@dataclass
class Round:
    """One model round-trip: what streams, what it asks for, or what it raises."""

    text: tuple[str, ...] = ()
    #: `(call_id, name, arguments as a JSON string)` per call. A string because that is
    #: what `on_invoke_tool` is handed, which is what makes unparseable arguments a
    #: case this fixture can express at all.
    tool_calls: tuple[tuple[str, str, str], ...] = ()
    usage: Usage | None = field(
        default_factory=lambda: Usage(input_tokens=1, output_tokens=1)
    )
    #: The run's assembled output, where it differs from what streamed — a host that
    #: sends no text deltas still produces a message the run assembles. `None` means it
    #: is the streamed text, which is the ordinary case.
    final: str | None = None
    #: Raised after the round's events, which is when the real stream re-raises: the
    #: exception is stored and surfaces once the event queue has drained.
    raises: BaseException | None = None


class FakeRunner:
    """`agents.Runner`, over scripted rounds.

    Usable directly as `runner_factory`, which is called with no arguments.
    """

    def __init__(self, *rounds: Round) -> None:
        self.rounds = list(rounds)
        #: What each run was given: the agent, the input items, the ceiling and the run
        #: config. A tool definition that never reaches the agent is a bug no assertion
        #: about events would catch.
        self.runs: list[dict[str, Any]] = []
        #: `(name, arguments json)` per tool the *runner* invoked. The runtime under
        #: test never appends here, so this is the assertion that execution went
        #: through the SDK.
        self.calls: list[tuple[str, str]] = []

    def __call__(self) -> FakeRunner:
        return self

    def run_streamed(
        self,
        agent: Any,
        *,
        input: Any,
        max_turns: int,
        run_config: Any,
    ) -> _Result:
        self.runs.append(
            {
                "agent": agent,
                # Copied, not referenced: the transcript is a list, and recording it by
                # reference would show every run the state of the last one.
                "input": list(input),
                "max_turns": max_turns,
                "run_config": run_config,
            }
        )
        return _Result(self, agent, max_turns, run_config)


class _Result:
    """`RunResultStreaming`, minus everything the adapter does not read."""

    def __init__(
        self, runner: FakeRunner, agent: Any, max_turns: int, run_config: Any
    ) -> None:
        self._runner = runner
        self._agent = agent
        self._max_turns = max_turns
        self._run_config = run_config
        self.final_output: Any = None

    async def stream_events(self) -> AsyncIterator[Any]:
        by_name = {tool.name: tool for tool in self._agent.tools}
        for index, scripted in enumerate(self._runner.rounds):
            # `current_turn` is incremented and then compared with `>`, so the turn
            # after the last permitted one is the one that raises.
            if index >= self._max_turns:
                raise MaxTurnsExceeded(f"Max turns ({self._max_turns}) exceeded")

            for chunk in scripted.text:
                yield _raw("response.output_text.delta", delta=chunk)
            yield _raw(
                "response.completed",
                response=SimpleNamespace(usage=scripted.usage),
            )

            if scripted.raises is not None:
                raise scripted.raises

            for call_id, name, raw_arguments in scripted.tool_calls:
                tool = by_name.get(name)
                if tool is None:
                    # `tool_not_found_behavior`, as the SDK reads it off the run's
                    # config: the call never reaches a tool object either way. The
                    # default raises and the run ends; `return_error_to_model` records
                    # the call, hands the model an error output, and carries on.
                    error = f"Tool {name} not found in agent {self._agent.name}"
                    behaviour = getattr(
                        self._run_config, "tool_not_found_behavior", "raise_error"
                    )
                    if behaviour != "return_error_to_model":
                        raise ModelBehaviorError(error)
                    yield _item(
                        "tool_called", SimpleNamespace(call_id=call_id, name=name)
                    )
                    yield _item(
                        "tool_output", SimpleNamespace(call_id=call_id, output=error)
                    )
                    continue
                yield _item("tool_called", SimpleNamespace(call_id=call_id, name=name))
                self._runner.calls.append((name, raw_arguments))
                output = await tool.on_invoke_tool(SimpleNamespace(), raw_arguments)
                yield _item(
                    "tool_output", SimpleNamespace(call_id=call_id, output=output)
                )

            if not scripted.tool_calls:
                self.final_output = (
                    scripted.final
                    if scripted.final is not None
                    else "".join(scripted.text)
                )
                return

        # Every scripted round asked for tools, so the run would open one more request
        # than the ceiling allows.
        raise MaxTurnsExceeded(f"Max turns ({self._max_turns}) exceeded")


def _raw(kind: str, **data: Any) -> SimpleNamespace:
    return SimpleNamespace(
        type="raw_response_event", data=SimpleNamespace(type=kind, **data)
    )


def _item(name: str, item: Any) -> SimpleNamespace:
    return SimpleNamespace(type="run_item_stream_event", name=name, item=item)
