"""Full-text ranking over a wiki's gist fields, in memory, with the standard library.

SQLite's FTS5 ships inside CPython's `sqlite3` almost everywhere, so ranking needs no
dependency and no network — an air-gapped machine ranks exactly as a connected one.
The index is built when the store is loaded, from what the manifest already carries,
and lives as long as the connector: nothing is written to disk and nothing goes stale.

**What it indexes is the gist, not the body,** unless the caller opts in. The one
ablation on a store of this shape found ranking over entry gists carried the benefit,
and a remote store has no bodies to index without one fetch per page.

**Weights say where a match means most.** A query term in a page's key or title says
more about what the page *is* than the same term in its lead, so BM25 is weighted per
column rather than over one concatenated string.

**Queries cannot break it.** Every term is quoted, so a stray `-`, `:` or `"` in what a
model typed is text, not FTS syntax. All terms are required first; when that finds
nothing, any term will do — a long natural-language question rarely has every word on
one page.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

#: Indexed fields, in order, with their BM25 weights.
COLUMNS: tuple[tuple[str, float], ...] = (
    ("key", 10.0),
    ("title", 8.0),
    ("aliases", 6.0),
    ("tags", 3.0),
    ("why", 2.0),
    ("lead", 1.0),
    ("body", 0.5),
)

_TERM = re.compile(r"[\w]+", re.UNICODE)

#: Words that say how a question is asked, not what it is about. Dropped from a query
#: that has other words, so "who decides at the gate" requires "decides" and "gate"
#: rather than "who" and "at". A query of nothing but these keeps them.
STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "do",
        "does",
        "for",
        "from",
        "how",
        "i",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "the",
        "this",
        "to",
        "was",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "with",
    ]
)


def available() -> bool:
    """Whether this interpreter's sqlite has FTS5. Checked, never assumed."""
    try:
        with sqlite3.connect(":memory:") as db:
            db.execute("CREATE VIRTUAL TABLE probe USING fts5(x)")
        return True
    except sqlite3.OperationalError:
        return False


def terms(query: str) -> list[str]:
    """A query's words, lowercased; question words dropped when others remain."""
    words = [t.lower() for t in _TERM.findall(query.replace("_", " "))]
    content = [w for w in words if w not in STOPWORDS]
    return content or words


@dataclass(frozen=True)
class Row:
    key: str
    fields: dict[str, str]


@dataclass(frozen=True)
class Ranked:
    key: str
    score: float
    matched_on: tuple[str, ...]


class Index:
    """An FTS5 table over rows of named fields. One per connector."""

    def __init__(self, rows: list[Row]) -> None:
        self._db = sqlite3.connect(":memory:", check_same_thread=False)
        names = ", ".join(name for name, _ in COLUMNS)
        # Porter stemming over unicode61, so "workflows" finds "workflow" and an accent
        # in a query or a page does not decide whether they meet.
        self._db.execute(
            f"CREATE VIRTUAL TABLE pages USING fts5({names}, "
            "tokenize = 'porter unicode61 remove_diacritics 2')"
        )
        self._fields: dict[str, dict[str, str]] = {}
        for row in rows:
            values = [row.fields.get(name, "") for name, _ in COLUMNS]
            # A key is written with hyphens; indexed with spaces, its words are words.
            values[0] = row.key.replace("-", " ").replace("_", " ")
            self._db.execute(
                f"INSERT INTO pages ({names}) VALUES ({', '.join('?' * len(COLUMNS))})",
                values,
            )
            self._fields[row.key] = {
                name: value.lower()
                for (name, _), value in zip(COLUMNS, values, strict=True)
            }
        self._keys = [row.key for row in rows]

    def rank(self, query: str, allowed: set[str] | None = None) -> list[Ranked]:
        """Every matching row the caller allows, best first.

        `allowed` is the caller's filter, applied before the all-terms / any-term
        decision: if every page holding all the terms is filtered out, the any-term
        query still runs. BM25's statistics are over the whole store either way, so
        the order is the same as ranking only the allowed pages.
        """
        words = terms(query)
        if not words:
            return []
        quoted = [f'"{w}"' for w in words]
        weights = ", ".join(str(weight) for _, weight in COLUMNS)
        out: list[Ranked] = []
        for joiner in (" AND ", " OR "):
            rows = self._db.execute(
                f"SELECT rowid, bm25(pages, {weights}) FROM pages "
                f"WHERE pages MATCH ? ORDER BY bm25(pages, {weights})",
                (joiner.join(quoted),),
            ).fetchall()
            out = [
                Ranked(key, -float(score), self._matched_on(key, words))
                for rowid, score in rows
                if (key := self._keys[int(rowid) - 1]) in (allowed or self._fields)
            ]
            if out:
                break
        return out

    def _matched_on(self, key: str, words: list[str]) -> tuple[str, ...]:
        """Which fields hold a query word. Approximate: a stem may match more."""
        fields = self._fields[key]
        stems = [w[:-1] if len(w) > 4 and w.endswith("s") else w for w in words]
        return tuple(
            name
            for name, _ in COLUMNS
            if fields.get(name) and any(stem in fields[name] for stem in stems)
        )
