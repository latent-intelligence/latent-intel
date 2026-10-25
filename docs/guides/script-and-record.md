# Script and record

Read latent-intel's output from another program, and keep a run to replay or share. Both rest on
one thing: every command produces a stream of events, and the terminal output is a rendering of
it. The events and their fields: [recording](../contracts/recording.md).

## JSON on stdout

`--json` on `search`, `get`, `ask` and `run` prints one event per line instead of rendering:

```
$ intel search "context collapse" --limit 1 --json | jq -c '{type, sequence, source_id}'
{"type":"retrieval_started","sequence":1,"source_id":null}
{"type":"retrieval_result","sequence":2,"source_id":"notes"}
```

Every line carries the envelope, `schema_version`, `event_id`, `session_id`, `operation_id`,
`parent_id`, `sequence` and `ts`, and every result its `provenance`. Warnings go to stderr, so
stdout stays JSON. A command whose stream carried an `agent_failed` event exits 1
([exit codes](../cli.md#exit-codes)).

From Python, `latent_intel.parse_event(line)` reads a line, and returns an `UnknownEvent` for a
type it does not know rather than raising ([library](../library.md#commands-and-events)).

## Record a run

`--record` keeps the live view and also appends every event to a file, after a header saying
what produced it:

```
$ intel search "context collapse" --record first-look --title "first look"
● share "first look" — no runtime · context e1cacd58 · sources notes
searching notes…
…
$ intel replay first-look
```

- **A plain name,** with no suffix or directory, is
  `~/.local/share/latent-intel/recordings/<name>.jsonl`, so the shell and the CLI find the same
  file. Anything else, such as `run.jsonl` or `./run`, is a path as typed.
- **The header** names the purpose (`share` by default, `demo` or `eval` with `--purpose`), the
  title, the runtime, host and model, the deployment, a hash of the persona, skills and subagents,
  and the sources by id and kind, never a path.
- **Two runs recorded to one file** replay as one, each with its own header. An interrupted run
  replays up to the interruption.

In the shell, `/record first-look --purpose demo` starts recording, and a new header is written
whenever the runtime, model, deployment, skills, sources or web scope change. `/record` alone
says what is being recorded, and `/record off` stops and says how many events were written.

## Replay

```bash
intel replay first-look                  # as it looked
intel replay first-look --since 12       # from line 12 of the file
intel replay first-look --json           # the lines, unchanged, for another program
intel replay ./capture.jsonl             # a saved --json stream replays too
```

`--since` counts lines of the file, as an editor numbers them. A line this build cannot read,
such as one cut off by an interrupt or written by a newer build, renders as an unknown event
rather than stopping the replay; a file with no header, or from a newer version, is named on
stderr.

## Keep recordings private

A recording holds what the tools returned, so it carries your sources' content. Treat it like
the sources themselves. Paths, targets and credentials are never written to a header, but a
tool's arguments and output are written as they ran.
