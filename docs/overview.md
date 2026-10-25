# latent-intel

## What it is for

Search and ask across all your context, with answers that cite their sources. Knowledge rarely
sits in one place: notes in one folder, papers in another, a codebase, a wiki, a service with its
own API. latent-intel attaches all of them at once, searches them side by side, and sends your
questions to the model or agent runtime you choose, which reads them through tools and cites what
it used. It is also a workbench for context engineering: an agent's persona, skills and
subagents are plain files you can change and compare.

## What goes in

- **Sources.** A folder of Markdown, a codebase, or any MCP server, on your machine or remote. A
  wiki or a records store is a source through its own server.
- **A runtime.** Who answers: a loop of this package's own, a vendor's agent SDK, Claude Code on
  your machine, or a service that runs the agent for you. Most reach a model through a host:
  Anthropic, Azure AI Foundry, Azure OpenAI, OpenAI, OpenRouter, or a server on your machine.
- **A deployment,** optionally: one YAML file and the directory around it, naming a set of
  sources together with a persona, skills, subagents, saved commands, branding and defaults. Its
  owner keeps it, and this package only reads it.

## What comes out

- **Search results,** one list per source, never merged: a lexical count and a vector score are
  not comparable. Every result has a ref, `source:key`, and says where it came from.
- **Documents,** by ref, in full.
- **Answers** that cite the refs they used, and say when the sources do not answer.
- **An event stream** behind all of it: what the terminal shows is a rendering of events, which
  can be printed as JSON lines, recorded to a file, replayed, and run in batches for latent-eval
  to score. Recordings and batch runs are published formats.

## How it works

- **Sources attach through connectors,** and say what they can do: search, fetch, tools. A
  source that claims what it cannot do fails when it attaches, not when it is first used.
- **Tools declare their effects.** A tool that writes is offered to the agent only when you allow
  it, and a tool that declares nothing is treated as one that writes.
- **The runtime is chosen apart from the sources.** The same sources, tools and citations work on
  every runtime; changing one is a line of config.
- **The open web is off** unless a deployment, your config or the session turns it on, and a
  runtime that cannot honour the scope refuses the question rather than ignoring it.

## How it is used

- **In the shell,** `intel`: search, read and ask from one prompt, with slash commands for
  everything else.
- **On the command line,** one command at a time, for scripts and one-offs.
- **As a Python library,** through a `Session` and its event stream.
- **Through a deployment,** when a set of sources and an agent's shape repeat for someone.

## What it costs

Search and reading cost nothing: they run here, without a model. A question costs whatever the
runtime's host charges, or uses your Claude Code plan under `claude-cli`. A server on your own
machine costs nothing.

## Where it fits

It is a client: it owns how sources are put together, not what is in them. It parses no wiki and
embeds nothing; latent-wiki and latent-records answer for their own stores through
`latent-wiki serve` and `latent-records serve`, which it attaches like any MCP server.
latent-eval scores its recordings. None of them is imported here.
