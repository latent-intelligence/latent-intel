# Format fixtures

- `recording/` — this package's own published format (`schemas/recording.v1.json`).
- `eval-run/` — this package's own published format (`schemas/eval-run.v1.json`): a run's
  `manifest.json` and its recordings, written by `batch` itself.
- `wiki-store/`, `context-store/` — **copied** from latent-wiki's `tests/fixtures/formats/`
  (`latent-wiki.store` v1, `latent-wiki.context` v1), never imported. When the producer
  bumps a format version, copy the new samples here. latent-intel reads neither format itself: the one
  consumer is `tests/test_wiki_over_mcp.py`, which serves them with `latent-wiki serve`.
