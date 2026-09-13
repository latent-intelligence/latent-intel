"""One row type for every endpoint an in-process runtime can reach.

**A host is a row, not a module** — and until this file there were two row types saying
so. The Anthropic table had five fields and could express neither a fallback nor a
custom constructor; the OpenAI one grew to eleven while gaining rows, with three
different ways to spell "this variable is needed" and a constructor reading variables by
tuple position. Every rule about variables, reasons and construction existed twice and
had already drifted, and Bedrock and Vertex fit neither table.

**A row declares what its service needs once, and everything else reads that
declaration.** `required` is a tuple of names in the order they should be reported; a
nested tuple means any one of the group will do. `fallback` and `defaults` sit beside
the names they apply to. From that one declaration come the reason `intel doctor`
prints, the remedy attached to a failure, and the kwargs the client is built with — so
a name cannot be reported missing under one spelling and read under another.

**Names of variables, never their values.** Every string this module returns is printed
by `doctor`, pasted into support threads and read aloud in screen shares. A value
reaches the client and nothing else.

**A constructor reads the row's own names.** `values(host)` resolves every name the row
mentions — environment, then fallback, then default — and a constructor indexes it by
name. Precedence therefore lives in exactly one place, and a name the row never declared
raises a `KeyError` in that row's own test rather than passing None to an SDK.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

#: Every variable a row mentions, resolved once and passed along. Resolved rather than
#: re-read so the reason someone is shown and the client built afterwards can never
#: disagree about what was set.
Values = Mapping[str, str | None]

#: One thing a host needs: a variable by name, or a group of which any one will do.
Requirement = str | tuple[str, ...]


def construct_base_url(host: Host, values: Values) -> dict[str, Any]:
    """The kwargs almost every client on either protocol takes: a credential and, where
    it is not the SDK's own default, a base URL.

    The credential is read here rather than left to the SDK, because a fallback variable
    is ours and the SDK has never heard of it.
    """
    return {"api_key": values[host.key], "base_url": base_url(host, values)}


@dataclass(frozen=True)
class Host:
    """One endpoint a runtime can reach, and everything host-specific about a turn.

    The claim the design rests on: a new host — another cloud's gateway, another
    resource, another server on the desk — is a row here rather than a module.
    """

    #: The client class on the SDK module, resolved by name so the SDK stays unimported
    #: until a turn actually starts.
    client: str
    #: The credential variable. A role, not a requirement: `required` says whether it
    #: is needed, and `auth_remedy` names it when an endpoint rejects it.
    key: str
    #: Where the endpoint lives, resource first and full address last. A role too: the
    #: only positional read left in this file is `base_url`, which asks the last for an
    #: address and the first for a resource name.
    endpoint: tuple[str, ...]
    #: What this host needs, in the order a reason should report it. A plain name is
    #: required; a nested tuple is a group of which any one member will do.
    required: tuple[Requirement, ...]
    #: What a 404 most likely means here. The rows fail differently enough that one
    #: shared sentence would be wrong for most of them.
    remedy_404: str
    #: Groups of this row's own names of which at most one may be set. Two at once is
    #: reported rather than ranked silently — one of them is not doing what whoever set
    #: it thinks, and one SDK refuses the pair outright.
    exclusive: tuple[tuple[str, ...], ...] = ()
    #: Our variable name → a variable already set for another surface of the same
    #: platform that carries the same value. Read when ours is unset, and named in the
    #: reason so nobody has to know the mapping to fix it.
    fallback: dict[str, str] = field(default_factory=dict)
    #: Our variable name → the value used when it is unset. A name with a default is
    #: never reported missing, because the requirement would be one invented here
    #: rather than one the endpoint has.
    defaults: dict[str, str] = field(default_factory=dict)
    #: The base URL this row implies when no address variable is set: a template
    #: formatted with `{resource}`, a constant where a gateway publishes one URL, and
    #: None where the SDK's own default applies.
    url: str | None = None
    #: This row's values, as the kwargs its client class is constructed with. A row
    #: whose client takes `base_url` needs nothing here; one that spells its endpoint
    #: some other way supplies its own rather than making `_client` grow a branch.
    construct: Callable[[Host, Values], dict[str, Any]] = field(
        default=construct_base_url
    )


@dataclass(frozen=True)
class OpenAIHost(Host):
    """A row on the OpenAI-compatible protocol, which has one thing more to say.

    A subclass rather than a freeform mapping on `Host`: this is typed, defaulted and
    checked by mypy, and the next protocol-specific field lands the same way.
    """

    #: The output-token parameter this host accepts. `max_tokens` is deprecated on the
    #: OpenAI API and rejected by reasoning models; an older compatible host may still
    #: want it, which is why it is a row rather than a constant.
    tokens_param: str = "max_completion_tokens"


def names(host: Host) -> tuple[str, ...]:
    """Every variable name this row mentions, once each, in the order it mentions them.

    The row's own names only. What stands in for one of them is a fallback target and
    is deliberately absent: a caller asks for the name it declared and gets whatever
    answered for it.
    """
    found: list[str] = [host.key, *host.endpoint]
    for requirement in host.required:
        found.extend([requirement] if isinstance(requirement, str) else requirement)
    for group in host.exclusive:
        found.extend(group)
    found.extend(host.defaults)
    return tuple(dict.fromkeys(found))


def value(host: Host, name: str) -> str | None:
    """One variable's value: the environment, then what this host accepts in its place,
    then what the row supplies when nobody set anything."""
    return (
        os.environ.get(name)
        or os.environ.get(host.fallback.get(name, ""))
        or host.defaults.get(name)
    )


def named(host: Host, name: str) -> str:
    """One variable, as it is reported when it is missing — with the variable that
    would also do, because a deployment that has set that one is already finished."""
    other = host.fallback.get(name)
    return f"{name} (or {other})" if other else name


def values(host: Host) -> dict[str, str | None]:
    """Every name this row mentions, resolved. What a constructor is handed."""
    return {name: value(host, name) for name in names(host)}


def _resolved(host: Host, given: Values | None) -> Values:
    """The values a caller passed, or a fresh resolution when it passed none."""
    return values(host) if given is None else given


def base_url(host: Host, values: Values | None = None) -> str | None:
    """Where the client points, or None to let the SDK use its own default.

    In order: the last `endpoint` variable, which is the full address and says exactly
    where to go; then the row's `url`, filled in from the first `endpoint` variable
    where the template names a resource, or taken verbatim where it names none; then
    nothing. A resource name and a full base URL are two ways of saying the same thing,
    so the second is built from the first rather than asked for twice. Nothing here is
    logged: a base URL is not a credential, but it is read from the same place one is.
    """
    resolved = _resolved(host, values)
    address = resolved[host.endpoint[-1]]
    if address:
        return address
    if host.url is None:
        return None
    if "{resource}" not in host.url:
        return host.url
    resource = resolved[host.endpoint[0]]
    return host.url.format(resource=resource) if resource else None


def missing(host: Host, values: Values, *, name: str) -> str | None:
    """What this host still needs, named, or None when it needs nothing.

    Reported in the row's own order and joined with `or` inside a group, because the
    reason is read top to bottom by someone exporting variables.
    """
    absent: list[str] = []
    for requirement in host.required:
        if isinstance(requirement, str):
            if not values[requirement]:
                absent.append(named(host, requirement))
        elif not any(values[member] for member in requirement):
            absent.append(" or ".join(named(host, member) for member in requirement))
    if not absent:
        return None
    return f"set {', '.join(absent)} for host '{name}'"


def conflict(host: Host, *, name: str) -> str | None:
    """Two of one exclusive group set at once, named, or None.

    Read from `os.environ` under this row's own names rather than from resolved values,
    so an address set for this surface beats a resource inherited through a fallback
    rather than being reported as a contradiction — that pair is one deployment's setup
    plus one deliberate override.
    """
    for group in host.exclusive:
        both = [member for member in group if os.environ.get(member)]
        if len(both) > 1:
            return f"set only one of {', '.join(both)} for host '{name}' — both are set"
    return None


def diagnose(host: Host, *, name: str) -> str | None:
    """Why this host cannot be reached from here, or None.

    What is missing before what contradicts: a machine with nothing set has no
    contradiction to report, and naming one would bury the four variables it wants.
    """
    return missing(host, values(host), name=name) or conflict(host, name=name)


def unknown(name: str, known: Iterable[str]) -> str:
    """A host nobody has a row for. Sorted, because the order a table happens to be
    written in is not an order anyone can scan."""
    return f"unknown host '{name}' — known hosts: {', '.join(sorted(known))}"


def auth_remedy(host: Host) -> str:
    """What to check when an endpoint rejects the credentials.

    The whole group where the credential is one of several alternatives: naming only
    one sends someone to check a variable they deliberately left unset.
    """
    group = named(host, host.key)
    for requirement in host.required:
        if not isinstance(requirement, str) and host.key in requirement:
            group = " or ".join(named(host, member) for member in requirement)
            break
    return f"check {group} — we report the name, never the value"


def connection_remedy(host: Host, values: Values) -> str:
    """What to check when the endpoint did not answer.

    A row reached at a default has no variable to check, and naming one nobody set
    reads as a misconfiguration rather than an outage — so what was dialled is named
    instead: the row's own URL where it implies one, and the SDK's default where
    neither the row nor the environment says anything.
    """
    if not any(values[name] for name in host.endpoint):
        url = base_url(host, values)
        if url is None:
            return "could not reach the SDK's default endpoint — check the network"
        return f"could not reach {url} — check the network"
    addresses = " or ".join(named(host, name) for name in host.endpoint)
    return f"check {addresses} and the network"


__all__ = [
    "Host",
    "OpenAIHost",
    "Requirement",
    "Values",
    "auth_remedy",
    "base_url",
    "conflict",
    "connection_remedy",
    "construct_base_url",
    "diagnose",
    "missing",
    "named",
    "names",
    "unknown",
    "value",
    "values",
]
