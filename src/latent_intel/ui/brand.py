"""What a deployment is called, as data.

A tool installed at a client that says LATENT is confusing to their staff, so the name,
the wordmark, the tagline, the prompt and the palette are all values a project supplies.
None of it is code, and none of it is a plugin API — a splash screen is not worth a
maintenance surface.

The engine's own branding is a project file like any other
(`data/projects/latent.yaml`), so our branding takes the client path and the client path
cannot rot unnoticed. That is the same discipline the entry points already carry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: LATENT in block characters. Widest row is 51 columns.
DEFAULT_LOGO: tuple[str, ...] = (
    "██       █████  ████████ ███████ ███    ██ ████████",
    "██      ██   ██    ██    ██      ████   ██    ██   ",
    "██      ███████    ██    █████   ██ ██  ██    ██   ",
    "██      ██   ██    ██    ██      ██  ██ ██    ██   ",
    "███████ ██   ██    ██    ███████ ██   ████    ██   ",
)


@dataclass(frozen=True)
class Brand:
    """A deployment's identity. Every field has a sane default, so a project that
    declares no branding still renders."""

    name: str = "LATENT"
    tagline: str = ""
    logo: tuple[str, ...] = DEFAULT_LOGO
    prompt: str = ""
    theme: dict[str, str] = field(default_factory=dict)

    @property
    def prompt_text(self) -> str:
        """`latent › `, unless the project says otherwise."""
        return self.prompt or f"{self.name.lower()} › "

    @property
    def logo_width(self) -> int:
        """Derived per brand, not a module constant.

        The thresholds used to be computed once from the Latent logo, so a client
        wordmark of a different width used the wrong drop-to-text point and overflowed
        the terminal — the exact failure the width test exists to catch.
        """
        return max((len(line) for line in self.logo), default=0)


DEFAULT = Brand()


def from_project(branding: dict[str, Any], directory: Path | None = None) -> Brand:
    """Build a brand from a project's `branding:` block.

    `logo` is either a path relative to the project file or an inline block scalar —
    both are useful, and telling them apart by looking for a newline is cheap and
    unambiguous enough.
    """
    if not branding:
        return DEFAULT

    logo: tuple[str, ...] = DEFAULT_LOGO
    raw = branding.get("logo")
    if isinstance(raw, str) and raw.strip():
        if "\n" in raw:
            logo = tuple(raw.rstrip("\n").split("\n"))
        elif directory is not None:
            candidate = directory / raw
            if candidate.is_file():
                logo = tuple(
                    candidate.read_text(encoding="utf-8").rstrip("\n").split("\n")
                )

    return Brand(
        name=str(branding.get("name") or "LATENT"),
        tagline=str(branding.get("tagline") or "").strip(),
        logo=logo,
        prompt=str(branding.get("prompt") or ""),
        theme={str(k): str(v) for k, v in (branding.get("theme") or {}).items()},
    )
