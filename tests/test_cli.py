"""The CLI frontend, and the renderer it shares with the shell.

The renderer tests run against the recorded streams — no store, no network, no model.
That is what makes the frontend-independence claim checkable now rather than once the
web client exists.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console
from typer.testing import CliRunner

from latent_intel import config as config_module
from latent_intel import events as ev
from latent_intel.agent import hosts
from latent_intel.frontends.cli.main import app
from latent_intel.models import Capability, Descriptor, WebScope
from latent_intel.ui import banner, render
from latent_intel.ui.theme import THEME

ROOT_FORMATS = Path(__file__).parent / "fixtures" / "formats" / "recording"
STREAMS = Path(__file__).parent / "fixtures" / "streams"
FIXTURES = sorted(STREAMS.glob("*.jsonl"))

runner = CliRunner()


def capture(width: int = 100) -> Console:
    return Console(theme=THEME, width=width, force_terminal=False, record=True)


#: The loop-owner headings `doctor` groups runtimes under, in printed order.
FAMILY_HEADINGS = ("custom loop", "sdk runner", "delegated", "managed")


def runtime_line(stdout: str, name: str) -> str | None:
    """One runtime's `doctor` row, as a whole stripped line, or None if it has none.

    Anchored to the name rather than matched as a substring, because the names nest:
    `"anthropic" in stdout` is satisfied by `anthropic-sdk`, and `"! anthropic"` by
    `! anthropic-something` — an assertion that passes because a sibling runtime is
    installed is an assertion about nothing.
    """
    for raw in stdout.splitlines():
        parts = raw.split()
        if len(parts) >= 2 and parts[1] == name:
            return raw.strip()
    return None


def runtime_group(stdout: str, name: str) -> str | None:
    """Which loop-owner group one runtime's row was printed under."""
    heading: str | None = None
    for raw in stdout.splitlines():
        stripped = raw.strip()
        if stripped in FAMILY_HEADINGS:
            heading = stripped
            continue
        parts = raw.split()
        if len(parts) >= 2 and parts[1] == name:
            return heading
    return None


def flat(text: str) -> str:
    """One line of whitespace-normalized text.

    A reason long enough to be worth printing is long enough to wrap, and where it
    wraps depends on a terminal width no assertion should depend on.
    """
    return " ".join(text.split())


def hosts_table(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """`intel hosts` read back as {runtime: the host names printed under it}.

    Captured wide so no row wraps, and keyed on the row names the table declares, so a
    runtime-level reason printed above the rows is not mistaken for one of them.
    """
    from latent_intel.frontends import _shared

    console = Console(theme=THEME, width=200, record=True)
    monkeypatch.setattr(_shared, "console", console)
    _shared.print_hosts()

    table: dict[str, list[str]] = {}
    runtime = ""
    for raw in console.export_text().splitlines():
        if raw and not raw.startswith(" "):
            runtime = raw.split()[0]
            continue
        parts = raw.split()
        if runtime and len(parts) >= 2 and parts[1] in hosts.HOSTS:
            table.setdefault(runtime, []).append(parts[1])
    return table


# -- the renderer -----------------------------------------------------------


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_every_recorded_event_renders(path: Path) -> None:
    """A renderer that raised on one event type would make every future addition a
    breaking change for every frontend at once."""
    console = capture()
    for event in ev.read_stream(path.read_text()):
        render.render(console, event)
    assert console.export_text() is not None


def test_an_unknown_event_renders_as_a_line_not_a_crash() -> None:
    console = capture()
    unknown = ev.parse_event(
        {
            "type": "invented_later",
            "session_id": "00000000-0000-4000-8000-000000000000",
            "operation_id": "00000000-0000-4000-8000-000000000001",
        }
    )
    render.render(console, unknown)
    assert "invented_later" in console.export_text()


def test_results_are_labelled_by_source() -> None:
    """The visible half of never merging rankings: two sources, two headings."""
    console = capture()
    for event in ev.read_stream((STREAMS / "search.jsonl").read_text()):
        render.render(console, event)
    text = console.export_text()
    assert "design" in text and "papers" in text
    assert "design:context-collapse" in text


def test_a_failure_shows_its_remedy() -> None:
    console = capture()
    for event in ev.read_stream((STREAMS / "failure.jsonl").read_text()):
        render.render(console, event)
    text = console.export_text()
    assert "no agent runtime configured" in text
    assert "intel doctor" in text


@pytest.mark.parametrize(
    ("stopped", "line"),
    [("tool_rounds", "stopped at the round ceiling"), ("cost", "stopped at the cost")],
)
def test_a_stopped_answer_says_so_after_the_answer(stopped: str, line: str) -> None:
    console = capture()
    events = list(ev.read_stream((STREAMS / "stopped.jsonl").read_text()))
    done = events[-1]
    assert isinstance(done, ev.AgentCompleted) and done.stopped == "tool_rounds"
    render.render(console, done.model_copy(update={"stopped": stopped}))
    assert line in console.export_text()


def test_an_answer_recorded_before_stopped_existed_reads_as_finished() -> None:
    done = [
        e
        for e in ev.read_stream((STREAMS / "agent_turn.jsonl").read_text())
        if isinstance(e, ev.AgentCompleted)
    ]
    assert done and all(e.stopped is None for e in done)
    console = capture()
    for event in done:
        render.render(console, event)
    assert "stopped" not in console.export_text()


@pytest.mark.anyio
async def test_a_stopped_answer_is_not_a_failure_to_a_script() -> None:
    """`intel ask` exits 0 on it: an answer arrived. `stopped` is in --json."""
    from latent_intel.frontends import _shared

    class Stopped:
        async def run(self, command: Any) -> Any:
            for event in ev.read_stream((STREAMS / "stopped.jsonl").read_text()):
                yield event

    assert await _shared.stream(Stopped(), None, as_json=True) is True  # type: ignore[arg-type]


def test_markup_in_content_is_escaped() -> None:
    """A page whose title contains `[bold]` must not restyle the terminal."""
    console = capture()
    render.render(
        console,
        ev.AgentFailed(
            session_id=ev.uuid4(),
            operation_id=ev.uuid4(),
            message="[bold red]not markup[/]",
        ),
    )
    assert "[bold red]not markup[/]" in console.export_text()


# -- the banner -------------------------------------------------------------


def test_banner_shows_the_logo_when_there_is_room() -> None:
    console = capture(width=100)
    banner.render(console, "9.9.9", hints=("/help",))
    text = console.export_text()
    assert "█" in text
    assert "v9.9.9" in text  # inlaid in the border
    assert "nothing attached" in text


def test_banner_drops_the_logo_when_narrow() -> None:
    """A banner that only looks right at one size breaks in a split pane."""
    console = capture(width=40)
    banner.render(console, "9.9.9", ("search", "fetch"), "1 source")
    text = console.export_text()
    assert "█" not in text
    assert "LATENT" in text
    assert "search" in text


def test_banner_names_capabilities_not_sources() -> None:
    """The landing page is the most over-the-shoulder surface the program has; which
    stores someone has attached is the part of it least worth putting there."""
    sources = [
        Descriptor(
            id="a-private-client-wiki",
            kind="wiki",
            capabilities=[Capability.SEARCH, Capability.FETCH],
            count=294,
        ),
        Descriptor(
            id="another-one", kind="files", capabilities=[Capability.SEARCH], count=316
        ),
    ]
    capabilities, scale = banner.summarise(sources)
    assert capabilities == ("search", "fetch")
    assert scale == "2 sources  ·  610 items"

    console = capture(width=100)
    banner.render(console, "0.1.0", capabilities, scale)
    text = console.export_text()
    assert "a-private-client-wiki" not in text
    assert "another-one" not in text
    assert "search" in text and "2 sources" in text


def test_banner_lines_never_exceed_the_terminal() -> None:
    """The failure this catches is a wrapped border, which looks broken rather than
    narrow."""
    for width in (36, 54, 72, 100, 200):
        console = capture(width=width)
        banner.render(
            console,
            "0.1.0",
            ("search", "fetch", "tools"),
            "2 sources  ·  610 items",
            hints=("/help", "/exit"),
        )
        for line in console.export_text().splitlines():
            assert len(line.rstrip()) <= width, f"width={width}: {line!r}"


# -- commands ---------------------------------------------------------------


def test_version_reports_the_event_schema() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "event schema v" in result.stdout


def test_doctor_runs_with_nothing_attached() -> None:
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "connectors" in result.stdout
    assert "nothing attached" in result.stdout


def test_doctor_expects_only_the_kinds_that_ship() -> None:
    """A records store is an `mcp` source on its own server; `vector` is not a kind."""
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "vector —" not in result.stdout
    assert "✓ mcp" in result.stdout


@pytest.mark.parametrize("command", ["doctor", "stores"])
def test_two_servers_of_one_name_get_one_warning_naming_both(command: str) -> None:
    """A name two servers share makes their refs resolve by key, or not at all: said
    before any question is asked."""
    from tests.test_mcp import probe_command

    for source in ("tide", "reef"):
        connected = runner.invoke(
            app, ["connect", probe_command(), "--kind", "mcp", "--as", source]
        )
        assert connected.exit_code == 0, connected.stdout
    result = runner.invoke(app, [command])
    assert result.exit_code == 0, result.stdout
    warnings = [
        line for line in result.stdout.splitlines() if "!" in line and "probe" in line
    ]
    assert len(warnings) == 1, result.stdout
    assert "tide" in warnings[0] and "reef" in warnings[0]


def test_one_server_per_name_gets_no_warning() -> None:
    from tests.test_mcp import probe_command

    runner.invoke(app, ["connect", probe_command(), "--kind", "mcp", "--as", "tide"])
    assert "probe" not in runner.invoke(app, ["doctor"]).stdout


def test_connecting_with_kind_vector_is_refused_with_the_installed_kinds(
    tmp_path: Path,
) -> None:
    result = runner.invoke(app, ["connect", str(tmp_path), "--kind", "vector"])
    assert result.exit_code == 1
    err = flat(result.stderr)
    assert "no connector of kind 'vector'" in err
    assert "mcp" in err


def test_the_kind_help_names_no_store() -> None:
    result = runner.invoke(app, ["connect", "--help"])
    out = flat(result.stdout)
    assert "vector" not in out
    assert "latent-wiki serve" not in out


def test_doctor_names_the_variables_a_runtime_is_missing() -> None:
    """A bare "cannot run here" is not a diagnosis when four variables could each be
    the missing one. The names are safe to print; the values never appear."""
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert runtime_line(result.stdout, "custom") is not None
    assert runtime_line(result.stdout, "anthropic-sdk") is not None
    assert "ANTHROPIC_FOUNDRY_API_KEY" in result.stdout
    assert runtime_line(result.stdout, "openai-agents") is not None
    assert "OPENAI_API_KEY" in result.stdout


def test_doctor_groups_runtimes_by_who_owns_the_loop() -> None:
    """The flat list blended two different things: a runtime whose orchestration is
    ours to configure and one that hands orchestration, tools and permissions to
    another harness. A reader discovered that only by noticing which rows `intel hosts`
    had nothing to say about.

    An empty group is not printed, since a machine with one runtime does not need a
    taxonomy; all four are installed here."""
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    printed = [
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip() in FAMILY_HEADINGS
    ]
    assert printed == ["custom loop", "sdk runner", "delegated", "managed"]
    assert runtime_group(result.stdout, "custom") == "custom loop"
    assert runtime_group(result.stdout, "anthropic-sdk") == "sdk runner"
    assert runtime_group(result.stdout, "openai-agents") == "sdk runner"
    assert runtime_group(result.stdout, "claude-agent-sdk") == "sdk runner"
    assert runtime_group(result.stdout, "claude-cli") == "delegated"
    assert runtime_group(result.stdout, "claude-managed-agents") == "managed"


def test_doctor_lists_custom_and_neither_of_the_names_it_replaced() -> None:
    """`anthropic` and `openai` named a wire protocol, which is a property of the
    endpoint rather than of the loop. They were folded into `custom` on 2026-09-17 and
    removed rather than aliased, so neither has a row here — and `custom`'s own reason
    is the one thing a machine that has not chosen a host needs to read."""
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert runtime_line(result.stdout, "anthropic") is None
    assert runtime_line(result.stdout, "openai") is None
    assert runtime_line(result.stdout, "custom") is not None
    assert "no host is set — set `host:` under `runtimes: custom:`" in flat(
        result.stdout
    )


def test_doctor_says_a_folded_runtime_still_in_config_is_not_installed() -> None:
    """The whole migration story for a machine that has not read the release note: the
    kind it names is gone, and `doctor` is where that is said rather than at the next
    question."""
    config = config_module.load()
    config.runtime = "anthropic"
    config_module.save(config)

    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "anthropic — configured, but not installed" in flat(result.stdout)


def test_doctor_names_an_orphaned_runtimes_block_rather_than_ignoring_it() -> None:
    """Options left under `runtimes:` for a kind nothing provides are read by nobody,
    and a file saying a host and a model are set while neither reaches anything is the
    failure this line exists to name. Display only: the settings layer stays
    kind-agnostic and nothing here rewrites the config."""
    config = config_module.load()
    config.runtime = "custom"
    config.runtimes = {
        "custom": {"host": "openrouter", "model": "vendor/model"},
        "anthropic": {"host": "foundry-anthropic", "model": "claude-sonnet-5"},
    }
    config_module.save(config)

    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert (
        "anthropic — options set under runtimes:, but no such runtime is installed"
        in flat(result.stdout)
    )
    assert runtime_line(result.stdout, "custom") is not None


def test_doctor_names_the_old_sdk_anthropic_name_rather_than_answering_to_it() -> None:
    """`sdk-anthropic` became `anthropic-sdk` and was removed rather than aliased, as
    `anthropic` and `openai` were when folded into `custom`. A config still naming it
    is told so, for the kind and for its options, while the new name has its own row."""
    config = config_module.load()
    config.runtime = "sdk-anthropic"
    config_module.save(config)

    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "sdk-anthropic — configured, but not installed" in flat(result.stdout)
    assert runtime_line(result.stdout, "anthropic-sdk") is not None

    config.runtime = "anthropic-sdk"
    config.runtimes = {"sdk-anthropic": {"host": "anthropic", "model": "m"}}
    config_module.save(config)

    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert (
        "sdk-anthropic — options set under runtimes:, but no such runtime is installed"
        in flat(result.stdout)
    )


def test_doctor_says_when_the_runtime_will_not_run_the_project_s_subagents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A project that declares delegates, answered by a runtime that cannot run them,
    would answer questions it believes it handed off — so doctor names the runtimes
    that can."""
    from latent_intel import settings as settings_module

    projects = tmp_path / "projects"
    (projects / "agents").mkdir(parents=True)
    (projects / "agents" / "reviewer.md").write_text(
        "---\nname: reviewer\ndescription: Reviews.\n---\nReview.\n", encoding="utf-8"
    )
    (projects / "deploy.yaml").write_text("agents: ./agents\n", encoding="utf-8")
    monkeypatch.setenv("LATENT_INTEL_PROJECT", "deploy")
    config = config_module.load()
    config.runtime = "custom"
    config_module.save(config)
    settings_module.invalidate()

    out = flat(runner.invoke(app, ["doctor"]).stdout)
    assert "declares 1 subagent(s), and custom does not run them" in out
    assert "runtimes that do: claude-agent-sdk" in out

    config.runtime = "claude-agent-sdk"
    config_module.save(config)
    assert "does not run them" not in flat(runner.invoke(app, ["doctor"]).stdout)


def test_doctor_builds_each_installed_runtime_exactly_once() -> None:
    """`doctor` asks three questions of every runtime — can it run, who owns its loop,
    which host did it resolve — and a build per question let the verdict describe a
    different object than the row beside it, at three times the cost."""
    from latent_intel.agent import base

    built: list[str] = []
    original = base.build

    def counting(kind: str, **options: Any) -> Any:
        built.append(kind)
        return original(kind, **options)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(base, "build", counting)
        result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert built and sorted(built) == sorted(set(built))


def test_doctor_diagnoses_the_runtime_as_configured_not_bare(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Doctor built every runtime with no arguments, so a configured model read as
    missing and a configured host was never evaluated: the machine was told to set a
    key it does not use, about a model it had already set."""
    monkeypatch.setenv("FOUNDRY_API_KEY", "k")
    monkeypatch.setenv("FOUNDRY_RESOURCE", "r")
    config = config_module.load()
    config.runtime = "custom"
    config.runtimes = {
        "custom": {"model": "gpt-5-deployment", "host": "foundry-openai"}
    }
    config_module.save(config)

    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "✓ custom" in result.stdout
    assert "no model is set" not in result.stdout


def test_doctor_names_a_host_s_third_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The host's variables are diagnosed, not the runtime's: classic Azure OpenAI
    needs an API version as well as a key and an endpoint, and the machine that set the
    usual pair has no other way to learn which one is left."""
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com")
    config = config_module.load()
    config.runtime = "custom"
    config.runtimes = {"custom": {"host": "azure-openai", "model": "m"}}
    config_module.save(config)

    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "OPENAI_API_VERSION" in result.stdout


def test_hosts_lists_every_endpoint_with_what_it_still_needs() -> None:
    """`doctor` diagnoses the configured host; this answers the question a machine has
    before it has chosen one. Names of variables, never values."""
    result = runner.invoke(app, ["hosts"])
    assert result.exit_code == 0
    for name in ("openrouter", "azure-openai", "local"):
        assert name in result.stdout
    assert "OPENROUTER_API_KEY" in result.stdout
    assert "AZURE_OPENAI_ENDPOINT" in result.stdout


def test_hosts_gives_each_runtime_the_rows_it_can_actually_dial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`custom` speaks both protocols and sees the whole table; a runtime built on one
    SDK can dial no other, so listing the rest would offer it endpoints it cannot reach
    and name variables that would not help."""
    table = hosts_table(monkeypatch)
    assert table["custom"] == list(hosts.HOSTS)
    assert table["anthropic-sdk"] == list(hosts.for_sdk("anthropic"))
    assert table["openai-agents"] == list(hosts.for_sdk("openai"))
    assert table["claude-agent-sdk"] == list(hosts.for_sdk("anthropic"))
    assert "claude-cli" not in table


def test_hosts_marks_the_configured_row_and_never_prints_a_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-secret-value")
    config = config_module.load()
    config.runtime = "custom"
    config.runtimes = {"custom": {"host": "openrouter", "model": "vendor/model"}}
    config_module.save(config)

    result = runner.invoke(app, ["hosts"])
    assert result.exit_code == 0
    assert "custom ← configured" in result.stdout
    assert "← in use" in result.stdout
    # The variable is named so a ✓ can be confirmed; its value never appears.
    assert "OPENROUTER_API_KEY" in result.stdout
    assert "sk-secret-value" not in result.stdout


def test_hosts_reports_a_runtime_level_reason_once_not_against_a_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unset model stops every host at once and belongs to none of them, while a
    configured host's missing variables are already printed against that host."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    config = config_module.load()
    config.runtime = "custom"
    config.runtimes = {"custom": {"host": "openrouter"}}
    config_module.save(config)

    result = runner.invoke(app, ["hosts"])
    assert result.exit_code == 0
    assert result.stdout.count("no model is set") == 1


def test_an_unconfigured_runtime_is_not_flagged_as_a_fault() -> None:
    """`!` is for what is in your way. A machine that has finished setting up one
    backend printed warnings about the two it had deliberately ignored, with the one
    that mattered indistinguishable among them."""
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    line = runtime_line(result.stdout, "custom")
    assert line is not None and line.startswith("· custom ")


def test_the_configured_runtime_is_flagged_when_it_cannot_run() -> None:
    """The other half of the same rule: the backend that was chosen and cannot run is
    the one thing worth a warning."""
    config = config_module.load()
    config.runtime = "custom"
    config_module.save(config)

    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    line = runtime_line(result.stdout, "custom")
    assert line is not None and line.startswith("! custom ")


def test_hosts_marks_only_the_answering_runtime_s_host_as_in_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every runtime resolves a host whether or not it is the one answering, so marking
    each of them `configured` said it about endpoints nobody had chosen — beside the one
    they had."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    config = config_module.load()
    config.runtime = "custom"
    config.runtimes = {"custom": {"host": "openrouter", "model": "vendor/model"}}
    config_module.save(config)

    result = runner.invoke(app, ["hosts"])
    assert result.exit_code == 0
    assert result.stdout.count("← in use") == 1
    assert result.stdout.count("← configured") == 1
    # anthropic-sdk resolves `foundry-anthropic` too, and it is nobody's choice here.
    assert "· foundry-anthropic" in result.stdout


def test_hosts_says_so_for_a_runtime_that_declares_none() -> None:
    """Omitting `claude-cli` would read as "not installed" beside two that are
    listed."""
    result = runner.invoke(app, ["hosts"])
    assert "claude-cli — no hosts" in result.stdout


def test_doctor_flags_a_configured_runtime_that_is_not_installed() -> None:
    """A stale kind from an older build, a typo, or a plugin whose extra is missing had
    no row at all — so doctor looked healthy while `ask` failed pointing back at it."""
    config = config_module.load()
    config.runtime = "api"
    config_module.save(config)

    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "api — configured, but not installed" in result.stdout


def test_connect_records_the_source_and_search_finds_it(tmp_path: Path) -> None:
    """The CLI is a new process each time, so 'attached' has to survive in config."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "note.md").write_text("# Note\n\nRetrieval is finding material.\n")

    connected = runner.invoke(
        app, ["connect", str(corpus), "--kind", "files", "--as", "c"]
    )
    assert connected.exit_code == 0, connected.stdout
    assert config_module.load().find("c") is not None

    found = runner.invoke(app, ["search", "retrieval"])
    assert found.exit_code == 0, found.stdout
    assert "c:note.md" in found.stdout


def test_connect_from_an_mcp_config_records_the_command_and_its_options(
    tmp_path: Path,
) -> None:
    """The JSON file is read once, at connect: what is recorded is an ordinary source
    row, so the config stays self-contained and nothing re-reads another host's file.
    Options had to survive `record()` for that — before this they were dropped."""
    probe = Path(__file__).parent / "fixtures" / "mcp_probe_server.py"
    config = tmp_path / ".mcp.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "records": {
                        "command": sys.executable,
                        "args": [str(probe)],
                        "env": {"PROBE_LITERAL": "fixed"},
                        "cwd": ".",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["connect", "--from", str(config)])
    assert result.exit_code == 0, result.stdout

    recorded = config_module.load().find("records")
    assert recorded is not None
    assert recorded.kind == "mcp"
    assert recorded.target == shlex.join([sys.executable, str(probe)])
    assert recorded.options == {
        "env": ["PROBE_LITERAL=fixed"],
        "cwd": str(tmp_path.resolve()),
    }


def test_connect_with_neither_a_source_nor_a_file_says_so() -> None:
    result = runner.invoke(app, ["connect"])
    assert result.exit_code == 1
    assert "--from" in result.stderr


def test_search_json_emits_parseable_events(tmp_path: Path) -> None:
    """`--json` is the scripting path and the serialization proof at once."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "note.md").write_text("# Note\n\nRetrieval is finding material.\n")
    runner.invoke(app, ["connect", str(corpus), "--kind", "files", "--as", "c"])

    result = runner.invoke(app, ["search", "retrieval", "--json"])
    assert result.exit_code == 0
    lines = [ln for ln in result.stdout.splitlines() if ln.startswith("{")]
    assert lines
    for line in lines:
        payload = json.loads(line)
        assert payload["schema_version"] == ev.SCHEMA_VERSION
        assert not isinstance(ev.parse_event(payload), ev.UnknownEvent)


def test_search_has_no_kind_option() -> None:
    """Every hit is a `file` or a `resource`, so `--kind` had nothing to filter;
    `--source` is what separates sources."""
    result = runner.invoke(app, ["search", "x", "--kind", "concept"])
    assert result.exit_code == 2
    assert "No such option" in result.stderr


def test_ask_exits_nonzero_and_says_what_to_do() -> None:
    result = runner.invoke(app, ["ask", "why"])
    assert result.exit_code == 1
    assert "no agent runtime configured" in result.stdout
    assert "intel doctor" in result.stdout


def test_disconnecting_something_unattached_fails_cleanly() -> None:
    result = runner.invoke(app, ["disconnect", "ghost"])
    assert result.exit_code == 1


def test_connecting_an_unknown_registry_id_lists_what_is_known(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stores = tmp_path / "stores.yaml"
    stores.write_text("stores:\n  - {id: alpha, kind: wiki, paths: {root: /tmp/a}}\n")
    monkeypatch.setenv("KNOWLEDGE_REGISTRY", str(stores))
    result = runner.invoke(app, ["connect", "beta"])
    assert result.exit_code == 1
    # Errors go to stderr so `--json` on stdout stays machine-readable. Click 8.4
    # separates the two streams, which is what makes that guarantee testable.
    assert "alpha" in result.stderr


def test_attach_failures_are_escaped_and_shown_first() -> None:
    """Two bugs in one line, both found in real use.

    Rich reads `[wiki,s3]` as a style tag, so the advice `uv tool install
    'latent-intel[wiki,s3]'` rendered as `uv tool install 'latent-intel'` — the message
    ate the very thing it was telling you to type. And the report ran *after* the
    listing, so `intel stores | head` hid it entirely.
    """
    from latent_intel.frontends._shared import report

    console_err = Console(theme=THEME, width=200, record=True, stderr=True)
    import latent_intel.frontends._shared as shared

    original, shared.err_console = shared.err_console, console_err
    try:
        report(["design: install 'latent-intel[wiki,s3]' --reinstall"])
    finally:
        shared.err_console = original

    assert "latent-intel[wiki,s3]" in console_err.export_text()


def test_doctor_reports_the_env_by_name_and_never_by_value(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one place a credential's presence is printed must stay safe to paste into a
    chat. And "is my .env picked up?" needs a better answer than a failed connect."""
    from latent_intel import env as env_module

    secret = "sentinel-never-printed-9f3a"
    home = config_module.home()
    home.mkdir(parents=True, exist_ok=True)
    (home / ".env").write_text(f"AWS_SECRET_ACCESS_KEY={secret}\n")
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    # `.env` is applied by the console-script entry (`main`), which `CliRunner`
    # bypasses by invoking the app directly — so apply it the way `main` does.
    env_module.load(None, home)

    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "environment" in result.stdout
    # The console wraps long paths, so assert the fact rather than the full string.
    assert "✓ .env" in result.stdout
    assert "no .env" not in result.stdout
    assert "AWS_SECRET_ACCESS_KEY" in result.stdout
    assert secret not in result.stdout


# -- record and replay ------------------------------------------------------


def printed(text: str) -> list[str]:
    """Rendered output as comparable lines — trailing spaces are not the subject."""
    return [line.rstrip() for line in text.splitlines()]


def test_a_shared_name_replays_with_the_labels_the_session_placed() -> None:
    result = runner.invoke(app, ["replay", str(STREAMS / "shared_prefix.jsonl")])
    assert result.exit_code == 0
    assert "sources: wiki:reed-bed records:survey-2019::chunk::0004" in flat(
        result.stdout
    )


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_replay_renders_exactly_what_the_live_renderer_renders(path: Path) -> None:
    """A replay is the run as it looked, not a summary of it. `replay` therefore calls
    the same renderer the live path does — checked against every shipped stream, so a
    second renderer cannot appear here without this failing.

    One event diverges on purpose. The live renderer drops `UserMessage`, because the
    frontend that caused it has already echoed it; on replay nothing has, so `replay`
    prints it as the prompt line. That one line is reproduced here rather than waved
    through, so a second divergence cannot arrive unnoticed.
    """
    result = runner.invoke(app, ["replay", str(path)])
    assert result.exit_code == 0

    console = capture(width=80)
    for event in ev.read_stream(path.read_text()):
        if isinstance(event, ev.UserMessage):
            console.print(f"[prompt]›[/] {event.text}")
        else:
            render.render(console, event)

    assert printed(result.stdout) == printed(console.export_text())


def test_a_replayed_ask_shows_the_question_that_was_asked() -> None:
    """An answer without its question is half a record. Nothing echoed the prompt on
    this side, so `replay` is the one place that prints `UserMessage`."""
    result = runner.invoke(app, ["replay", str(STREAMS / "agent_turn.jsonl")])
    assert result.exit_code == 0
    assert "which sources disagree about context compaction?" in result.stdout


def test_a_type_this_build_never_heard_of_replays_as_a_line(tmp_path: Path) -> None:
    """Invariant 3, end to end rather than at the parser: a run recorded by a newer
    build replays on an older one instead of refusing it."""
    recording = tmp_path / "from-the-future.jsonl"
    recording.write_text(
        json.dumps(
            {
                "type": "invented_later",
                "schema_version": 99,
                "session_id": "00000000-0000-4000-8000-000000000000",
                "operation_id": "00000000-0000-4000-8000-000000000001",
                "sequence": 1,
                "shape": {"nobody": "knows"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["replay", str(recording)])
    assert result.exit_code == 0
    assert "invented_later" in result.stdout

    # And a type nothing can parse into an event still comes back out unchanged: the
    # re-emitted line is the recorded line, not a re-serialisation of what was salvaged.
    piped = runner.invoke(app, ["replay", str(recording), "--json"])
    assert piped.stdout.splitlines() == recording.read_text().splitlines()


def test_record_tees_the_run_and_replays_identically(tmp_path: Path) -> None:
    """The whole feature in one test: `--record` does not replace the live view, what
    it wrote is events this build understands, and reading it back gives the run."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "note.md").write_text("# Note\n\nRetrieval is finding material.\n")
    runner.invoke(app, ["connect", str(corpus), "--kind", "files", "--as", "c"])

    recording = tmp_path / "run.jsonl"
    live = runner.invoke(app, ["search", "retrieval", "--record", str(recording)])
    assert live.exit_code == 0
    assert "c:note.md" in live.stdout  # teed, not diverted

    lines = recording.read_text().splitlines()
    assert lines
    for line in lines:
        # The acceptance criterion a web client will depend on, asserted against a
        # recording rather than a fixture: what was written is what this build reads.
        assert not isinstance(ev.parse_event(line), ev.UnknownEvent)

    replayed = runner.invoke(app, ["replay", str(recording)])
    assert replayed.exit_code == 0
    assert printed(replayed.stdout) == printed(live.stdout)


def test_a_run_cut_off_partway_replays_up_to_the_cut(tmp_path: Path) -> None:
    """An interrupted run is the common case, which is why the file is appended and
    flushed per event. The half-written last line renders as the unknown line rather
    than losing everything before it."""
    whole = (STREAMS / "search.jsonl").read_text()
    cut = tmp_path / "interrupted.jsonl"
    cut.write_text(whole[: whole.index("\n", whole.index("\n") + 1) + 60])

    result = runner.invoke(app, ["replay", str(cut)])
    assert result.exit_code == 0
    assert "searching" in result.stdout  # the complete events are all there
    assert "unparseable" in result.stdout  # and the truncation says so


def test_since_starts_at_that_line_of_the_file() -> None:
    """`--since` is a line number, not a `sequence`: sequence is monotonic within an
    operation and restarts, so two runs appended to one file give it several line 3s.
    A line number is what the person reading the file already has in the gutter."""
    path = STREAMS / "search.jsonl"
    lines = path.read_text().splitlines()

    whole = runner.invoke(app, ["replay", str(path), "--json", "--since", "1"])
    tail = runner.invoke(app, ["replay", str(path), "--json", "--since", "3"])

    assert whole.exit_code == 0 and tail.exit_code == 0
    assert whole.stdout.splitlines() == lines
    assert tail.stdout.splitlines() == lines[2:]  # inclusive of line 3


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_replay_json_re_emits_the_recorded_lines_byte_for_byte(path: Path) -> None:
    """`--json` is what makes a recording pipeable onward, so what comes out has to be
    what went in. Re-dumping the parsed event instead would reorder fields and rewrite
    timestamps, and `replay --json` would disagree with the run that recorded it."""
    result = runner.invoke(app, ["replay", str(path), "--json"])
    assert result.exit_code == 0
    assert result.stdout.splitlines() == [
        line for line in path.read_text().splitlines() if line.strip()
    ]


def test_a_stream_from_a_newer_schema_is_named_once_and_still_rendered(
    tmp_path: Path,
) -> None:
    """Named rather than refused: the run still reads, and a version this build does
    not know about is a caveat on stderr rather than a silent misreading."""
    line = json.loads((STREAMS / "search.jsonl").read_text().splitlines()[1])
    line["schema_version"] = ev.SCHEMA_VERSION + 1
    path = tmp_path / "from-a-newer-build.jsonl"
    path.write_text(json.dumps(line) + "\n" + json.dumps(line) + "\n")

    result = runner.invoke(app, ["replay", str(path)])
    assert result.exit_code == 0
    assert "searching" in result.stdout  # still rendered
    assert result.stderr.count("schema_version") == 1  # two lines, one warning
    assert str(ev.SCHEMA_VERSION + 1) in result.stderr


def test_replaying_a_file_that_is_not_there_says_so_and_exits_nonzero(
    tmp_path: Path,
) -> None:
    result = runner.invoke(app, ["replay", str(tmp_path / "never-written.jsonl")])
    assert result.exit_code == 1
    assert "never-written.jsonl" in result.stderr


def test_a_record_target_that_cannot_be_opened_is_a_line_not_a_traceback(
    tmp_path: Path,
) -> None:
    """The file is opened before any source is attached, so a mistyped path fails
    before the work rather than after it — and as one line, not a stack trace."""
    result = runner.invoke(app, ["search", "anything", "--record", str(tmp_path)])
    assert result.exit_code != 0
    assert "✗" in result.stderr
    assert "Traceback" not in result.stderr
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_record_on_get_writes_the_document_it_fetched(tmp_path: Path) -> None:
    """`--record` is on every command that streams, not on `search` alone."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "note.md").write_text("# Note\n\nRetrieval is finding material.\n")
    runner.invoke(app, ["connect", str(corpus), "--kind", "files", "--as", "c"])

    recording = tmp_path / "fetch.jsonl"
    result = runner.invoke(app, ["get", "c:note.md", "--record", str(recording)])
    assert result.exit_code == 0
    assert "document_fetched" in recording.read_text()


def test_a_recording_opens_with_what_produced_it(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "note.md").write_text("# Note\n\nRetrieval is finding material.\n")
    runner.invoke(app, ["connect", str(corpus), "--kind", "files", "--as", "c"])

    recording = tmp_path / "run.jsonl"
    result = runner.invoke(
        app,
        ["search", "retrieval", "--record", str(recording)]
        + ["--purpose", "demo", "--title", "first look"],
    )
    assert result.exit_code == 0

    first = ev.parse_event(recording.read_text().splitlines()[0])
    assert isinstance(first, ev.RunContext)
    assert (first.purpose, first.title) == ("demo", "first look")
    assert [s.id for s in first.sources] == ["c"]
    replayed = runner.invoke(app, ["replay", str(recording)])
    assert '● demo "first look"' in replayed.stdout


def test_the_json_stream_has_no_header(tmp_path: Path) -> None:
    """`--json` is the wire format; a header belongs to the recording file only."""
    recording = tmp_path / "run.jsonl"
    result = runner.invoke(app, ["search", "x", "--json", "--record", str(recording)])
    assert "run_context" not in result.stdout
    assert "run_context" in recording.read_text().splitlines()[0]


@pytest.mark.parametrize(
    "extra",
    [["--purpose", "demo"], ["--title", "t"]],
)
def test_recording_options_without_a_recording_are_refused(extra: list[str]) -> None:
    result = runner.invoke(app, ["search", "x", *extra])
    assert result.exit_code == 1
    assert "--record" in result.stderr


def test_an_unknown_purpose_is_refused_before_anything_runs(tmp_path: Path) -> None:
    recording = tmp_path / "run.jsonl"
    result = runner.invoke(
        app, ["search", "x", "--record", str(recording), "--purpose", "archive"]
    )
    assert result.exit_code == 1
    assert not recording.exists()


def test_a_file_with_no_header_replays_and_says_so() -> None:
    result = runner.invoke(app, ["replay", str(STREAMS / "search.jsonl")])
    assert result.exit_code == 0
    assert "searching" in result.stdout
    assert "no run context" in result.stderr


def test_a_header_from_another_format_is_named_once(tmp_path: Path) -> None:
    """Its version means nothing against ours, so no schema warning follows."""
    header = json.loads((ROOT_FORMATS / "search.jsonl").read_text().splitlines()[0])
    header.update(format="other.tool", format_version=3)
    path = tmp_path / "other.jsonl"
    path.write_text(json.dumps(header) + "\n")
    result = runner.invoke(app, ["replay", str(path)])
    assert "other.tool" in result.stderr
    assert "schema_version" not in result.stderr


def test_a_header_appended_after_a_json_capture_still_warns(tmp_path: Path) -> None:
    path = tmp_path / "mixed.jsonl"
    path.write_text(
        (STREAMS / "search.jsonl").read_text()
        + (ROOT_FORMATS / "search.jsonl").read_text()
    )
    result = runner.invoke(app, ["replay", str(path)])
    assert result.exit_code == 0
    assert "no run context on the first line" in result.stderr


def test_a_header_cannot_restyle_the_terminal(tmp_path: Path) -> None:
    header = json.loads((ROOT_FORMATS / "search.jsonl").read_text().splitlines()[0])
    header["context"]["hash"] = "[bold]xx" + "0" * 56
    path = tmp_path / "styled.jsonl"
    path.write_text(json.dumps(header) + "\n")
    result = runner.invoke(app, ["replay", str(path)])
    assert "[bold]xx" in result.stdout


def test_an_unwritable_data_directory_is_one_line_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from latent_intel import config as config_module

    blocked = tmp_path / "blocked"
    blocked.write_text("a file where a directory should be")
    monkeypatch.setenv(config_module.ENV_HOME, str(blocked))
    result = runner.invoke(app, ["search", "x", "--record", "run"])
    assert result.exit_code == 1
    assert "✗" in result.stderr
    assert not isinstance(result.exception, OSError)


# -- the web scope ------------------------------------------------------------


def test_connect_refuses_the_web_s_id(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    result = runner.invoke(
        app, ["connect", str(corpus), "--kind", "files", "--as", "web"]
    )
    assert result.exit_code == 1
    assert "reserved for web" in " ".join(result.output.split())


def test_ask_refuses_a_web_scope_the_runtime_cannot_honour_before_asking() -> None:
    config = config_module.load()
    config.runtime = "openai-agents"
    config_module.save(config)
    result = runner.invoke(app, ["ask", "anything new?", "--web", "search"])
    assert result.exit_code == 1
    assert "honours only `web: off`" in " ".join(result.output.split())

    result = runner.invoke(app, ["ask", "anything new?", "--web", "everything"])
    assert result.exit_code == 1
    assert "no mode 'everything'" in " ".join(result.output.split())


def test_doctor_lists_the_modes_each_runtime_honours_and_warns_on_open_browse(
    tmp_path: Path,
) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "note.md").write_text("# Note\n")
    runner.invoke(app, ["connect", str(corpus), "--kind", "files", "--as", "c"])
    config = config_module.load()
    config.web = "browse"
    config_module.save(config)

    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    rows = {line.split()[0]: line for line in lines}
    assert "web browse (user)" in result.stdout
    # Declares nothing; and `custom` with no host set has no row to serve the web.
    assert rows["openai-agents"].split()[1:] == ["off"]
    assert rows["custom"].split()[1:] == ["off"]
    for name in ("anthropic-sdk", "claude-agent-sdk", "claude-cli"):
        assert rows[name].split()[1:6] == ["off", "·", "search", "·", "browse"]
    printed = " ".join(result.stdout.split())
    assert "web browse is on with sources attached and no allowed_domains" in printed

    config.web = {"mode": "browse", "allowed_domains": ["arxiv.org"]}
    config_module.save(config)
    result = runner.invoke(app, ["doctor"])
    assert "no allowed_domains" not in " ".join(result.stdout.split())


def test_any_drops_a_configured_cap_the_claude_code_runtimes_cannot_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mode alone keeps `max_uses`, which `claude-cli` refuses; `--any` is the way to
    use it without editing the config. The header names the scope the turn ran under."""
    from latent_intel.session import Session

    async def answered(self: Session, prompt: str, **_: Any) -> Any:
        yield ev.Emitter(self.session_id).emit(
            ev.AgentCompleted, text="ok", streamed=False
        )

    monkeypatch.setattr(Session, "ask", answered)
    config = config_module.load()
    config.runtime = "claude-cli"
    config.web = {"mode": "search", "max_uses": 5}
    config_module.save(config)

    refused = runner.invoke(app, ["ask", "anything?", "--web", "search"])
    assert refused.exit_code == 1
    assert "/scope search --any" in " ".join(refused.output.split())

    path = tmp_path / "run.jsonl"
    result = runner.invoke(
        app, ["ask", "anything?", "--web", "search", "--any", "--record", str(path)]
    )
    assert result.exit_code == 0, result.output
    header = ev.parse_event(path.read_text().splitlines()[0])
    assert header.web == WebScope(mode="search")  # type: ignore[union-attr]

    alone = runner.invoke(app, ["ask", "anything?", "--any"])
    assert alone.exit_code == 1
    assert "--web" in alone.output


def test_doctor_warns_when_the_configured_runtime_would_refuse_the_scope() -> None:
    config = config_module.load()
    config.runtime = "claude-cli"
    config.web = {"mode": "search", "max_uses": 5}
    config_module.save(config)
    result = runner.invoke(app, ["doctor"])
    printed = " ".join(result.stdout.split())
    assert "claude-cli would refuse this scope" in printed
    assert "max_uses" in printed


# -- intel deployment ---------------------------------------------------------


def _deployment(tmp_path: Path, name: str, body: str = "") -> Path:
    """A deployment file in the search directory the suite points every test at."""
    directory = tmp_path / "projects"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.yaml"
    path.write_text(f"name: {name}\ntitle: {name.title()}\n{body}", encoding="utf-8")
    return path


def test_the_command_is_deployment_and_project_is_gone() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "deployment" in result.stdout
    assert "project" not in result.stdout
    assert runner.invoke(app, ["project", "list"]).exit_code != 0


def test_deployment_list_names_what_it_finds(tmp_path: Path) -> None:
    _deployment(tmp_path, "research")
    result = runner.invoke(app, ["deployment", "list"])
    assert result.exit_code == 0
    assert "research" in result.stdout and "latent" in result.stdout


def test_deployment_use_records_the_choice(tmp_path: Path) -> None:
    _deployment(tmp_path, "research")
    result = runner.invoke(app, ["deployment", "use", "research"])
    assert result.exit_code == 0, result.output
    assert config_module.load().deployment == "research"

    result = runner.invoke(app, ["deployment", "use", "none"])
    assert result.exit_code == 0
    assert config_module.load().deployment is None


def test_deployment_use_refuses_a_name_it_cannot_find() -> None:
    result = runner.invoke(app, ["deployment", "use", "nowhere"])
    assert result.exit_code == 1
    assert "no deployment 'nowhere'" in result.output


def test_deployment_show_names_the_root(tmp_path: Path) -> None:
    _deployment(tmp_path, "research", "root: ./products\n")
    runner.invoke(app, ["deployment", "use", "research"])
    result = runner.invoke(app, ["deployment", "show"])
    assert result.exit_code == 0, result.output
    assert "Research" in result.stdout
    flat = "".join(result.stdout.split())
    assert "".join(str((tmp_path / "projects" / "products").resolve()).split()) in flat


def test_deployment_add_searches_a_directory(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "field.yaml").write_text("name: field\n", encoding="utf-8")
    result = runner.invoke(app, ["deployment", "add", str(elsewhere)])
    assert result.exit_code == 0, result.output
    assert str(elsewhere.resolve()) in config_module.load().deployment_paths
    assert "field" in result.stdout


def test_deployment_validate_passes_a_good_file_and_fails_a_bad_one(
    tmp_path: Path,
) -> None:
    _deployment(tmp_path, "good")
    _deployment(tmp_path, "bad", "serv: {}\n")
    assert runner.invoke(app, ["deployment", "validate", "good"]).exit_code == 0
    result = runner.invoke(app, ["deployment", "validate", "bad"])
    assert result.exit_code == 1
    assert "serv" in result.output


def test_deployment_validate_with_nothing_active_says_so() -> None:
    result = runner.invoke(app, ["deployment", "validate"])
    assert result.exit_code == 1
    assert "no deployment to validate" in result.output


def test_a_search_that_reaches_no_source_fails_on_stdout_and_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every source failed to attach, so nothing was searched. Exit 0 and an empty
    result would read as "no hits" to a script; the failure goes on stdout too, where
    the results would have been."""
    _deployment(
        tmp_path,
        "field",
        "sources:\n  - {id: notes, kind: files, target: ./not-here}\n",
    )
    monkeypatch.setenv("LATENT_INTEL_PROJECT", "field")

    result = runner.invoke(app, ["search", "x"])
    assert result.exit_code == 1
    assert "✗" in result.stdout and "nothing to search" in result.stdout
    assert "notes" in result.stderr  # why, said once by the attach report

    as_json = runner.invoke(app, ["search", "x", "--json"])
    assert as_json.exit_code == 1
    types = [json.loads(line)["type"] for line in as_json.stdout.splitlines()]
    assert types == ["agent_failed"]


def test_validate_reports_targets_that_are_not_there_and_starts_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Checked without attaching: a local `files` or `code` directory must exist and an
    `mcp` command must be on PATH. A command that is there is never run."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "started"
    server = bin_dir / "example-mcp"
    server.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\n")
    server.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    (tmp_path / "projects" / "here").mkdir(parents=True)
    _deployment(
        tmp_path,
        "field",
        "sources:\n"
        "  - {id: notes, kind: files, target: ./not-here}\n"
        "  - {id: repo, kind: code, target: ./also-not-here}\n"
        "  - {id: fine, kind: files, target: ./here}\n"
        "  - {id: tools, kind: mcp, target: 'no-such-server-xyz serve'}\n"
        "  - {id: served, kind: mcp, target: 'example-mcp --flag'}\n"
        "  - {id: hosted, kind: mcp, target: 'https://mcp.example.org/mcp'}\n"
        "  - {id: remote, kind: files, target: 's3://example-bucket/notes'}\n",
    )

    result = runner.invoke(app, ["deployment", "validate", "field"])
    assert result.exit_code == 1
    err = flat(result.stderr)
    assert "not-here" in err and "also-not-here" in err
    assert "no-such-server-xyz" in err and "PATH" in err
    for clean in ("'fine'", "'served'", "'hosted'", "'remote'"):
        assert clean not in err
    assert not marker.exists()


def test_deployment_add_finds_a_deployment_one_folder_down(tmp_path: Path) -> None:
    """The layout `deployment new` writes is `<name>/<name>.yaml`, so adding the folder
    that holds several finds each of them."""
    parent = tmp_path / "deployments"
    for name in ("field", "lab"):
        (parent / name).mkdir(parents=True)
        (parent / name / f"{name}.yaml").write_text(f"name: {name}\n")
    result = runner.invoke(app, ["deployment", "add", str(parent)])
    assert result.exit_code == 0, result.output
    assert "field" in result.stdout and "lab" in result.stdout
    assert runner.invoke(app, ["deployment", "use", "lab"]).exit_code == 0


def test_deployment_add_lists_only_what_it_finds_first_and_names_a_shadow(
    tmp_path: Path,
) -> None:
    """A name an earlier search path already holds is not this directory's to offer."""
    _deployment(tmp_path, "lab")
    parent = tmp_path / "deployments"
    for name in ("lab", "field"):
        (parent / name).mkdir(parents=True)
        (parent / name / f"{name}.yaml").write_text(f"name: {name}\n")
    result = runner.invoke(app, ["deployment", "add", str(parent)])
    assert result.exit_code == 0, result.output
    assert "(field)" in flat(result.stdout)
    assert "lab" in result.stderr and "shadowed" in result.stderr


def test_one_file_reached_from_two_search_paths_shadows_nothing(
    tmp_path: Path,
) -> None:
    """`projects/lab/lab.yaml` is found one folder down from `projects` and again from
    `projects/lab` once that is added: one file, so no shadow to warn about."""
    (tmp_path / "projects" / "lab").mkdir(parents=True)
    (tmp_path / "projects" / "lab" / "lab.yaml").write_text("name: lab\n")
    result = runner.invoke(app, ["deployment", "add", str(tmp_path / "projects/lab")])
    assert result.exit_code == 0, result.output
    assert "(lab)" in flat(result.stdout)
    assert "shadowed" not in result.stderr
    assert "shadowed" not in runner.invoke(app, ["deployment", "list"]).stderr


def test_connect_warns_when_the_deployment_it_records_into_is_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    monkeypatch.setenv("LATENT_INTEL_PROJECT", "nowhere")
    result = runner.invoke(app, ["connect", str(corpus), "--kind", "files"])
    assert "'nowhere' not found" in flat(result.stderr)


def test_deployment_add_says_when_it_finds_nothing_and_records_nothing(
    tmp_path: Path,
) -> None:
    empty = tmp_path / "empty"
    (empty / "deeper" / "still").mkdir(parents=True)
    (empty / "deeper" / "still" / "still.yaml").write_text("name: still\n")
    result = runner.invoke(app, ["deployment", "add", str(empty)])
    assert result.exit_code == 1
    assert "✓" not in result.output
    assert "no deployment" in flat(result.stderr)
    assert str(empty.resolve()) not in config_module.load().deployment_paths


def _renamed(tmp_path: Path) -> Path:
    """A deployment chosen as `team` whose `name:` says `research`."""
    path = _deployment(tmp_path, "research")
    renamed = path.with_name("team.yaml")
    path.rename(renamed)
    assert runner.invoke(app, ["deployment", "use", "team"]).exit_code == 0
    return renamed


def test_a_source_connected_to_a_renamed_deployment_attaches_and_disconnects(
    tmp_path: Path,
) -> None:
    """The overlay is kept under the name a deployment is chosen by, not its `name:`."""
    _renamed(tmp_path)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "note.md").write_text("# Note\n\nRetrieval is finding material.\n")

    connected = runner.invoke(
        app, ["connect", str(corpus), "--kind", "files", "--as", "c"]
    )
    assert connected.exit_code == 0, connected.output
    assert config_module.load().find("c", "team") is not None
    found = runner.invoke(app, ["search", "retrieval"])
    assert "c:note.md" in found.stdout

    gone = runner.invoke(app, ["disconnect", "c"])
    assert gone.exit_code == 0, gone.output
    assert config_module.load().find("c", "team") is None


def test_list_and_validate_know_a_renamed_deployment_is_active(
    tmp_path: Path,
) -> None:
    _renamed(tmp_path)
    listed = runner.invoke(app, ["deployment", "list"])
    assert any("✓" in line and "team" in line for line in listed.stdout.splitlines())
    validated = runner.invoke(app, ["deployment", "validate"])
    assert validated.exit_code == 0, validated.output


def test_validate_with_no_argument_reports_a_broken_active_deployment(
    tmp_path: Path,
) -> None:
    _renamed(tmp_path).write_text("name: [unclosed\n", encoding="utf-8")
    result = runner.invoke(app, ["deployment", "validate"])
    assert result.exit_code == 1
    assert "no deployment to validate" not in result.output
    assert "team.yaml" in "".join(result.output.split())


def test_a_deployment_chosen_by_the_environment_keys_the_overlay_by_that_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from latent_intel.frontends import _shared

    _deployment(tmp_path, "research").rename(tmp_path / "projects" / "team.yaml")
    monkeypatch.setenv("LATENT_INTEL_PROJECT", "team")
    cfg = config_module.load()
    cfg.add(
        config_module.SourceSpec(id="c", kind="files", target=str(tmp_path)), "team"
    )
    config_module.save(cfg)
    assert _shared.forget("c")
    assert config_module.load().find("c", "team") is None


def test_deployment_new_writes_one_that_add_and_use_take(tmp_path: Path) -> None:
    where = tmp_path / "work"
    made = runner.invoke(app, ["deployment", "new", "field", str(where)])
    assert made.exit_code == 0, made.output
    assert (where / "field" / "field.yaml").is_file()
    assert "intel deployment add" in made.stdout

    assert (
        runner.invoke(app, ["deployment", "add", str(where / "field")]).exit_code == 0
    )
    assert runner.invoke(app, ["deployment", "use", "field"]).exit_code == 0
    assert runner.invoke(app, ["deployment", "validate"]).exit_code == 0


def test_deployment_new_refuses_a_name_already_found(tmp_path: Path) -> None:
    _deployment(tmp_path, "research")
    result = runner.invoke(app, ["deployment", "new", "research", str(tmp_path / "w")])
    assert result.exit_code == 1
    assert "already" in result.output
    assert not (tmp_path / "w").exists()


def test_deployment_new_refuses_a_bad_name_and_writes_nothing(tmp_path: Path) -> None:
    result = runner.invoke(app, ["deployment", "new", "Bad Name", str(tmp_path)])
    assert result.exit_code == 1
    assert not (tmp_path / "Bad Name").exists()


def test_deployment_new_into_a_file_says_so_without_a_traceback(
    tmp_path: Path,
) -> None:
    blocker = tmp_path / "a-file"
    blocker.write_text("", encoding="utf-8")
    result = runner.invoke(app, ["deployment", "new", "field", str(blocker)])
    assert result.exit_code == 1
    assert "✗" in result.output
    assert isinstance(result.exception, SystemExit)


def test_there_is_no_deployment_migrate() -> None:
    result = runner.invoke(app, ["deployment", "migrate"])
    assert result.exit_code != 0
    assert "No such command" in result.output


# -- intel eval run -----------------------------------------------------------

from tests.test_batch import setup  # noqa: E402,F401 — the fixture, by name


def _eval(setup: dict[str, Any], *extra: str) -> Any:  # noqa: F811
    work = setup["work"]
    return runner.invoke(
        app,
        ["eval", "run", str(work / "cases.yaml"), str(work / "variants.yaml"), *extra],
    )


def test_eval_dry_run_prints_the_matrix_and_spends_nothing(
    setup: dict[str, Any],  # noqa: F811
) -> None:
    from tests.test_batch import FakeRuntime

    result = _eval(setup, "--repeat", "2", "--dry-run")
    assert result.exit_code == 0, result.output
    assert "2 cases × 2 variants × 2 = 8 runs" in result.output
    assert "small" in result.output and "paid" in result.output
    assert FakeRuntime.turns == []
    assert not setup["root"].exists()


def test_eval_says_when_the_case_file_is_unstamped(
    setup: dict[str, Any],  # noqa: F811
) -> None:
    (setup["work"] / "cases.yaml").write_text(
        "cases:\n  - {id: q1, input: {question: hi}}\n", encoding="utf-8"
    )
    result = _eval(setup, "--dry-run")
    assert result.exit_code == 0, result.output
    assert "unstamped" in result.output
    assert "unstamped" not in _eval_stamped(setup).output


def _eval_stamped(setup: dict[str, Any]) -> Any:  # noqa: F811
    from tests.test_batch import CASES

    (setup["work"] / "cases.yaml").write_text(CASES, encoding="utf-8")
    return _eval(setup, "--dry-run")


def test_eval_shows_a_variant_that_cannot_run_and_refuses_to_run_it(
    setup: dict[str, Any],  # noqa: F811
) -> None:
    from tests.test_batch import _variants

    _variants(
        setup["work"],
        "variants:\n  - {id: small, runtime: fake, options: {unavailable: true}}\n",
    )
    dry = _eval(setup, "--dry-run")
    assert dry.exit_code == 0, dry.output
    assert "FAKE_KEY" in dry.output
    run = _eval(setup, "--yes")
    assert run.exit_code == 1
    assert "cannot run here" in run.output
    assert not setup["root"].exists()


def test_eval_refuses_paid_variants_with_no_one_to_ask(
    setup: dict[str, Any],  # noqa: F811
) -> None:
    result = _eval(setup)
    assert result.exit_code == 1
    assert "--yes" in result.output
    assert not setup["root"].exists()


def test_eval_with_yes_runs_and_names_the_manifest(
    setup: dict[str, Any],  # noqa: F811
) -> None:
    result = _eval(setup, "--yes")
    assert result.exit_code == 0, result.output
    assert "4 of 4 ok" in result.output
    [run] = list((setup["root"] / "runs").iterdir())
    assert (run / "manifest.json").exists()


def test_eval_from_the_built_in_deployment_refuses_and_writes_nothing(
    setup: dict[str, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from latent_intel import batch

    monkeypatch.setenv("LATENT_INTEL_PROJECT", "latent")
    result = _eval(setup, "--yes")
    assert result.exit_code == 1
    assert "intel deployment use" in result.output
    assert not (batch.PACKAGE / "data" / "projects" / "runs").exists()


# -- relative connect paths -----------------------------------------------


def _notes(directory: Path) -> Path:
    directory.mkdir(parents=True)
    (directory / "note.md").write_text("# Note\n\nRetrieval is finding material.\n")
    return directory


def test_a_relative_connect_path_is_saved_absolute_with_home_as_tilde(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recorded as typed, `./notes` meant a different directory to every later
    command. It is anchored where `connect` ran, and the home folder stays `~`, so the
    config names no user."""
    user = tmp_path / "user"
    _notes(user / "work" / "notes")
    monkeypatch.setenv("HOME", str(user))
    monkeypatch.chdir(user / "work")

    result = runner.invoke(app, ["connect", "./notes", "--kind", "files", "--as", "n"])
    assert result.exit_code == 0, result.output
    recorded = config_module.load().find("n")
    assert recorded is not None and recorded.target == "~/work/notes"

    monkeypatch.chdir(tmp_path)
    found = runner.invoke(app, ["search", "retrieval"])
    assert "n:note.md" in found.stdout, found.output


def test_a_relative_connect_path_outside_home_is_saved_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    code = _notes(tmp_path / "elsewhere" / "repo")
    monkeypatch.chdir(code.parent)

    result = runner.invoke(app, ["connect", "repo", "--kind", "code"])
    assert result.exit_code == 0, result.output
    recorded = config_module.load().find("repo")
    assert recorded is not None and recorded.target == str(code)


def test_a_home_or_variable_path_is_saved_as_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sources are recorded as named: `~` and `${VAR}` re-resolve wherever the config
    is read, so neither is expanded on the way in."""
    user = tmp_path / "user"
    _notes(user / "notes")
    monkeypatch.setenv("HOME", str(user))
    monkeypatch.setenv("NOTES_ROOT", str(user))
    monkeypatch.chdir(tmp_path)

    for spec, name in (("~/notes", "home"), ("${NOTES_ROOT}/notes", "var")):
        result = runner.invoke(app, ["connect", spec, "--kind", "files", "--as", name])
        assert result.exit_code == 0, result.output
        recorded = config_module.load().find(name)
        assert recorded is not None and recorded.target == spec


def test_a_relative_path_under_a_symlinked_directory_keeps_the_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`getcwd()` follows links, and a Drive mount's real path carries an email
    address. The directory the shell reports, `PWD`, is the one the user typed."""
    user = tmp_path / "user"
    user.mkdir()
    real = tmp_path / "mount" / "Drive-someone@example.com"
    _notes(real / "notes")
    (user / "drive").symlink_to(real)
    monkeypatch.setenv("HOME", str(user))
    monkeypatch.chdir(user / "drive")
    monkeypatch.setenv("PWD", str(user / "drive"))

    result = runner.invoke(app, ["connect", "notes", "--kind", "files"])
    assert result.exit_code == 0, result.output
    recorded = config_module.load().find("notes")
    assert recorded is not None and recorded.target == "~/drive/notes"


def test_a_parent_path_under_a_symlinked_directory_means_what_ls_means(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`..` under a link is the link target's parent, as the connector resolves it, so
    it is kept rather than collapsed against `PWD`."""
    user = tmp_path / "user"
    user.mkdir()
    real = tmp_path / "mount" / "real"
    real.mkdir(parents=True)
    _notes(real.parent / "x")
    (user / "drive").symlink_to(real)
    monkeypatch.setenv("HOME", str(user))
    monkeypatch.chdir(user / "drive")
    monkeypatch.setenv("PWD", str(user / "drive"))

    result = runner.invoke(app, ["connect", "../x", "--kind", "files", "--as", "x"])
    assert result.exit_code == 0, result.output
    monkeypatch.chdir(tmp_path)
    found = runner.invoke(app, ["search", "retrieval"])
    assert "x:note.md" in found.stdout, found.output


def test_a_relative_path_holding_a_variable_is_anchored_with_it_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = tmp_path / "user"
    _notes(user / "data" / "notes")
    monkeypatch.setenv("HOME", str(user))
    monkeypatch.setenv("SUBDIR", "notes")
    monkeypatch.chdir(user)

    result = runner.invoke(
        app, ["connect", "data/${SUBDIR}", "--kind", "files", "--as", "n"]
    )
    assert result.exit_code == 0, result.output
    recorded = config_module.load().find("n")
    assert recorded is not None and recorded.target == "~/data/${SUBDIR}"

    monkeypatch.chdir(tmp_path)
    found = runner.invoke(app, ["search", "retrieval"])
    assert "n:note.md" in found.stdout, found.output


def test_an_unset_variable_is_not_reported_as_a_relative_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The entry is fine; the variable is missing in this shell."""
    monkeypatch.delenv("NOTES_ROOT", raising=False)
    config = config_module.load()
    config.add(
        config_module.SourceSpec(id="v", kind="files", target="${NOTES_ROOT}/notes")
    )
    config_module.save(config)

    result = runner.invoke(app, ["search", "retrieval"])
    assert "relative" not in result.stderr


def test_an_old_relative_entry_fails_with_a_message_to_reconnect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No migration: the directory it was typed in is not known, so guessing would
    attach the wrong one. It fails loudly, even where `./notes` happens to exist."""
    _notes(tmp_path / "notes")
    monkeypatch.chdir(tmp_path)
    config = config_module.load()
    config.add(config_module.SourceSpec(id="old", kind="files", target="./notes"))
    config_module.save(config)

    result = runner.invoke(app, ["search", "retrieval"])
    assert "old:note.md" not in result.stdout
    message = " ".join(result.stderr.split())
    assert "old: './notes' is a relative path" in message
    assert "connect it again with the full path" in message


def test_a_variable_set_to_a_relative_path_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _notes(tmp_path / "notes")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOTES", "notes")
    config = config_module.load()
    config.add(config_module.SourceSpec(id="v", kind="files", target="$NOTES"))
    config_module.save(config)

    result = runner.invoke(app, ["search", "retrieval"])
    assert "v:note.md" not in result.stdout
    assert "is a relative path" in " ".join(result.stderr.split())


def test_a_deployment_s_relative_target_still_resolves_against_its_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deployment's paths are anchored by the file that declares them, so the
    refusal of a relative user entry never reaches one."""
    _notes(tmp_path / "projects" / "corpus")
    _deployment(
        tmp_path,
        "research",
        "sources:\n  - {id: notes, kind: files, target: ./corpus}\n",
    )
    assert runner.invoke(app, ["deployment", "use", "research"]).exit_code == 0
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["search", "retrieval"])
    assert result.exit_code == 0, result.output
    assert "notes:note.md" in result.stdout
    assert "relative" not in result.stderr
