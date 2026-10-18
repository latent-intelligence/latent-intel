# Contributing

```bash
just install                          # the venv: every runtime, plus test and lint tooling
just check                            # ruff · mypy --strict · import contracts · pytest
uv tool install ".[all]" --reinstall  # put your build on PATH
```

`just check` passes on a fresh clone with nothing beside it: no sibling package is needed
to build, test or type-check. It also runs the import contracts, so some architectural
rules fail the build rather than review. They are stated once, in [CLAUDE.md](CLAUDE.md).
