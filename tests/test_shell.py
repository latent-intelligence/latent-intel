"""Shell input parsing.

Split from the loop so it can be tested exhaustively without a terminal — which is the
only reason it is a separate module. Everything the shell decides about a line is here;
the loop only routes the result.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from latent_intel.commands import Ask, Connect, Disconnect, Fetch, Find, ListSources
from latent_intel.frontends.shell.parse import Invalid, Local, parse


def test_blank_input_does_nothing() -> None:
    assert parse("") is None
    assert parse("   ") is None


# -- the one grammar rule ---------------------------------------------------


def test_slash_is_session_management_and_bare_words_are_queries() -> None:
    """The whole grammar. Making the two look different means a typo in one can never
    be silently read as the other."""
    assert isinstance(parse("/sources"), ListSources)
    assert isinstance(parse("search context collapse"), Find)


def test_search_carries_the_query_not_the_verb() -> None:
    command = parse("search context collapse")
    assert isinstance(command, Find)
    assert command.query == "context collapse"


def test_open_and_get_are_the_same_thing() -> None:
    for line in ("open design:x", "get design:x"):
        command = parse(line)
        assert isinstance(command, Fetch)
        assert command.ref == "design:x"


def test_anything_else_is_a_question() -> None:
    command = parse("which sources disagree about compaction?")
    assert isinstance(command, Ask)
    assert command.prompt.startswith("which sources")


def test_a_verb_with_nothing_to_act_on_says_so() -> None:
    result = parse("search")
    assert isinstance(result, Invalid)
    assert "needs something" in result.message


# -- slash commands ---------------------------------------------------------


def test_connect_parses_its_flags() -> None:
    command = parse("/connect ~/notes --kind files --as n")
    assert isinstance(command, Connect)
    assert command.spec == "~/notes"
    assert command.kind == "files"
    assert command.source_id == "n"


def test_connect_with_a_dangling_flag_is_refused() -> None:
    result = parse("/connect x --kind")
    assert isinstance(result, Invalid)
    assert "needs a value" in result.message


def test_an_unknown_option_does_not_kill_the_session() -> None:
    """argparse would exit the process here, taking a session someone spent ten minutes
    assembling with it."""
    result = parse("/connect x --wat y")
    assert isinstance(result, Invalid)
    assert "--wat" in result.message


def test_quit_and_exit_are_the_same() -> None:
    for line in ("/quit", "/exit"):
        result = parse(line)
        assert isinstance(result, Local)
        assert result.action == "exit"


def test_disconnect_and_use_carry_their_argument() -> None:
    command = parse("/disconnect design")
    assert isinstance(command, Disconnect)
    assert command.source_id == "design"

    local = parse("/use design")
    assert isinstance(local, Local)
    assert local.action == "use" and local.argument == "design"


def test_use_with_no_argument_is_a_query_not_an_error() -> None:
    local = parse("/use")
    assert isinstance(local, Local)
    assert local.argument == ""


def test_wrong_arity_names_the_usage() -> None:
    result = parse("/sources extra")
    assert isinstance(result, Invalid)
    assert "/sources" in result.hint


def test_an_unknown_slash_command_suggests_a_close_one() -> None:
    result = parse("/sourc")
    assert isinstance(result, Invalid)
    assert "/sources" in result.hint


def test_unbalanced_quotes_do_not_raise() -> None:
    result = parse("/connect 'unclosed")
    assert isinstance(result, Invalid)


# -- the near-miss guard ----------------------------------------------------


def test_a_mistyped_verb_suggests_rather_than_redirects() -> None:
    """Silently rewriting someone's input is worse than asking. The hint says how to
    force the question through if the guess was wrong."""
    result = parse("searc context collapse")
    assert isinstance(result, Invalid)
    assert "search context collapse" in result.message
    assert result.hint.startswith("or to ask it as a question: ask searc")


def test_a_real_question_is_not_mistaken_for_a_typo() -> None:
    for line in ("why is retrieval slow", "what does the wiki say", "compare these"):
        assert isinstance(parse(line), Ask), line


@pytest.mark.parametrize("line", ["/help", "/tools", "/clear"])
def test_shell_side_commands_stay_local(line: str) -> None:
    result = parse(line)
    assert isinstance(result, Local)


def test_help_lists_every_command_without_being_written_twice() -> None:
    """Help is generated from the command table, so a command cannot be added to the
    grammar and forgotten in the documentation — which had already happened."""
    from rich.console import Console

    from latent_intel.frontends.shell.parse import SLASH
    from latent_intel.frontends.shell.repl import help_text
    from latent_intel.ui.theme import THEME

    console = Console(theme=THEME, width=100, record=True)
    console.print(help_text())
    text = console.export_text()

    for name in SLASH:
        if name == "quit":  # an alias for /exit, deliberately not listed twice
            continue
        assert f"/{name}" in text, f"/{name} is in the grammar but not in help"


def test_a_project_command_appears_in_help_with_brackets_intact() -> None:
    """Two things at once, both of which have bitten.

    A command nobody can discover may as well not exist, so a project's own commands are
    listed. And rich reads `[id]` as a style tag and drops it silently, which once made
    `/use [id]` render as `/use` — the help text losing the name of its own argument.
    """
    from dataclasses import dataclass

    from rich.console import Console

    from latent_intel.frontends.shell.repl import help_text
    from latent_intel.ui.theme import THEME

    @dataclass
    class Fake:
        description: str

    console = Console(theme=THEME, width=100, record=True)
    console.print(help_text({"brief": Fake("a standing brief on [topic]")}))
    text = console.export_text()

    assert "/brief" in text
    assert "[topic]" in text


def test_a_runtime_option_this_build_rejects_prints_rather_than_tracebacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Building a runtime now refuses an option it does not know, and `/runtime` is
    where a typo in one is typed. A traceback in a REPL is not a diagnosis, and nothing
    is persisted, because the config as written is what was refused."""
    from rich.console import Console

    from latent_intel import config as config_module
    from latent_intel.frontends.shell import repl
    from latent_intel.session import Session
    from latent_intel.ui.theme import THEME

    config = config_module.load()
    config.runtimes = {"claude-cli": {"modle": "opus"}}
    config_module.save(config)

    console = Console(theme=THEME, width=100, record=True)
    monkeypatch.setattr(repl, "console", console)
    repl._runtime(Session(), "claude-cli")

    text = console.export_text()
    assert "modle" in text
    assert config_module.load().runtime is None


def test_completion_offers_every_slash_command() -> None:
    """The completion list is derived, not hand-maintained.

    The literal it replaced had already drifted from `SLASH` — `exit` was missing, so
    `/ex<Tab>` completed nothing. Deriving it removes the whole class of bug.
    """
    from latent_intel.frontends.shell.parse import SLASH
    from latent_intel.frontends.shell.repl import _SLASH_NAMES

    assert set(_SLASH_NAMES) == set(SLASH)


def test_the_agent_commands_parse_bare_and_with_a_name() -> None:
    for line, argument in (("/runtime", ""), ("/runtime claude-cli", "claude-cli")):
        result = parse(line)
        assert isinstance(result, Local)
        assert result.action == "runtime" and result.argument == argument
    for line, argument in (("/model", ""), ("/model sonnet", "sonnet")):
        result = parse(line)
        assert isinstance(result, Local)
        assert result.action == "model" and result.argument == argument
    for line, argument in (("/host", ""), ("/host openrouter", "openrouter")):
        result = parse(line)
        assert isinstance(result, Local)
        assert result.action == "host" and result.argument == argument


def test_too_many_arguments_names_the_usage() -> None:
    result = parse("/runtime a b")
    assert isinstance(result, Invalid)
    assert "/runtime [name]" in result.hint


def _shell(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A recording console in place of the shell's own, returned for its text."""
    from rich.console import Console

    from latent_intel.frontends.shell import repl
    from latent_intel.ui.theme import THEME

    console = Console(theme=THEME, width=100, record=True)
    monkeypatch.setattr(repl, "console", console)
    return console


def test_host_refuses_when_no_runtime_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host is a row in one runtime's table, so storing one with no runtime would be
    storing a value with nowhere to live.

    The refusal names what is installed rather than one runtime as an example: the
    example was advice to install something on a machine that already has the others."""
    from latent_intel.frontends.shell import repl
    from latent_intel.session import Session

    console = _shell(monkeypatch)
    repl._host(Session(), "foundry")

    text = console.export_text()
    assert "no runtime configured" in text
    for kind in Session.runtime_status():
        assert kind in text


def test_bare_host_reports_the_host_the_environment_put_in_force(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`LATENT_INTEL_CUSTOM_HOST` is how one machine runs against Foundry and another
    against the public API with the same project checked out on both. Reading only the
    config file reported that machine's host as unset — and a report that contradicts
    the runtime is worse than no report."""
    from latent_intel import config as config_module
    from latent_intel.frontends.shell import repl
    from latent_intel.session import Session

    config = config_module.load()
    config.runtime = "custom"
    config_module.save(config)
    monkeypatch.setenv("LATENT_INTEL_CUSTOM_HOST", "anthropic")

    console = _shell(monkeypatch)
    repl._host(Session(), "")

    text = console.export_text()
    assert "anthropic" in text
    assert "its default" not in text


def test_bare_model_says_unset_where_the_runtime_holds_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chat row has no default model on purpose — every host names its models
    differently — so "its default" named a thing that does not exist, next to the
    runtime's own reason saying no model is set."""
    from latent_intel import config as config_module
    from latent_intel.frontends.shell import repl
    from latent_intel.session import Session

    config = config_module.load()
    config.runtime = "custom"
    config.set_runtime_option("custom", "host", "openai")
    config_module.save(config)
    # The credential, so the reason left is the model's: the variables are diagnosed
    # first, and one missing key would answer for both halves of this test.
    monkeypatch.setenv("OPENAI_API_KEY", "never-printed-key")

    console = _shell(monkeypatch)
    repl._model(Session(), "")

    text = console.export_text()
    assert "unset" in text
    assert "no model is set" in text
    assert "its default" not in text


def test_clearing_a_host_the_project_declares_reports_what_is_still_in_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`/host none` clears the user's override, not the setting: a deployment declares
    its host in the project file, and that one is still what every question goes to.
    Printing the word typed reported `none` while the declared host answered."""
    from latent_intel import settings as settings_module
    from latent_intel.frontends.shell import repl
    from latent_intel.session import Session

    directory = tmp_path / "projects"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "deploy.yaml").write_text(
        "agent:\n"
        "  runtime: custom\n"
        "  runtimes:\n"
        "    custom: {host: foundry-anthropic}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LATENT_INTEL_PROJECT", "deploy")
    settings_module.invalidate()

    console = _shell(monkeypatch)
    repl._host(Session(), "none")

    text = console.export_text()
    assert "foundry-anthropic" in text
    assert "none" not in text


def test_choosing_a_host_persists_it_under_the_runtime_it_belongs_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nested under the runtime, the way the model is: the same name means different
    things in two tables, so a flat key would be ambiguous the day a second runtime is
    configured."""
    from latent_intel import config as config_module
    from latent_intel.frontends.shell import repl
    from latent_intel.session import Session

    config = config_module.load()
    config.runtime = "custom"
    config_module.save(config)

    console = _shell(monkeypatch)
    repl._host(Session(), "anthropic")

    assert config_module.load().runtime_options("custom")["host"] == "anthropic"
    text = console.export_text()
    assert "host" in text and "anthropic" in text


def test_a_host_this_runtime_has_no_row_for_prints_the_known_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reason is the runtime's own, which is why nothing here has to know the
    tables: it names the known hosts for a typo and the missing variables otherwise."""
    from latent_intel import config as config_module
    from latent_intel.frontends.shell import repl
    from latent_intel.session import Session

    config = config_module.load()
    config.runtime = "custom"
    config_module.save(config)

    console = _shell(monkeypatch)
    repl._host(Session(), "nonsense")

    text = console.export_text()
    assert "unknown host" in text and "nonsense" in text
    assert "foundry-anthropic" in text


def test_choosing_custom_then_a_row_persists_both_and_reports_what_is_in_force(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The migration in two keystrokes. `anthropic` and `openai` named a protocol
    rather than a loop owner and were folded into `custom`, so a machine that ran one
    of them names the runtime and then the row. Both persist immediately, and a bare
    `/host` afterwards reports what the built runtime holds."""
    from latent_intel import config as config_module
    from latent_intel.frontends.shell import repl
    from latent_intel.session import Session

    console = _shell(monkeypatch)
    session = Session()
    repl._runtime(session, "custom")
    repl._host(session, "openrouter")

    saved = config_module.load()
    assert saved.runtime == "custom"
    assert saved.runtime_options("custom")["host"] == "openrouter"
    assert "host openrouter for custom" in " ".join(console.export_text().split())

    repl._host(session, "")
    assert console.export_text().count("openrouter") >= 2


def test_a_host_named_foundry_is_kept_and_the_reason_names_both_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One Foundry resource serves two surfaces on different variables and paths, so
    `foundry` is a row nobody has — and the reason is the only place someone finds out
    there are two of them. It is still written: `/host` reports the reason standing
    after it rather than refusing, the way it does for a host whose key is unset."""
    from latent_intel import config as config_module
    from latent_intel.frontends.shell import repl
    from latent_intel.session import Session

    config = config_module.load()
    config.runtime = "custom"
    config_module.save(config)

    console = _shell(monkeypatch)
    repl._host(Session(), "foundry")

    assert config_module.load().runtime_options("custom")["host"] == "foundry"
    text = console.export_text()
    assert "unknown host 'foundry'" in text
    assert "foundry-anthropic" in text and "foundry-openai" in text


def test_a_runtime_folded_into_custom_is_refused_with_the_installed_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removed rather than aliased: one deployment exists and it runs `claude-cli`, so
    alias machinery would serve nobody. `/runtime anthropic` fails the way any unknown
    kind does, and the list is what says where to go instead."""
    from latent_intel import config as config_module
    from latent_intel.frontends.shell import repl
    from latent_intel.session import Session

    console = _shell(monkeypatch)
    repl._runtime(Session(), "anthropic")

    text = " ".join(console.export_text().split())
    assert "no runtime 'anthropic'" in text
    for kind in Session.runtime_status():
        assert kind in text
    assert config_module.load().runtime is None


def test_a_host_on_a_runtime_that_has_none_is_refused_and_not_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`claude-cli` has no host table, so `host:` is an option it rejects by name. The
    refusal must also undo the write, or the runtime that answered a moment ago refuses
    every later `ask` until someone finds the stray key in the config file."""
    from latent_intel import config as config_module
    from latent_intel.frontends.shell import repl
    from latent_intel.session import Session

    config = config_module.load()
    config.runtime = "claude-cli"
    config_module.save(config)

    console = _shell(monkeypatch)
    repl._host(Session(), "foundry")

    assert "unknown option" in console.export_text()
    assert "host" not in config_module.load().runtime_options("claude-cli")


def test_a_model_the_runtime_accepts_is_persisted_and_nothing_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/model` and `/host` are one handler, so the rollback the host test pins is the
    model's too. The other half of that handler is this one: an option the runtime
    accepts is written, kept, and reported without a refusal."""
    from latent_intel import config as config_module
    from latent_intel.frontends.shell import repl
    from latent_intel.session import Session

    config = config_module.load()
    config.runtime = "custom"
    config.set_runtime_option("custom", "host", "anthropic")
    config_module.save(config)

    console = _shell(monkeypatch)
    repl._model(Session(), "claude-sonnet-5")

    assert config_module.load().runtime_options("custom")["model"] == (
        "claude-sonnet-5"
    )
    assert "✗" not in console.export_text()


def test_hosts_prints_the_whole_table_through_the_shared_printer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/hosts` and `intel hosts` are one function, so the shell cannot phrase an
    endpoint's needs differently from the CLI. Patched on `_shared`, because that is
    where the console the printer writes to lives."""
    from rich.console import Console

    from latent_intel.frontends import _shared
    from latent_intel.ui.theme import THEME

    console = Console(theme=THEME, width=100, record=True)
    monkeypatch.setattr(_shared, "console", console)
    _shared.print_hosts()

    text = console.export_text()
    for name in ("openrouter", "azure-openai", "local"):
        assert name in text
    assert "OPENROUTER_API_KEY" in text
    assert "claude-cli — no hosts" in text


def test_hosts_takes_no_argument() -> None:
    """A report, not a setting: `/host` chooses one and `/hosts` lists them all."""
    result = parse("/hosts")
    assert isinstance(result, Local)
    assert result.action == "hosts" and result.argument == ""

    assert isinstance(parse("/hosts openrouter"), Invalid)


# -- recording a session ----------------------------------------------------


def test_record_parses_bare_with_a_file_and_with_off() -> None:
    """One command with three moods, the way `/runtime` and `/project` already are."""
    for line, argument in (
        ("/record", ""),
        ("/record run.jsonl", "run.jsonl"),
        ("/record off", "off"),
    ):
        result = parse(line)
        assert isinstance(result, Local)
        assert result.action == "record" and result.argument == argument


def test_record_tees_the_session_and_off_reports_what_it_wrote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session is where experimentation actually happens, so remembering to have
    started the CLI with a flag is not a workflow.

    The recording is session state: it says where this shell sends its events, not
    anything about the machine, so the config file must come out byte for byte as it
    went in — unlike `/connect` or `/runtime`, which persist on purpose.
    """
    import anyio

    from latent_intel import config as config_module
    from latent_intel import events as ev
    from latent_intel.commands import Find
    from latent_intel.frontends.shell import repl
    from latent_intel.session import Session

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "note.md").write_text("# Note\n\nRetrieval is finding material.\n")
    config_module.save(config_module.load())
    before = config_module.config_path().read_bytes()

    console = _shell(monkeypatch)
    recording = repl.Recording()
    path = tmp_path / "session.jsonl"
    repl._record(recording, str(path))

    async def search() -> None:
        session = Session()
        await session.connect(str(corpus), kind="files", source_id="c")
        completer = repl.ShellCompleter(session)
        await repl._dispatch(session, Find(query="retrieval"), completer, recording)
        await session.aclose()

    anyio.run(search)
    repl._record(recording, "off")

    written = path.read_text().splitlines()
    assert written
    for line in written:
        assert not isinstance(ev.parse_event(line), ev.UnknownEvent)

    printed = console.export_text()
    text = " ".join(printed.split())
    # Whitespace removed rather than squeezed for the path: a long one wraps at the
    # console width, and where it wraps is not what this test is about.
    assert str(path) in "".join(printed.split())
    assert "c:note.md" in text  # teed: the terminal still saw the run
    assert f"{len(written)} events written to" in text
    assert config_module.config_path().read_bytes() == before


def test_bare_record_says_whether_anything_is_being_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The state has to be askable, or the only way to find out is to stop it."""
    from latent_intel.frontends.shell import repl

    console = _shell(monkeypatch)
    recording = repl.Recording()

    repl._record(recording, "")
    path = tmp_path / "session.jsonl"
    repl._record(recording, str(path))
    repl._record(recording, "")

    printed = console.export_text()
    assert "not recording" in printed
    assert str(path) in "".join(printed.split())
    assert "(0 events)" in " ".join(printed.split())
    assert recording.path == path
