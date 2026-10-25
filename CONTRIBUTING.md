# Contributing

```bash
just install                          # the venv: every runtime, plus test and lint tooling
just check                            # ruff · mypy --strict · import contracts · pytest
uv tool install ".[all]" --reinstall  # put your build on PATH
```

`just check` passes on a fresh clone with nothing beside it: no sibling package is needed
to build, test or type-check. It also runs the import contracts, so some architectural
rules fail the build rather than review. They are stated once, in [CLAUDE.md](CLAUDE.md).

Tests reach no network and spend nothing: renderers are tested against
`tests/fixtures/streams/*.jsonl`, and a test against a store's server skips when that
store's package is not installed.

A new connector or runtime is a class registered as an entry point, `latent_intel.connectors`
or `latent_intel.runtimes` in `pyproject.toml`, as ours are; start from
`src/latent_intel/connectors/base.py` or `src/latent_intel/agent/base.py`.
