"""Removing the offending item from a page, instead of judging the page.

A shop's navigation names its whole catalogue on every page. An Amazon
search for socks lists Lingerie, Bras, Panties and Swimwear in the
sidebar, so scoring the page as a whole gives two bad answers and no good
one: judge it strictly and Amazon is blocked outright, judge it loosely
and the sidebar stays on screen.

The right answer is neither. Take out the sidebar links and leave the rest
of the page alone — then the child never sees the words, and what remains
is a page about socks, which can be judged strictly like anything else.

Written on `html.parser` from the standard library rather than a real DOM.
That is a deliberate limit: this can drop an element whose text matches,
and it can leave everything else byte-identical, and it does not try to do
anything cleverer. A filter that rewrites markup it does not understand
breaks sites in ways nobody can debug.
"""

from __future__ import annotations

import logging
from html.parser import HTMLParser

log = logging.getLogger(__name__)

# Elements worth considering. Anything else is either too big to remove
# safely (a whole section) or too small to be a navigation item.
DEFAULT_TAGS = ("a", "li", "option")

# Tags a browser closes for you when the next one opens. Without this an
# unclosed <li> list — which is most of them — buffers until the size cap
# and nothing is ever removed from it.
IMPLICITLY_CLOSED = frozenset({"li", "option", "p", "td", "tr", "dt", "dd"})

# Tags that never have a closing tag, so they can never open a candidate.
VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
})

# If a candidate element runs longer than this it is not a navigation
# item, it is the page. Flush it and move on rather than buffering a
# megabyte to decide.
MAX_ELEMENT = 8000
# Above this a page is not worth reparsing: the cost lands on the family's
# own laptop, per page.
MAX_PAGE = 2_000_000


class _Stripper(HTMLParser):
    """Rebuild the document, dropping candidate elements whose text matches.

    Buffers a candidate element until its closing tag, then decides. Only
    the outermost candidate is buffered — a nested one goes with its
    parent or stays with it, which is what a reader would expect.
    """

    def __init__(self, is_blocked, tags):
        # convert_charrefs=False so entities pass through untouched: this
        # must be able to leave what it does not remove byte-identical.
        super().__init__(convert_charrefs=False)
        self.is_blocked = is_blocked
        self.tags = tuple(tags)
        self.out: list[str] = []
        self.removed = 0
        self._buffer: list[str] | None = None
        self._text: list[str] = []
        self._tag = ""
        self._depth = 0

    # -- output plumbing ------------------------------------------------------

    def _emit(self, raw: str) -> None:
        if self._buffer is None:
            self.out.append(raw)
            return
        self._buffer.append(raw)
        if sum(len(part) for part in self._buffer) > MAX_ELEMENT:
            self._flush()  # too big to be a navigation item

    def _flush(self) -> None:
        if self._buffer is not None:
            self.out.extend(self._buffer)
        self._buffer = None
        self._text = []
        self._tag = ""
        self._depth = 0

    def _finish(self) -> None:
        text = " ".join(self._text).strip()
        if text and self.is_blocked(text):
            self.removed += 1
            self._buffer = None
            self._text = []
            self._tag = ""
            self._depth = 0
            return
        self._flush()

    # -- parser hooks ---------------------------------------------------------

    def handle_starttag(self, tag, attrs):
        raw = self.get_starttag_text() or f"<{tag}>"
        if self._buffer is None and tag in self.tags:
            self._buffer = [raw]
            self._tag = tag
            self._depth = 0
            # An address can carry the department even when the link text
            # does not: /b/womens-lingerie labelled "Shop now".
            for name, value in attrs:
                if name in ("href", "title", "aria-label", "alt") and value:
                    self._text.append(value.replace("/", " ").replace("-", " "))
            return
        if self._buffer is not None and tag == self._tag and tag not in VOID_TAGS:
            if tag in IMPLICITLY_CLOSED and not self._depth:
                # <li>one<li>two — the browser closed the first for us.
                self._finish()
                self._buffer = [raw]
                self._tag = tag
                self._depth = 0
                for name, value in attrs:
                    if name in ("href", "title", "aria-label", "alt") and value:
                        self._text.append(
                            value.replace("/", " ").replace("-", " "))
                return
            self._depth += 1
        self._emit(raw)

    def handle_startendtag(self, tag, attrs):
        self._emit(self.get_starttag_text() or f"<{tag}/>")

    def handle_endtag(self, tag):
        raw = f"</{tag}>"
        if self._buffer is not None and tag == self._tag:
            if self._depth:
                self._depth -= 1
                self._buffer.append(raw)
                return
            self._buffer.append(raw)
            self._finish()
            return
        self._emit(raw)

    def handle_data(self, data):
        if self._buffer is not None:
            self._text.append(data)
        self._emit(data)

    def handle_entityref(self, name):
        self._emit(f"&{name};")

    def handle_charref(self, name):
        self._emit(f"&#{name};")

    def handle_comment(self, data):
        self._emit(f"<!--{data}-->")

    def handle_decl(self, decl):
        self._emit(f"<!{decl}>")

    def handle_pi(self, data):
        self._emit(f"<?{data}>")

    def unknown_decl(self, data):
        self._emit(f"<![{data}]>")


def strip(html: str, is_blocked, tags=DEFAULT_TAGS,
          quick_reject=None) -> tuple[str, int]:
    """Return the page without its blocked items, and how many went.

    `quick_reject` is a compiled pattern that must appear somewhere in the
    page before it is worth reparsing at all. Reparsing a megabyte of HTML
    costs a fifth of a second on the machine this has to run on, and the
    overwhelming majority of pages contain none of these words — so the
    scan that skips them is what makes this affordable.

    On any parse trouble the original is returned untouched: a page we
    could not rewrite cleanly is a page we must not rewrite at all.
    """
    if not html or len(html) > MAX_PAGE:
        return html, 0
    if quick_reject is not None and not quick_reject.search(html.lower()):
        return html, 0
    parser = _Stripper(is_blocked, tags)
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - a broken page is not ours to mangle
        log.debug("could not strip elements", exc_info=True)
        return html, 0
    parser._flush()  # an element whose closing tag never came
    if not parser.removed:
        return html, 0
    return "".join(parser.out), parser.removed
