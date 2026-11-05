"""A command line in a `target:` reads the same on every OS, and a path survives it."""

from __future__ import annotations

import shlex

import pytest

from latent_intel import commandline


def test_a_windows_path_keeps_its_backslashes() -> None:
    line = r"latent-wiki-mcp --deployment C:\Users\someone\wiki"
    assert commandline.split(line) == [
        "latent-wiki-mcp",
        "--deployment",
        r"C:\Users\someone\wiki",
    ]


def test_quotes_group_words_as_a_posix_shell_does() -> None:
    line = """serve "C:/My Data" 'a b' it"'"s"""
    assert commandline.split(line) == ["serve", "C:/My Data", "a b", "it's"]


def test_a_line_shlex_join_wrote_reads_back_as_it_was() -> None:
    """`.mcp.json` entries and the tests' own servers are recorded with `shlex.join`."""
    parts = [r"C:\Program Files\serve.exe", "it's", "a b", "--x=$1"]
    assert commandline.split(shlex.join(parts)) == parts


def test_a_line_with_no_closing_quote_is_refused() -> None:
    with pytest.raises(ValueError):
        commandline.split("serve 'open")


def test_home_and_variables_are_expanded_after_the_split(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expanding first put the value's `\\` and spaces into the line being split, so a
    Windows home folder lost its backslashes and one with a space became two words."""
    home = r"C:\Users\some one"
    monkeypatch.setenv("HOME", home)
    monkeypatch.setenv("USERPROFILE", home)
    monkeypatch.setenv("TOOLS", r"D:\a\bin")
    assert commandline.argv("~/bin/serve --from $TOOLS/x ${TOOLS}") == [
        home + "/bin/serve",
        "--from",
        r"D:\a\bin/x",
        r"D:\a\bin",
    ]


def test_a_uri_argument_is_left_as_written(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOOLS", "/opt/bin")
    assert commandline.argv("$TOOLS/bridge https://host/$PATHLIKE") == [
        "/opt/bin/bridge",
        "https://host/$PATHLIKE",
    ]
