"""The web scope as a person writes it: the short form, the mapping, and every way a
value is refused rather than half-honoured."""

from __future__ import annotations

from typing import Any

import pytest

from latent_intel.models import WebScope


@pytest.mark.parametrize(
    ("raw", "mode"),
    [(None, "off"), (False, "off"), ("off", "off"), ("search", "search")],
)
def test_the_short_form_is_a_mode(raw: Any, mode: str) -> None:
    """`web: off` reaches us as False — YAML reads a bare `off` as a boolean."""
    assert WebScope.parse(raw).mode == mode


def test_the_long_form_carries_a_domain_list_and_a_cap() -> None:
    scope = WebScope.parse(
        {"mode": "browse", "allowed_domains": ["arxiv.org"], "max_uses": 5}
    )
    assert scope == WebScope(mode="browse", allowed_domains=["arxiv.org"], max_uses=5)
    assert str(scope) == "browse (allow arxiv.org; max 5)"


@pytest.mark.parametrize(
    ("raw", "says"),
    [
        (True, "expected one of"),
        ("crawl", "no mode 'crawl'"),
        ({"mode": "search", "domains": ["a.org"]}, "unknown key"),
        (
            {
                "mode": "search",
                "allowed_domains": ["a.org"],
                "blocked_domains": ["b.org"],
            },
            "cannot both be set",
        ),
        ({"mode": "search", "allowed_domains": ["https://a.org"]}, "plain hostname"),
        ({"mode": "search", "allowed_domains": ["a.org/blog"]}, "plain hostname"),
        ({"mode": "search", "blocked_domains": ["*.a.org"]}, "plain hostname"),
        ({"mode": "search", "max_uses": 0}, "web:"),
    ],
)
def test_a_scope_that_cannot_be_honoured_as_written_is_refused(
    raw: Any, says: str
) -> None:
    with pytest.raises(ValueError, match=says):
        WebScope.parse(raw)


def test_both_lists_are_refused_however_the_scope_is_built() -> None:
    with pytest.raises(ValueError, match="cannot both be set"):
        WebScope(mode="search", allowed_domains=["a.org"], blocked_domains=["b.org"])


def test_a_new_mode_keeps_the_list_in_force_unless_one_is_given() -> None:
    base = WebScope(allowed_domains=["a.org"], max_uses=2)
    assert base.narrowed("search") == WebScope(
        mode="search", allowed_domains=["a.org"], max_uses=2
    )
    assert base.narrowed("browse", blocked=["b.org"]) == WebScope(
        mode="browse", blocked_domains=["b.org"], max_uses=2
    )
    with pytest.raises(ValueError, match="no mode"):
        base.narrowed("everything")


def test_any_drops_the_lists_and_the_cap_and_takes_no_list() -> None:
    """The one way back from a configured list or `max_uses` short of `off` — which the
    Claude Code runtimes, refusing both, need to be usable at all."""
    base = WebScope(mode="search", allowed_domains=["a.org"], max_uses=5)
    assert base.narrowed("search", unbounded=True) == WebScope(mode="search")
    with pytest.raises(ValueError, match="--any"):
        base.narrowed("search", allowed=["b.org"], unbounded=True)


def test_a_key_that_is_not_a_string_is_reported_not_raised() -> None:
    """YAML reads `1:` or `yes:` as an int or a bool; sorting one beside a str raised
    TypeError, which `settings.load()` does not catch."""
    with pytest.raises(ValueError, match="unknown key"):
        WebScope.parse({"mode": "search", 1: "x", None: "y"})


def test_a_hostname_is_stored_lowercase_without_a_trailing_dot() -> None:
    scope = WebScope.parse(
        {"mode": "search", "allowed_domains": ["ArXiv.ORG.", "b.example.org"]}
    )
    assert scope.allowed_domains == ["arxiv.org", "b.example.org"]
    with pytest.raises(ValueError, match="plain hostname"):
        WebScope(blocked_domains=["a.org.."])
