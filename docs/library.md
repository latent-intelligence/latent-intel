# Python library

The public API, by import path. Both frontends are built on these calls and nothing else. The
names nearly every caller uses are exported from `latent_intel` itself; importing it loads no
runtime SDK.

| Module | Public names |
|---|---|
| `latent_intel` | `AgentEvent`, `dump_event`, `parse_event`, `read_stream`, `Capability`, `Descriptor`, `Doc`, `Effect`, `Hit`, `Provenance`, `Ref`, `SessionError`, `ToolSpec` |
| `latent_intel.session` | `Session` |
| `latent_intel.commands` | `Command`, `Connect`, `Disconnect`, `ListSources`, `Find`, `Fetch`, `Ask` |
| `latent_intel.events` | every event class, `UnknownEvent`, `RunContext`, `SCHEMA_VERSION`, `RECORDING_FORMAT`, `recording_schema` |
| `latent_intel.models` | the value types above, `WebScope`, and the errors |
| `latent_intel.connectors.base` | `Connector`, `Searchable`, `Fetchable`, `ToolProvider`, `AsyncOpen`, `Servable`, `SelfNamed`, `ListsKeys` |
| `latent_intel.agent.base` | `Runtime` and the optional Protocols beside it |

Any other module, and any name starting with `_`, is internal and may change. To read what this
package writes without it, use the [published formats](contracts/index.md).

## A session

```python
import anyio

from latent_intel.session import Session


async def main() -> None:
    session = Session()
    try:
        await session.connect("~/notes", kind="files", source_id="notes")
        grouped = await session.find("context collapse", limit=3)
        for source_id, hits in grouped.items():
            for hit in hits:
                print(source_id, hit.ref, hit.score)
        doc = await session.fetch("notes:context-collapse.md")
        print(doc.title)
    finally:
        await session.aclose()


anyio.run(main)
```

`Session(session_id=None, runtime=None, registry_path=None, deployment=None)` reads the active
deployment for its persona, skills, runtime and web scope, or the deployment file at
`deployment`. It attaches nothing by itself: the CLI attaches your recorded sources first.
Always `aclose()` it, since an `mcp` source is a process or a connection.

- **`await connect(spec, *, kind=None, source_id=None, remote=False, **options)`** attaches a
  source and returns its `Descriptor`. A `spec` with no `kind` is a registry id. `options` are
  the [source options](deployments.md#sources). Raises `ConnectError`.
- **`await disconnect(source_id)`**, **`sources()`** (attached, in order) and **`available()`**
  (the registry's entries).
- **`await find(query, *, source=None, limit=10, **filters)`** returns
  `{source_id: [Hit, ...]}`, never one merged list. A source that fails gives an empty list;
  with no source to search, it raises `SessionError` rather than returning `{}`.
  `filters` go to every source, and a source refuses one it cannot apply: none of `files`,
  `code` and `mcp` has a field to filter on, so a non-empty filter is that source's failure.
- **`await fetch(ref)`** returns a `Doc`. A bare key reads from `session.current`, the first
  attached source unless set; with none, it raises a plain `ValueError` today, and so does
  `run(Fetch(...))`.
- **`tools()`** lists every attached tool as a `ToolSpec`, and **`await call_tool(source_id,
  name, arguments)`** runs one.
- **`ask(prompt, *, skills=None)`** streams one turn's events through the configured runtime,
  keeping the conversation for the next call.
- **`run(command)`** streams the events for one `Command`, below.

These methods raise `SessionError` or a subclass from `latent_intel.models`: `ConnectError`,
`CapabilityError` (a source asked for what it does not do), `RuntimeUnavailable`, `RuntimeFailed`.

## Commands and events

```python
from latent_intel import dump_event
from latent_intel.commands import Find

async for event in session.run(Find(query="context collapse", limit=3)):
    print(dump_event(event))
```

A command is `Connect`, `Disconnect`, `ListSources`, `Find(query, source=None, limit=10,
filters={})`, `Fetch(ref)` or `Ask(prompt, skills={})`. For a failure a person should see,
`run` emits `agent_failed` and ends rather than raising; the bare-key `ValueError` above is the
one exception today. Every event carries the envelope and is
JSON-serializable; the vocabulary and its fields are in [recording](contracts/recording.md).

- **`dump_event(event)`** is one JSON line.
- **`parse_event(line_or_dict)`** never raises: a `type` it does not know, or a line it cannot
  read, becomes an `UnknownEvent` holding the original payload.
- **`read_stream(text)`** parses a JSON Lines string, skipping blank lines.

## Values

| Type | Fields |
|---|---|
| `Hit` | `ref`, `source_id`, `title`, `kind`, `lead`, `score` (comparable within one source only), `provenance`, `metadata` |
| `Doc` | `ref`, `source_id`, `title`, `body`, `kind`, `metadata`, `provenance` |
| `Provenance` | `source_id`, `method`, `ref`, `origin` (the underlying material), `as_of` |
| `Descriptor` | `id`, `kind`, `title`, `capabilities`, `count`, `unit`, `freshness`, `detail` |
| `ToolSpec` | `name`, `source_id`, `description`, `effect`, `effect_declared`, `input_schema`; `qualified` is `source.tool` |
| `Ref` | `source_id`, `key`; `Ref.parse("notes:a.md")` |
| `Capability` | `search`, `fetch`, `tools` |
| `Effect` | `none`, `external_read`, `local_write`, `external_write`, `destructive` |

## Adding a connector or a runtime

Both are Python entry points, and this package's own register the same way:
`latent_intel.connectors` and `latent_intel.runtimes` in `pyproject.toml`. A connector class is
built as `cls(source_id, target, **options)`, satisfies `Connector` (`id`, `kind`, `describe()`,
`aclose()`) and declares its capabilities in `describe()`; each one it claims, such as
`Searchable`, is checked when it attaches. A runtime class is built as `cls(**options)`, always
with `approval=`, and satisfies `Runtime`: an `id`, `available()`, and
`stream(messages, tools, *, emitter, **options)`, which yields events. Read the module
docstrings of `connectors/base.py` and `agent/base.py` before writing one.
