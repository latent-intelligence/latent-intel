# Format fixtures

- `recording/` — this package's own published format (`schemas/recording.v1.json`).
- `wiki-store/`, `context-store/` — **copied** from latent-wiki's `tests/fixtures/formats/`
  (`latent-wiki.store` v1, `latent-wiki.context` v1), never imported
  (house-rules/integration.md, "Fixtures, not imports"). When the producer bumps a format
  version, copy the new samples here. latent-intel reads neither format itself: the one
  consumer is `tests/test_wiki_over_mcp.py`, which serves them with `lw serve`.
