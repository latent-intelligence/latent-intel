"""Declared views — a generic structure over a source's frontmatter, named by a project.

The engine knows how to draw a tree; the project says what the tree is made of: which
page type sits at the top, which field points at a parent, which orders siblings, and
which fields ride along. No field name lives here, so a second deployment with a
different vocabulary is a different `views:` entry, not a code change.

A view is a rung beside search, not a replacement for it: it answers "what are the
steps of X, in order, and who does them" in one call, where search answers "which page
is about X".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .project import View

#: Lines one rendered view may carry before it says what it cut.
MAX_LINES = 400


@dataclass
class Node:
    key: str
    title: str
    fields: dict[str, Any] = field(default_factory=dict)
    order: Any = None
    status: str = ""
    children: list[Node] = field(default_factory=list)


@dataclass
class Tree:
    view: View
    roots: list[Node]
    #: Pages naming a parent that is not a page.
    orphans: list[str]
    #: Pages with a parent that no root reaches — under a page of another type, or in a
    #: parent cycle. Reported so a view never silently leaves pages out.
    unreached: list[str] = field(default_factory=list)

    @property
    def empty_roots(self) -> list[str]:
        return [r.key for r in self.roots if not r.children]


def build(view: View, pages: dict[str, tuple[str, dict[str, Any]]]) -> Tree:
    """A tree from `key -> (title, frontmatter)`. Orphans are reported, not dropped."""
    nodes = {
        key: Node(
            key=key,
            title=title,
            fields={f: raw[f] for f in view.fields if raw.get(f) not in (None, "", [])},
            order=raw.get(view.order) if view.order else None,
            status=str(raw.get("status") or ""),
        )
        for key, (title, raw) in pages.items()
    }
    orphans: list[str] = []
    for key, (_, raw) in pages.items():
        parent = raw.get(view.parent)
        if not parent:
            continue
        if str(parent) in nodes:
            nodes[str(parent)].children.append(nodes[key])
        else:
            orphans.append(key)

    roots = [
        nodes[key]
        for key, (_, raw) in pages.items()
        if str(raw.get("type") or "") == view.root_type and not raw.get(view.parent)
    ]
    roots.sort(key=lambda n: n.title)
    reached: set[str] = set()

    def visit(node: Node) -> None:
        if node.key in reached:  # a cycle reached from a root stops here
            return
        reached.add(node.key)
        node.children.sort(key=_sibling_order)
        for child in node.children:
            visit(child)

    for root in roots:
        visit(root)
    unreached = sorted(
        key
        for key, (_, raw) in pages.items()
        if raw.get(view.parent) and key not in reached and key not in orphans
    )
    return Tree(view=view, roots=roots, orphans=sorted(orphans), unreached=unreached)


def _sibling_order(node: Node) -> tuple[int, Any, str]:
    """Integer orders first, in order; then any other order as text; then none."""
    if isinstance(node.order, int) and not isinstance(node.order, bool):
        return (0, node.order, node.title)
    if node.order not in (None, ""):
        return (1, str(node.order), node.title)
    return (2, "", node.title)


def render(tree: Tree, root: str = "", depth: int = 0) -> str:
    """The tree as indented text — one root by key, or all of them — capped."""
    chosen = [r for r in tree.roots if not root or r.key == root]
    if root and not chosen:
        found = _find(tree.roots, root)
        chosen = [found] if found else []
    if root and not chosen:
        return f"no node '{root}' in view '{tree.view.name}'"
    lines: list[str] = []

    seen: set[str] = set()

    def walk(node: Node, level: int) -> None:
        if node.key in seen:
            lines.append(f"{'  ' * level}- … {node.key} (cycle)")
            return
        seen.add(node.key)
        step = f"{node.order}. " if isinstance(node.order, int) else ""
        draft = " · draft" if node.status == "draft" else ""
        lines.append(f"{'  ' * level}- {step}{node.title} ({node.key}){draft}")
        for name, value in node.fields.items():
            shown = ", ".join(map(str, value)) if isinstance(value, list) else value
            lines.append(f"{'  ' * (level + 1)}{name}: {shown}")
        if depth and level + 1 >= depth:
            if node.children:
                lines.append(f"{'  ' * (level + 1)}… {len(node.children)} beneath")
            return
        for child in node.children:
            walk(child, level + 1)

    for node in chosen:
        walk(node, 0)
    cut = ""
    if len(lines) > MAX_LINES:
        cut = f"{MAX_LINES} of {len(lines)} lines; pass root=<key> or depth=<n>"
        lines = lines[:MAX_LINES]
    empty = [r for r in tree.empty_roots if not root]
    tail = [
        f"\n— view {tree.view.name}: {len(chosen)} root(s)",
        f"  named but not broken down: {', '.join(empty)}" if empty else "",
        f"  orphans (parent not a page): {', '.join(tree.orphans)}"
        if tree.orphans
        else "",
        f"  not reached from any root: {', '.join(tree.unreached)}"
        if tree.unreached and not root
        else "",
        f"  cut: {cut or 'nothing'}",
    ]
    return "\n".join(lines) + "\n".join(t for t in tail if t)


def as_dict(tree: Tree) -> dict[str, Any]:
    """The whole tree as data, for a `view://` resource."""

    def one(node: Node) -> dict[str, Any]:
        out: dict[str, Any] = {"key": node.key, "title": node.title, **node.fields}
        if node.order is not None:
            out["order"] = node.order
        if node.status:
            out["status"] = node.status
        if node.children:
            out["children"] = [one(c) for c in node.children]
        return out

    return {
        "view": tree.view.name,
        "roots": [one(r) for r in tree.roots],
        "orphans": tree.orphans,
        "unreached": tree.unreached,
        "empty_roots": tree.empty_roots,
    }


def _find(nodes: list[Node], key: str) -> Node | None:
    for node in nodes:
        if node.key == key:
            return node
        if found := _find(node.children, key):
            return found
    return None
