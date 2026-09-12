"""The CLI frontend, and the renderer it shares with the shell.

The renderer tests run against the recorded streams — no store, no network, no model.
That is what makes the frontend-independence claim checkable now rather than once the
web client exists.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from latent_intel import config as config_module
from latent_intel import events as ev
from latent_intel.frontends.cli.main import app
from latent_intel.models import Capability, Descriptor
from latent_intel.ui import banner, render
from latent_intel.ui.theme import THEME

STREAMS = Path(__file__).parent / "fixtures" / "streams"
FIXTURES = sorted(STREAMS.glob("*.jsonl"))

runner = CliRunner()


def capture(width: int = 100) -> Console:
    return Console(theme=THEME, width=width, force_terminal=False, record=True)


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
    assert "anthropic" in result.stdout
    assert "ANTHROPIC_FOUNDRY_API_KEY" in result.stdout


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
