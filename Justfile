# latent-intel — task runner.
#
# `just` with no argument lists the recipes.

default:
    @just --list

# -- environment ------------------------------------------------------------

# Install the package with its development dependencies.
install:
    uv sync --extra dev --extra api

# -- checks -----------------------------------------------------------------

# Everything CI would run.
check: lint types contracts test

# Run the test suite.
test:
    uv run pytest -q

# Lint the package source.
lint:
    uv run ruff check src/ tests/

# Format the package source.
format:
    uv run ruff format src/ tests/

# Type-check the package.
types:
    uv run mypy src/

# Enforce the frontend import boundary.
#
# This is the contract that keeps a web client an addition rather than a rewrite: a
# frontend may reach Session, the event models and the renderer, and nothing else. It is
# checked rather than documented because the violation that breaks it — one convenient
# import of a connector — passes every other test in the suite.
contracts:
    uv run lint-imports

# -- running ----------------------------------------------------------------

# The interactive shell.
shell:
    uv run intel

# What is reachable, and which runtimes are configured.
doctor:
    uv run intel doctor
