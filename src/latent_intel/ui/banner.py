"""The landing page.

Two things make a terminal banner look deliberate rather than pasted:

**The version sits inside the top border.** A rule split around a dim ` v0.1.0 ` reads
as one designed object; the same text on its own line reads as debug output. Rich's
`Panel` does this natively via `title` — the first version of this file hand-rolled it
and got multi-line bodies wrong, drawing the side borders on the first row only.

**It is bounded and degrades in two steps.** The card is `min(width - 2, 96)`, so a
maximised terminal does not stretch a five-row logo across 300 columns. Below ~57 the
logo drops and a wordmark replaces it; below ~46 the table collapses to lines. A banner
that looks right at only one size breaks exactly when someone works in a split pane.

The logo is a module-level literal — one place, so a future web client can carry the
same wordmark without redrawing it.
"""

from __future__ import annotations

from rich.align import Align
from rich.box import ROUNDED
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..models import Capability, Descriptor
from .brand import DEFAULT as DEFAULT_BRAND
from .brand import Brand

#: Margin the logo needs beyond its own width before it is worth drawing.
LOGO_MARGIN = 6
#: Below this a table is worse than plain lines.
TABLE_MIN_WIDTH = 46
#: A five-row logo stretched across a maximised terminal looks accidental.
MAX_CARD_WIDTH = 96


def summarise(sources: list[Descriptor]) -> tuple[tuple[str, ...], str]:
    """Reduce attached sources to what the landing page shows.

    Lives here so both terminal frontends render the same summary from the same rule,
    rather than each deciding separately what counts as a capability.
    """
    order = [Capability.SEARCH, Capability.FETCH, Capability.TOOLS]
    present = {c for source in sources for c in source.capabilities}
    capabilities = tuple(str(c) for c in order if c in present)

    if not sources:
        return capabilities, ""
    parts = [f"{len(sources)} source" + ("s" if len(sources) != 1 else "")]
    total = sum(s.count or 0 for s in sources)
    if total:
        parts.append(f"{total:,} items")
    return capabilities, "  ·  ".join(parts)


def _body(capabilities: tuple[str, ...], scale: str, width: int) -> RenderableType:
    """What this session can do, and how much of it there is.

    Capabilities rather than source names, deliberately. A landing page is the most
    screenshotted, most over-the-shoulder surface the program has, and the names of
    someone's attached stores are the part of it least worth putting there. `intel
    stores` answers "which ones" for whoever actually needs to know.
    """
    if not capabilities:
        return Text("nothing attached — try  intel connect <store>", style="dim")

    caps = Text()
    for index, capability in enumerate(capabilities):
        if index:
            caps.append("  ·  ", style="border")
        caps.append(capability, style="accent")

    if not scale:
        return caps
    if width < TABLE_MIN_WIDTH:
        caps.append("\n")
        caps.append(scale, style="dim")
        return caps

    row = Table.grid(expand=True)
    row.add_column(justify="left")
    row.add_column(justify="right", style="dim", no_wrap=True)
    row.add_row(caps, scale)
    return row


def render(
    console: Console,
    version: str,
    capabilities: tuple[str, ...] = (),
    scale: str = "",
    hints: tuple[str, ...] = (),
    brand: Brand = DEFAULT_BRAND,
) -> None:
    """Draw the landing page at whatever width the terminal currently is."""
    card = min(max(console.size.width - 2, 24), MAX_CARD_WIDTH)
    # Derived from *this* brand: a client logo of another width would otherwise use the
    # Latent threshold and overflow.
    logo_min = brand.logo_width + LOGO_MARGIN

    blocks: list[RenderableType] = []
    # Centred against the card, not the terminal: the logo has to sit over the panel
    # below it, and the panel is the narrower of the two once the terminal is wide.
    wordmark = (
        Text("\n".join(brand.logo), style="accent.strong", no_wrap=True)
        if card >= logo_min
        else Text(brand.name, style="accent.strong")
    )
    blocks.append(Align.center(wordmark, width=card))
    if brand.tagline:
        blocks.append(
            Align.center(Text(brand.tagline, style="dim", no_wrap=True), width=card)
        )
    blocks.append(Text(""))
    blocks.append(
        Panel(
            _body(capabilities, scale, card - 4),
            title=f"v{version}",
            title_align="center",
            box=ROUNDED,
            border_style="border",
            width=card,
            padding=(0, 1),
        )
    )
    if hints:
        blocks.append(Text(""))
        hint = Text("  ".join(hints), style="dim", no_wrap=True)
        blocks.append(Align.center(hint, width=card))

    console.print(Align.center(Group(*blocks)))
