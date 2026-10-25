# What latent-intel reads and writes

For tools that feed this package, or read what it writes. Only a **published** format is a
contract: it carries a `format` and a version, has a JSON Schema in [`schemas/`](../../schemas/),
and a breaking change gets a new version. Everything else may change without notice; a tool that
needs one of those as a contract should ask for it to be published.

## Reads

| Input | Read by | Version | Shape | Refused |
|---|---|---|---|---|
| A deployment, `<name>.yaml` | `deployment` commands, every command once active | `schema_version: 1`, not checked | [Deployment declaration](../deployments.md) | A file that is not YAML or not a mapping. Anything else is a problem reported by `deployment validate`, and the rest still loads |
| Files a user points at | `files` sources | none | Markdown under one directory, `**/*.md` by default | A key that climbs out of the directory |
| A codebase | `code` sources | none | Text files under one directory | A key that climbs out of it; a file outside `include` or inside `exclude`; a `graph` option |
| An MCP server | `mcp` sources | MCP | A stdio command or a Streamable HTTP URL. Stores are reached the same way, through their own servers: `latent-wiki serve` and `latent-records serve`, not yet published | An option other than `env`, `cwd` and `server_stderr`; an `env` name that is unset |
| `.mcp.json`, `.vscode/mcp.json` | `connect --from` | none | `mcpServers` or `servers`: `command`, `args`, `url`, `env`, `cwd` | `headers`; a file of several servers when none is named |
| A registry, `registry.yaml` | `connect <id>`, `stores`, a source's `from:` | none | `stores:`, each with `id`, `kind`, `access`, `paths` | An id it does not hold; a wiki entry without `access: mcp:<command>` |
| `.env` | every command | none | dotenv | Nothing; it never overrides an exported variable ([credentials](../deployments.md#credentials)) |
| A recording | `replay`, `/replay` | [`latent-intel.recording`](recording.md) v1 | JSON Lines | Nothing: a line it cannot read renders as unknown, and a missing header or newer version is named on stderr |
| Cases | `eval run` | `latent-eval.dataset` v1, published by latent-eval | YAML or JSON Lines; `id`, `input.question` and `metadata` are read | [Inputs](eval-run.md#inputs) |
| Variants | `eval run` | none | YAML, `variants:` of `id`, `deployment`, `runtime`, `options` | An unknown key; `approval` in `options`; a deployment with a problem |

## Writes

| Output | Written by | Published? |
|---|---|---|
| Recordings | `--record` on `search`, `get`, `ask` and `run`; `/record` | **Yes:** [`latent-intel.recording`](recording.md) v1 |
| The event stream on stdout | `--json` on the same commands | **Yes:** each line is an event of [`latent-intel.recording`](recording.md) v1, with no header |
| Eval runs | `eval run` | **Yes:** [`latent-intel.eval-run`](eval-run.md) v1. latent-eval reads it |
| A new deployment file | `deployment new` | No: the worked example, commented |
| Your config, `config.yaml`, and its overlays | `connect`, `disconnect`, `deployment use` and `add`; the shell's `/connect`, `/disconnect`, `/deployment`, `/runtime`, `/model` and `/host` | No: [your config](../deployments.md#your-config) |
| The managed agent's ids | `managed sync`, in your config's overlay | No |
| Shell history | the shell, `~/.local/share/latent-intel/history` | No |
| Terminal output | every command | No: for people to read |
