# Ask with web access

Let the agent search or read the open web as well as your sources, and bound where it may go.
The web is off unless something turns it on.

| Mode | The agent may |
|---|---|
| `off` | use the attached sources only; the default |
| `search` | also search the web, seeing result titles and URLs |
| `browse` | also read the pages it finds |

## Turn it on

For one question, or one shell session, without writing anything:

```
intel ask "What changed in agent memory research this month?" --web search
latent › /scope browse --allow arxiv.org,docs.python.org
latent › /scope                      # what is in force
latent › /scope off
```

As a default, in the deployment, or at the top level of your own config, which wins over it:

```yaml
# research.yaml
defaults:
  web: {mode: browse, allowed_domains: [arxiv.org, docs.python.org], max_uses: 5}

# ~/.config/latent-intel/config.yaml
web: search
```

`allowed_domains` or `blocked_domains`, never both, are plain hostnames with no scheme, path or
wildcard. `max_uses` caps the calls per web tool per question. Every key:
[defaults](../deployments.md#defaults).

**A mode alone keeps the configured list and `max_uses`,** so turning `search` on for one
question keeps a deployment's allow-list. In the shell, `/scope <mode> --allow …` or `--block …`
replaces the list. `--any`, on `/scope` or `intel ask`, drops both lists and `max_uses`. `/scope` lasts for the session and is never saved, and switching
deployment drops it.

## Which runtimes honour it

A runtime either honours the scope as set or refuses the question; it never answers without the
web when the scope says otherwise. `intel doctor` lists the modes each runtime honours, and warns
when the configured one would refuse the scope in force:

```
web off (engine)
  anthropic-sdk      off · search · browse
  claude-agent-sdk   off · search · browse
  claude-cli         off · search · browse
  claude-managed-agents off · search · browse
  custom             off
  openai-agents      off
```

That was captured with no host chosen for `custom`, which then honours only `off`. In short: a
domain list holds on `custom` and `anthropic-sdk` with an Anthropic host and on
`claude-managed-agents`; the two Claude Code runtimes refuse any list or `max_uses`, so use
`/scope search --any` there. The full table: [the web](../runtimes.md#the-web).

## What a run records

Each web search or page read is a tool call under the source id `web`, and the pages it found are
`web:<url>` refs on its tool result; those the answer cites join its citations. Under the Claude Code runtimes a search's
results arrive as prose, so only a fetched page becomes a ref. A recording's header carries the
scope, and `/scope` writes a new header ([recording](../contracts/recording.md)).

## Known limits

- **Browsing with sources attached can leak them.** A page the agent fetches is an address it
  chose, and the address can carry what your sources said. `intel doctor` warns when `browse` is
  on with sources attached and no `allowed_domains`. On the Claude Code runtimes, which cannot
  hold an allow-list, use `search` unless the sources are ones you would publish.
- **A `web:` value that does not parse turns the web off.** `intel deployment show` says why;
  `intel doctor` shows only `off`.
