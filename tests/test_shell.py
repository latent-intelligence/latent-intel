"""Shell input parsing.

Split from the loop so it can be tested exhaustively without a terminal — which is the
only reason it is a separate module. Everything the shell decides about a line is here;
the loop only routes the result.
"""

from __future__ import annotations

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


def test_too_many_arguments_names_the_usage() -> None:
    result = parse("/runtime a b")
    assert isinstance(result, Invalid)
    assert "/runtime [name]" in result.hint
