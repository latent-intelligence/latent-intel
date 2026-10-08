# Recordings — `latent-intel.recording` v1

A recording is a session's event stream written to a file, headed by what produced it.
`--record` on `search`, `get`, `ask` and `run`, and `/record` in the shell, write one;
`intel replay` and `/replay` render one. Other programs read recordings from disk:
`latent-eval` turns them into run records in its own adapter, and a demo renderer reads
them for presentation. Neither imports `latent-intel`.

| | |
|---|---|
| Format | `latent-intel.recording` |
| Version | `1` — `SCHEMA_VERSION` in `src/latent_intel/events.py` |
| Schema | [`schemas/recording.v1.json`](../../schemas/recording.v1.json), one line |
| Samples | [`tests/fixtures/formats/recording/`](../../tests/fixtures/formats/recording/) |

## File

JSON Lines, UTF-8, one event per line, appended and flushed as the run happens. Each line
is an event with the common envelope:

| Field | Meaning |
|---|---|
| `type` | the event's kind, snake_case |
| `schema_version` | the event vocabulary's version — equal to `format_version` |
| `event_id` | unique per event |
| `session_id` | one shell session or one CLI invocation |
| `operation_id` | one user action; every event it caused shares it |
| `parent_id` | the event this one happened inside — a tool call under a subagent |
| `sequence` | ordering within an operation; restarts per operation |
| `ts` | ISO 8601, UTC |

## The header — `run_context`

**The first line of a recording is a `run_context`**, and another follows whenever what
it describes changes: a runtime, model, host or project switch, a source attached or
dropped, a skill edited. Each describes the events after it, up to the next. Every
header repeats `format` and `format_version`, so files can be concatenated freely.

| Field | Type | Meaning |
|---|---|---|
| `format` | `"latent-intel.recording"` | |
| `format_version` | `1` | |
| `purpose` | `share` · `demo` · `eval` | what the recording is for; `share` by default. A reader accepts another value; the schema holds producers to these |
| `title` | string | free text, may be empty |
| `latent_intel_version` | string | the package that wrote it |
| `runtime`, `host`, `model` | string or null | what answers `ask`; null when none is configured. The runtime's other options are left out — a `cwd` or a binary path is a location |
| `approval` | string | `ask`, `auto`, … |
| `web` | object or null | how much of the open web the turns after it could reach: `mode` (`off` · `search` · `browse`), `allowed_domains`, `blocked_domains`, `max_uses`. Null in a header written before the field existed — "not recorded", not `off` |
| `project` | string or null | the active project's name, never its path |
| `context` | object | the context engineering in force — see below |
| `sources` | list of `{id, kind}` | attached sources; never a path or a target |
| `case_id`, `variant_id`, `epoch` | string, string, integer, or null | set by batch eval runs |

`context` holds sha256 hex digests, never the text itself: `persona` (null when none),
`persona_mode`, `skills` and `agents` as `{name: digest}`, and `hash` over all of them.
Two runs with equal `hash` had the same persona, skills and agents.

## Events

The vocabulary is `AgentEvent` in `src/latent_intel/events.py`; the schema carries every
field. What a reader scoring a run usually needs:

| Event | Fields |
|---|---|
| `user_message` | `text`, `skills` run with `/name` |
| `tool_started` | `tool`, `source_id`, `arguments`, `effect` |
| `tool_result` | `tool`, `source_id`, `ok`, `output`, `error`, `duration_ms`, `refs`; `parent_id` is its `tool_started` |
| `retrieval_result` | `source_id`, `query`, `hits` — one event per source, never merged |
| `agent_completed` | `text`, `citations`, `usage` (integer counts: tokens, and `web_search_requests` / `web_fetch_requests` where the host ran web tools), `cost_usd` (null when the runtime does not report one) |
| `agent_failed` | `message`, `kind`, `remedy` |

**Refs.** `tool_result.refs` lists the `source:key` refs in `output`, in order, once each.
`agent_completed.citations` lists the runtime's own citations, then those in the answer.
Only an attached source's id makes a ref, so a URL or a clock time never does. A key runs
to the first whitespace, bracket, quote, comma, semicolon, pipe or asterisk, less trailing
`. : ! ?`.

**The web.** A call the host ran — `web_search`, `web_fetch` — is a `tool_started` /
`tool_result` pair with `source_id` `web`, `effect` `external_read` and no `duration_ms`.
Its refs, and any citations of the pages it found, are `web:` followed by the full URL.
`output` lists titles and URLs, never page text. Under the Claude Code runtimes the same
pair is named `WebSearch` / `WebFetch`, is timed, and carries the binary's own output;
only a fetch has a ref, the URL it read. `web` is reserved: no source attaches
under it.

## Reading a recording

- **Refuse a missing or unknown `format_version`.** A file with no `run_context` is not a
  recording — it is a `--json` stream, or was written before headers existed. A header
  naming another `format`, or a version this reader does not know, is refused, not
  guessed at.
- **Skip an event `type` you do not know**, and ignore fields you do not know. Adding an
  event type or an optional field does not change the version.
- **A breaking change is a new version** and a new schema file. Older readers keep
  refusing it.
- **The last line may be cut off** by an interrupt. Read up to it.

`intel replay` is a renderer and refuses nothing: it renders what it can and names, on
stderr, a first line that is not a header, a header from another format, or a newer
version.

## Privacy

Tool output carries source content verbatim, and an answer quotes it. Treat a recording
like the sources it read from. `--record NAME`, with no suffix or directory, writes to
`<data dir>/recordings/NAME.jsonl`, outside any repository. Paths, targets and credentials
are never written to a header, but a tool's arguments and output are written as they ran.
