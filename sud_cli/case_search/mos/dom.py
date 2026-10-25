"""Minimal stdlib-only DOM tree for replay parsing.

Built on `html.parser.HTMLParser`. Performs NO network I/O and is used
exclusively for saved fixture replay. Tolerates the real-world markup of the
mos-gorsud card page (void elements, stray end tags, inline SVG, `<br>`,
`noindex`) the same way a browser-ish stack would: unknown containers nest,
stray closers are ignored.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
})

TEXT_SKIP_TAGS = frozenset({"script", "style", "noscript", "svg", "i",
                            "template"})
TEXT_SKIP_CLASSES = frozenset({"hint", "copy__copy", "copy__copied"})


class Text:
    __slots__ = ("text",)

    def __init__(self, text: str):
        self.text = text


class Node:
    __slots__ = ("tag", "attrs", "children")

    def __init__(self, tag: str, attrs: dict[str, str] | None = None):
        self.tag = tag
        self.attrs = dict(attrs or {})
        self.children: list[Node | Text] = []

    def get(self, attr: str, default: str = "") -> str:
        value = self.attrs.get(attr)
        return default if value is None else value

    @property
    def classes(self) -> frozenset[str]:
        return frozenset(self.get("class", "").lower().split())


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#document")
        self._stack: list[Node] = [self.root]

    def _append(self, node: Node) -> None:
        self._stack[-1].children.append(node)

    def handle_starttag(self, tag: str, attrs) -> None:
        node = Node(tag, attrs)
        self._append(node)
        if tag not in VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs) -> None:
        self._append(Node(tag, attrs))

    def handle_endtag(self, tag: str) -> None:
        for idx in range(len(self._stack) - 1, 0, -1):
            if self._stack[idx].tag == tag:
                del self._stack[idx:]
                return

    def handle_data(self, data: str) -> None:
        self._stack[-1].children.append(Text(data))


def collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def build_tree(html: str) -> Node:
    builder = _TreeBuilder()
    builder.feed(html)
    builder.close()
    return builder.root


def iter_nodes(node: Node):
    for child in node.children:
        if isinstance(child, Node):
            yield child
            yield from iter_nodes(child)


def _collect_text(node: Node, parts: list[str]) -> None:
    if node.tag in TEXT_SKIP_TAGS:
        return
    if TEXT_SKIP_CLASSES & node.classes:
        return
    for child in node.children:
        if isinstance(child, Text):
            parts.append(child.text)
        else:
            _collect_text(child, parts)


def text_of(node: Node) -> str:
    """Whitespace-collapsed visible text of a subtree (scripts/icons
    excluded)."""
    parts: list[str] = []
    _collect_text(node, parts)
    return collapse(" ".join(parts))


def find_all(root: Node, tag: str | None = None,
             css_class: str | None = None) -> list[Node]:
    out: list[Node] = []
    for node in iter_nodes(root):
        if tag is not None and node.tag != tag:
            continue
        if css_class is not None and css_class not in node.classes:
            continue
        out.append(node)
    return out


def find_first(root: Node, tag: str | None = None,
               css_class: str | None = None) -> Node | None:
    found = find_all(root, tag, css_class)
    return found[0] if found else None


def anchors(node: Node) -> list[tuple[str, str]]:
    """Return (anchor_text, href) pairs in document order."""
    return [(text_of(a), a.get("href")) for a in iter_nodes(node)
            if a.tag == "a" and a.get("href")]


__all__ = ["Node", "Text", "build_tree", "iter_nodes", "find_all",
           "find_first", "text_of", "anchors", "collapse"]
