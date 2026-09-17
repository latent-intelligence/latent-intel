"""The CLI frontend, and the renderer it shares with the shell.

The renderer tests run against the recorded streams — no store, no network, no model.
That is what makes the frontend-independence claim checkable now rather than once the
web client exists.
"""

from __future__ import annotations

import json
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
from latent_intel.models import Capability, Descriptor
from latent_intel.ui import banner, render
from latent_intel.ui.theme import THEME

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
    `"anthropic" in stdout` is satisfied by `sdk-anthropic`, and `"! anthropic"` by
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


def test_doctor_names_the_variables_a_runtime_is_missing() -> None:
    """A bare "cannot run here" is not a diagnosis when four variables could each be
    the missing one. The names are safe to print; the values never appear."""
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert runtime_line(result.stdout, "custom") is not None
    assert runtime_line(result.stdout, "sdk-anthropic") is not None
    assert "ANTHROPIC_FOUNDRY_API_KEY" in result.stdout
    assert runtime_line(result.stdout, "openai-agents") is not None
    assert "OPENAI_API_KEY" in result.stdout


def test_doctor_groups_runtimes_by_who_owns_the_loop() -> None:
    """The flat list blended two different things: a runtime whose orchestration is
    ours to configure and one that hands orchestration, tools and permissions to
    another harness. A reader discovered that only by noticing which rows `intel hosts`
    had nothing to say about.

    `managed` is absent because nothing installed declares it — an empty group is not
    printed, since a machine with one runtime does not need a taxonomy."""
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    printed = [
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip() in FAMILY_HEADINGS
    ]
    assert printed == ["custom loop", "sdk runner", "delegated"]
    assert runtime_group(result.stdout, "custom") == "custom loop"
    assert runtime_group(result.stdout, "sdk-anthropic") == "sdk runner"
    assert runtime_group(result.stdout, "openai-agents") == "sdk runner"
    assert runtime_group(result.stdout, "claude-cli") == "delegated"


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
    assert table["sdk-anthropic"] == list(hosts.for_sdk("anthropic"))
    assert table["openai-agents"] == list(hosts.for_sdk("openai"))
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
    # sdk-anthropic resolves `foundry-anthropic` too, and it is nobody's choice here.
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
