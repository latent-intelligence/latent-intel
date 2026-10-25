# Use managed agents

`claude-managed-agents` runs the agent loop on Claude Managed Agents, Anthropic's service. The
agent is a versioned definition the service holds, created ahead of any question, and each
conversation is a session on one version of it. Two things come with that: a hard dollar budget
per session, and versions that map one to one onto eval variants. Your sources stay here: the
service calls their tools back over the session, so none needs a public address.

## Before you start

- The base install has it; it needs `ANTHROPIC_API_KEY`. The service is reached only through the
  `anthropic` host, not Foundry.
- Choose it, in the shell or your config: `/runtime claude-managed-agents`, or
  `runtime: claude-managed-agents`.
- Make the deployment and its sources the ones you mean. The agent is built from the active
  deployment, what is attached, the persona and skills, the web scope and approval.

## Create the agent

```bash
intel managed sync --dry-run      # print the definition; no request, no key needed
intel managed sync
```

The definition carries the model, a system prompt built as every runtime's is, one custom tool
per attached tool that approval allows, and of Anthropic's own toolset only the web tools the
scope turns on: no bash, no files. `sync` creates the agent, or a new version of it when the
definition changed, and one cloud environment beside it, which may reach only the scope's
`allowed_domains`. Run again with nothing changed, it makes no request.

The ids and version are kept in your config, in the overlay for this deployment, never in the
deployment's file. `sync` refuses when a source did not attach, since the agent would be created
without it; `--dry-run` still prints the definition.

## Ask

```bash
intel ask "What do my notes say causes context collapse?"
```

Under `max_cost_usd` ([options](../runtimes.md#options)) a session has that budget; a question
that reaches it keeps its answer so far. Follow-ups in the shell continue the session, sharing
its budget. After a stop or a failure, the next question starts a new session with a fresh
budget, and is sent alone, without the earlier conversation.

## When it is stale

Attach or drop a source, or change the persona, skills, web scope or approval, and the next
question fails with `managed_stale` until you `sync` again. To keep running an older version as
it is, pin it:

```yaml
runtimes:
  claude-managed-agents:
    agent_version: 3
```

An eval variant can pin a version the same way, in its `options`.

## Cost and data

A session is billed by the service for its active time, plus tokens and web searches, at
[Anthropic's prices](https://docs.anthropic.com/en/docs/about-claude/pricing). A completed
answer's `cost_usd` is this question's share of the session's cost at list price, not a
contracted one.

**The service keeps the session's history, tool output included, until the session is
deleted.** Tool output carries your sources' content, so treat a session like the sources it read.

## Known limits

- **`max_tool_rounds` is refused:** the service runs the loop. Bound a question with
  `max_cost_usd` instead.
- **`max_uses` in a web scope is refused;** domain lists are honoured.
- **It needs the asyncio event loop,** which the CLI and shell use; another loop is refused.
- **The service cites nothing itself.** An answer's `citations` are the `source:key` refs it
  quotes; the pages the web tools read are refs on their tool results.
