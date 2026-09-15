"""A stdio MCP server that reports what the child process actually received.

Here rather than in the test module because the point is the spawn: `env` and `cwd` only
mean anything across one, and an in-process server object would prove nothing about
either. It is `sys.executable` running this file — no network, no credential.
"""

from __future__ import annotations

import json
import os

from mcp.server import MCPServer

#: Reported by name, so a test can assert on forwarding without the answer depending on
#: which variables the machine running it happens to have.
REPORTED = ("PROBE_FORWARDED", "PROBE_LITERAL", "PROBE_UNNAMED")

server = MCPServer("probe", instructions="Reports its own environment.")


@server.tool()
def whoami() -> str:
    """The environment and working directory this server was started with."""
    return json.dumps(
        {
            "env": {name: os.environ[name] for name in REPORTED if name in os.environ},
            "cwd": os.getcwd(),
        }
    )


if __name__ == "__main__":
    server.run("stdio")
