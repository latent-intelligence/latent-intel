"""A stand-in for the `claude` binary: replays a recorded stream, then exits.

Injected as `command=[sys.executable, this_file, scenario]` rather than shimmed onto
PATH — no PATH mutation, no chmod, portable, and the real `open_process`, line
splitting, stderr draining and exit-code handling still run.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SCENARIOS = Path(__file__).parent / "claude_cli"


def main() -> int:
    scenario = sys.argv[1] if len(sys.argv) > 1 else "text"
    sys.stdin.read()  # the prompt arrives here; drain it like the real binary
    if scenario == "mcp_config":
        # Answers with the block it was given, so a test sees what the binary sees.
        block = sys.argv[sys.argv.index("--mcp-config") + 1]
        result = {"type": "result", "is_error": False, "result": block}
        print(json.dumps(result), flush=True)
        return 0
    path = SCENARIOS / f"{scenario}.jsonl"
    if path.is_file():
        sys.stdout.write(path.read_text(encoding="utf-8"))
        sys.stdout.flush()
    if scenario in {"empty", "model_error"}:
        print(f"fake_claude: {scenario}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
