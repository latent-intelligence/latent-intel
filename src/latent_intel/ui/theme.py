"""Semantic colour tokens. Retheming is this file and nothing else.

Every renderer says `[accent]` or `[dim]`, never a colour. The rule is worth keeping
even at this size: the moment one module writes `[green]`, a theme change becomes a
grep, and the web client — which needs the same tokens in CSS — has nothing to map from.

The palette is Everforest, chosen because it is legible on both dark and light terminals
and easy on the eyes for long sessions. It is stated as truecolor deliberately: the
alternative, rich's named colours, means the product looks different on every terminal
and the landing page can never be trusted to look right.
"""

from __future__ import annotations

from rich.theme import Theme

#: Named colours, so the semantic mapping below reads as intent rather than hex.
PALETTE = {
    "sage": "#a7c080",
    "teal": "#7fbbb3",
    "ink": "#d3c6aa",
    "ash": "#859289",
    "slate": "#5c6a72",
    "amber": "#dbbc7f",
    "rust": "#e67e80",
    "orchid": "#d699b6",
}

#: The semantic tokens this program uses. A project may override any of these and
#: nothing else — rich's own built-ins are not ours to hand out.
SEMANTIC = {
    # -- roles ----------------------------------------------------------
    "accent": PALETTE["sage"],
    "accent.strong": f"bold {PALETTE['sage']}",
    "text": PALETTE["ink"],
    "dim": PALETTE["ash"],
    "border": PALETTE["slate"],
    "border.accent": PALETTE["teal"],
    # -- severity -------------------------------------------------------
    "ok": PALETTE["sage"],
    "warn": PALETTE["amber"],
    "fail": PALETTE["rust"],
    # -- domain ---------------------------------------------------------
    "source": PALETTE["teal"],
    "kind": PALETTE["orchid"],
    "score": PALETTE["ash"],
    "ref": PALETTE["teal"],
    "tool": PALETTE["amber"],
    "prompt": f"bold {PALETTE['sage']}",
}

THEME = Theme(SEMANTIC)


#: Every token a project may override. An unknown name is reported rather than silently
#: ignored — a typo'd `acccent` that changes nothing is worse than an error.
TOKENS = frozenset(SEMANTIC)


def build_theme(overrides: dict[str, str] | None = None) -> tuple[Theme, list[str]]:
    """The palette with a project's (and a user's) tokens merged over it.

    Returns the theme and the names it did not recognise, so a caller can report them.
    Retheming stays this file plus data — there is no second palette anywhere, which is
    what the module docstring promises and what `PROMPT_STYLE` used to quietly break.
    """
    if not overrides:
        return THEME, []
    unknown = sorted(set(overrides) - TOKENS)
    merged = {**SEMANTIC, **{k: v for k, v in overrides.items() if k in TOKENS}}
    return Theme(merged), unknown


def prompt_style(theme: Theme | None = None) -> str:
    """The `prompt` token, as a string prompt_toolkit understands.

    `repl.py` used to carry a copy of the accent hex, because prompt_toolkit cannot
    read a rich `Theme`. It can read what a rich `Style` stringifies to, so the bridge
    is one expression rather than a second palette that drifts.
    """
    return str((theme or THEME).styles["prompt"])
