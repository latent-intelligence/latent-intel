"""The row type both in-process runtimes declare their endpoints with.

Exercised against synthetic rows rather than the shipped ones: a test reading
`HOSTS["foundry"]` would pass or fail on a decision about Foundry, while what is under
test here is the rules — what a requirement means, when a fallback counts, what reaches
a constructor. The shipped rows are checked in each runtime's own file, which is where
their variable names belong.

Every variable is `AZURE_T_*`, so `conftest`'s `AZURE_` prefix deletes it whether this
file set it or a developer exported it.
"""

from __future__ import annotations

from typing import Any

import pytest

from latent_intel.agent import hosts
from latent_intel.agent.hosts import Host


def row(**overrides: Any) -> Host:
    """A row with only what every row must have, and one field changed per test."""
    fields: dict[str, Any] = {
        "client": "AsyncThing",
        "key": "AZURE_T_KEY",
        "endpoint": ("AZURE_T_BASE_URL",),
        "required": ("AZURE_T_KEY",),
        "remedy_404": "check the name against what the endpoint publishes",
    }
    fields.update(overrides)
    return Host(**fields)


# -- what a row says it needs -----------------------------------------------


def test_requirements_are_reported_in_the_order_the_row_declares_them() -> None:
    """The order is the row's, not this module's: a reason is read top to bottom by
    someone exporting variables, and the credential comes before the address."""
    host = row(
        endpoint=("AZURE_T_RESOURCE", "AZURE_T_BASE_URL"),
        required=(
            "AZURE_T_KEY",
            ("AZURE_T_RESOURCE", "AZURE_T_BASE_URL"),
            "AZURE_T_VERSION",
        ),
    )
    assert hosts.diagnose(host, name="t") == (
        "set AZURE_T_KEY, AZURE_T_RESOURCE or AZURE_T_BASE_URL, AZURE_T_VERSION "
        "for host 't'"
    )


def test_any_one_member_of_a_group_satisfies_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resource name and a full address are two ways of saying the same thing, and
    demanding both would refuse a correctly configured machine."""
    host = row(
        endpoint=("AZURE_T_RESOURCE", "AZURE_T_BASE_URL"),
        required=("AZURE_T_KEY", ("AZURE_T_RESOURCE", "AZURE_T_BASE_URL")),
    )
    monkeypatch.setenv("AZURE_T_KEY", "k")
    monkeypatch.setenv("AZURE_T_BASE_URL", "https://written.example/v1")
    assert hosts.diagnose(host, name="t") is None


def test_a_requirement_that_is_not_an_address_is_found_by_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dated API version one row needs: nothing about it is an endpoint, and the
    row type that had a slot for the credential and a slot for the address had nowhere
    to put it."""
    host = row(required=("AZURE_T_KEY", "AZURE_T_VERSION"))
    monkeypatch.setenv("AZURE_T_KEY", "k")
    assert hosts.diagnose(host, name="t") == "set AZURE_T_VERSION for host 't'"
    monkeypatch.setenv("AZURE_T_VERSION", "2026-01-01")
    assert hosts.diagnose(host, name="t") is None
    assert hosts.values(host)["AZURE_T_VERSION"] == "2026-01-01"


def test_a_fallback_satisfies_a_requirement_and_is_named_while_it_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A variable already set for a sibling surface carries the same value, so a
    deployment that has it is finished — and the reason has to say so, or someone sets
    a second variable to a value they already have."""
    host = row(fallback={"AZURE_T_KEY": "AZURE_T_OTHER_KEY"})
    assert hosts.diagnose(host, name="t") == (
        "set AZURE_T_KEY (or AZURE_T_OTHER_KEY) for host 't'"
    )
    monkeypatch.setenv("AZURE_T_OTHER_KEY", "k")
    assert hosts.diagnose(host, name="t") is None
    assert hosts.values(host)["AZURE_T_KEY"] == "k"


def test_a_default_is_never_missing_and_reaches_the_constructor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A server that authenticates nobody still has to be given something, because the
    SDK refuses to build a client with no credential at all — and that is the row's to
    supply, not a branch in the constructor."""
    host = row(
        required=("AZURE_T_KEY", "AZURE_T_BASE_URL"),
        defaults={"AZURE_T_KEY": "local"},
    )
    monkeypatch.setenv("AZURE_T_BASE_URL", "http://localhost:11434/v1")
    assert hosts.diagnose(host, name="t") is None
    assert hosts.construct_base_url(host, hosts.values(host)) == {
        "api_key": "local",
        "base_url": "http://localhost:11434/v1",
    }


def test_an_exclusive_pair_is_read_under_this_row_s_own_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An address set here plus a resource inherited through a fallback is one
    deployment's setup and one deliberate override, not a contradiction. Two of this
    row's own names is the contradiction, and the one worth refusing."""
    host = row(
        endpoint=("AZURE_T_RESOURCE", "AZURE_T_BASE_URL"),
        required=("AZURE_T_KEY", ("AZURE_T_RESOURCE", "AZURE_T_BASE_URL")),
        exclusive=(("AZURE_T_RESOURCE", "AZURE_T_BASE_URL"),),
        fallback={"AZURE_T_RESOURCE": "AZURE_T_OTHER_RESOURCE"},
    )
    monkeypatch.setenv("AZURE_T_KEY", "k")
    monkeypatch.setenv("AZURE_T_OTHER_RESOURCE", "inherited")
    monkeypatch.setenv("AZURE_T_BASE_URL", "https://written.example/v1")
    assert hosts.diagnose(host, name="t") is None

    monkeypatch.setenv("AZURE_T_RESOURCE", "ours")
    assert hosts.diagnose(host, name="t") == (
        "set only one of AZURE_T_RESOURCE, AZURE_T_BASE_URL for host 't' — both are set"
    )


def test_an_unknown_host_lists_the_known_ones_in_order() -> None:
    """Sorted, because the order a table happens to be written in is not an order
    anyone can scan."""
    assert hosts.unknown("x", {"zebra": None, "alpha": None}) == (
        "unknown host 'x' — known hosts: alpha, zebra"
    )


# -- what a row resolves to --------------------------------------------------


def test_values_carries_every_name_the_row_mentions_and_no_fallback_target() -> None:
    """A constructor reads `values["OUR_NAME"]` and never the variable that stood in
    for it, so precedence lives in one place. A name the row never mentions is a
    `KeyError` in that row's own test rather than a silent None at construction."""
    host = row(
        endpoint=("AZURE_T_RESOURCE", "AZURE_T_BASE_URL"),
        required=(
            "AZURE_T_KEY",
            ("AZURE_T_RESOURCE", "AZURE_T_BASE_URL"),
            "AZURE_T_VERSION",
        ),
        fallback={"AZURE_T_KEY": "AZURE_T_OTHER_KEY"},
        defaults={"AZURE_T_EXTRA": "x"},
    )
    resolved = hosts.values(host)
    assert set(resolved) == {
        "AZURE_T_KEY",
        "AZURE_T_RESOURCE",
        "AZURE_T_BASE_URL",
        "AZURE_T_VERSION",
        "AZURE_T_EXTRA",
    }
    with pytest.raises(KeyError):
        resolved["AZURE_T_OTHER_KEY"]


def test_where_a_client_points_is_the_address_then_the_template_then_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resource name is what a portal shows and a URL is what it implies, so the
    second is built from the first rather than asked for twice — and an address written
    out says exactly where to go, so it wins."""
    host = row(
        endpoint=("AZURE_T_RESOURCE", "AZURE_T_BASE_URL"),
        url="https://{resource}.example/v1",
    )
    assert hosts.base_url(host) is None
    monkeypatch.setenv("AZURE_T_RESOURCE", "demo")
    assert hosts.base_url(host) == "https://demo.example/v1"
    monkeypatch.setenv("AZURE_T_BASE_URL", "https://written.example/v1")
    assert hosts.base_url(host) == "https://written.example/v1"


def test_one_url_field_serves_a_template_and_a_constant_alike() -> None:
    """A gateway publishes one address and needs no variable at all; a row with no
    `url` must be left to the SDK's own default, which is None rather than an empty
    string."""
    assert hosts.base_url(row(url="https://gateway.example/v1")) == (
        "https://gateway.example/v1"
    )
    assert hosts.base_url(row()) is None


# -- what a failure says -----------------------------------------------------


def test_the_auth_remedy_names_the_group_the_credential_belongs_to() -> None:
    """Where two credentials are alternatives, naming only one sends someone to check
    a variable they deliberately left unset."""
    grouped = row(required=(("AZURE_T_KEY", "AZURE_T_TOKEN"),))
    assert hosts.auth_remedy(grouped) == (
        "check AZURE_T_KEY or AZURE_T_TOKEN — we report the name, never the value"
    )
    single = row(fallback={"AZURE_T_KEY": "AZURE_T_OTHER_KEY"})
    assert hosts.auth_remedy(single) == (
        "check AZURE_T_KEY (or AZURE_T_OTHER_KEY) — we report the name, never the value"
    )


def test_the_connection_remedy_names_a_variable_a_url_or_neither(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row reached at a default has no variable to check, and naming one nobody set
    reads as a misconfiguration rather than an outage — so what was dialled is named
    instead, and where nothing was, the network is."""
    host = row(
        endpoint=("AZURE_T_RESOURCE", "AZURE_T_BASE_URL"),
        url="https://{resource}.example/v1",
    )
    assert hosts.connection_remedy(host, hosts.values(host)) == (
        "could not reach the SDK's default endpoint — check the network"
    )

    constant = row(url="https://gateway.example/v1")
    assert hosts.connection_remedy(constant, hosts.values(constant)) == (
        "could not reach https://gateway.example/v1 — check the network"
    )

    monkeypatch.setenv("AZURE_T_BASE_URL", "https://written.example/v1")
    assert hosts.connection_remedy(host, hosts.values(host)) == (
        "check AZURE_T_RESOURCE or AZURE_T_BASE_URL and the network"
    )
