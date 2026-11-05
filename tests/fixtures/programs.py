"""A program on disk that `shutil.which` finds by its bare name on every OS.

POSIX finds an executable file; Windows finds a name only with an extension listed in
PATHEXT, so there the program is a `.cmd`. Running it creates `marker`, which is how a
test proves nothing started it.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path


def program(directory: Path, name: str, marker: Path) -> Path:
    if sys.platform == "win32":
        path = directory / f"{name}.cmd"
        path.write_text(f'@type nul > "{marker}"\r\n', encoding="utf-8")
        return path
    path = directory / name
    path.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\n", encoding="utf-8")
    path.chmod(0o755)
    return path
