"""Turning mail into plain text that is safe to hand to a model.

Mail is written by strangers. Two things a person reading a message never sees
can still reach a model that is given the raw text:

- **Invisible characters.** Zero-width and bidirectional controls, Unicode "tag"
  characters, fillers and variation selectors can carry a whole hidden sentence
  inside what looks like an empty line or a normal word.
- **Hidden HTML.** Text in an element styled so that no mail client shows it
  (``display:none``, zero size, moved off the page, and the like), comments,
  scripts and styles.

Everything this server returns from a message goes through :func:`clean` or
:func:`clean_line`, and an HTML body through :func:`html_to_text`. Nothing here
fetches anything: links and images are reduced to text, never followed.

The HTML side is best effort and says so (SECURITY.md lists what it does not
catch): it follows the element structure and each element's own ``style``
attribute, not style sheets and not colours against backgrounds. It is also
written to take time in proportion to the size of the message whatever the
markup, because the sender chooses the markup.
"""

import re
from html.parser import HTMLParser

# Characters that take no space on screen, as inclusive code point ranges. Kept
# as numbers, never as the characters themselves, so that this file can be read.
# tests/test_textclean.py checks the list against Python's own Unicode tables.
_INVISIBLE_RANGES = (
    (0x0000, 0x0008),  # C0 controls; tab (9) and newline (10) are kept
    (0x000B, 0x000C),
    (0x000E, 0x001F),
    (0x007F, 0x009F),  # delete and the C1 controls
    (0x00AD, 0x00AD),  # soft hyphen
    (0x034F, 0x034F),  # combining grapheme joiner
    (0x0600, 0x0605),  # Arabic number signs (format characters)
    (0x061C, 0x061C),  # Arabic letter mark
    (0x06DD, 0x06DD),
    (0x070F, 0x070F),
    (0x0890, 0x0891),
    (0x08E2, 0x08E2),
    (0x115F, 0x1160),  # Hangul fillers
    (0x17B4, 0x17B5),  # Khmer inherent vowels, invisible
    (0x180B, 0x180F),  # Mongolian variation selectors and vowel separator
    (0x200B, 0x200F),  # zero-width space, joiners, direction marks
    (0x202A, 0x202E),  # direction embeddings and overrides
    (0x2060, 0x206F),  # word joiner, invisible operators, isolates
    (0x2800, 0x2800),  # braille blank
    (0x3164, 0x3164),  # Hangul filler
    (0xD800, 0xDFFF),  # lone surrogates: bytes that never were text
    (0xE000, 0xF8FF),  # private use
    (0xFDD0, 0xFDEF),  # noncharacters
    (0xFE00, 0xFE0F),  # variation selectors
    (0xFEFF, 0xFEFF),  # byte-order mark
    (0xFFA0, 0xFFA0),  # halfwidth Hangul filler
    (0xFFF0, 0xFFFB),  # specials and annotation controls
    (0xFFFE, 0xFFFF),  # noncharacters
    (0x110BD, 0x110BD),
    (0x110CD, 0x110CD),
    (0x13430, 0x1345F),  # Egyptian hieroglyph format controls
    (0x1BCA0, 0x1BCAF),  # shorthand format controls
    (0x1D159, 0x1D159),  # musical null notehead
    (0x1D173, 0x1D17A),  # musical format controls
    (0xE0000, 0xE0FFF),  # tags and the variation selectors supplement
    (0xF0000, 0x10FFFF),  # private-use planes
)
# The last two code points of every plane are noncharacters too.
_PLANE_ENDS = tuple((plane << 16 | 0xFFFE, plane << 16 | 0xFFFF) for plane in range(1, 15))
# Characters that are a space or a line break under another name.
_LINE_BREAK_POINTS = (0x0085, 0x2028, 0x2029)
_SPACE_POINTS = (0x00A0, 0x1680, *range(0x2000, 0x200B), 0x202F, 0x205F, 0x3000)


def _char_class(ranges: tuple[tuple[int, int], ...]) -> str:
    return "".join(
        re.escape(chr(low)) if low == high else f"{re.escape(chr(low))}-{re.escape(chr(high))}"
        for low, high in ranges
    )


_INVISIBLE = re.compile("[" + _char_class(_INVISIBLE_RANGES + _PLANE_ENDS) + "]")
_LINE_BREAKS = re.compile("\r\n|\r|[" + "".join(chr(point) for point in _LINE_BREAK_POINTS) + "]")
_SPACES = re.compile(r"[ \t\f\v" + "".join(chr(point) for point in _SPACE_POINTS) + "]+")
_BLANK_LINES = re.compile(r"\n{3,}")
_ANY_SPACE = re.compile(r"\s+")
ELLIPSIS = "…"


def clean(text: str) -> str:
    """``text`` without invisible characters, with plain ``\\n`` line breaks."""
    return _INVISIBLE.sub("", _LINE_BREAKS.sub("\n", text))


def clean_line(text: str, limit: int) -> str:
    """``text`` as one clean line of at most ``limit`` characters (a subject, a name)."""
    line = _ANY_SPACE.sub(" ", clean(text)).strip()
    if len(line) > limit:
        return line[: max(limit - 1, 0)].rstrip() + ELLIPSIS
    return line


def tidy(text: str) -> str:
    """Clean a body: no invisible characters, no trailing spaces, no runs of blank lines."""
    lines = [_SPACES.sub(" ", line).strip() for line in clean(text).split("\n")]
    return _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


# ── styles ────────────────────────────────────────────────────────────────────────────────────────

_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
# A CSS escape: a backslash and up to six hexadecimal digits (then one optional
# space), or a backslash and any other character.
_CSS_ESCAPE = re.compile(r"\\(?:([0-9a-fA-F]{1,6})[ \t\n]?|(.))", re.DOTALL)
_NUMBER = re.compile(r"^(-?(?:\d+\.?\d*|\.\d+))\s*([a-z%]*)")
# How far off the page text has to be pushed, or indented, to count as removed.
_FAR = {"px": 500.0, "pt": 400.0, "em": 40.0, "rem": 40.0, "ex": 80.0, "ch": 80.0, "%": 200.0}
# Below this a font can't be read. A 1em font is the normal size, so only the
# absolute units have a threshold above zero.
_TINY_FONT = {"px": 2.0, "pt": 1.5}
_TINY_BOX = {"px": 1.0, "pt": 1.0}
_EDGES = ("left", "top", "right", "bottom", "margin-left", "margin-top")
_BOX = ("height", "max-height", "width", "max-width")
MAX_STYLE_CHARS = 4000


def _unescape(match: re.Match[str]) -> str:
    if match.group(1):
        point = int(match.group(1), 16)
        return chr(point) if 0 < point <= 0x10FFFF and not 0xD800 <= point <= 0xDFFF else ""
    return match.group(2)


def _declarations(style: str) -> dict[str, str]:
    """A ``style`` attribute as property and value, the way a browser reads it.

    Comments and escapes are resolved first (``d\\69splay`` is ``display``),
    names and values are lowercased, ``!important`` is dropped, and a property
    given twice keeps its last value.
    """
    text = _CSS_ESCAPE.sub(_unescape, _CSS_COMMENT.sub("", style[:MAX_STYLE_CHARS]))
    found: dict[str, str] = {}
    for declaration in text.split(";"):
        name, colon, value = declaration.partition(":")
        if colon:
            value = _ANY_SPACE.sub(" ", value.lower().replace("!important", "")).strip()
            found[_ANY_SPACE.sub("", name).lower()] = value
    return found


def _length(value: str) -> tuple[float, str] | None:
    match = _NUMBER.match(value)
    if match is None:
        return None
    return float(match.group(1)), match.group(2)


def _at_most(value: str, limits: dict[str, float]) -> bool:
    """True when ``value`` is zero, or a length no larger than the limit for its unit."""
    length = _length(value)
    if length is None:
        return False
    number, unit = length
    return number == 0 or (unit in limits and number <= limits[unit])


def _far_negative(value: str) -> bool:
    length = _length(value)
    return length is not None and length[1] in _FAR and length[0] <= -_FAR[length[1]]


def _style_hides(style: str) -> bool:
    """True when an element with this ``style`` is one no mail client shows."""
    css = _declarations(style)
    if css.get("display") == "none" or css.get("visibility") in ("hidden", "collapse"):
        return True
    if css.get("mso-hide") == "all":
        return True
    opacity = _length(css.get("opacity", ""))
    if opacity is not None and opacity[0] <= (5.0 if opacity[1] == "%" else 0.05):
        return True
    if "font-size" in css and _at_most(css["font-size"], _TINY_FONT):
        return True
    font = css.get("font", "")
    if font[:1] == "0" and (len(font) == 1 or not (font[1].isdigit() or font[1] == ".")):
        return True  # the shorthand: `font: 0/0 a`
    colour = css.get("color", "").replace(" ", "")
    if colour == "transparent" or re.fullmatch(r"(?:rgba|hsla)\(.*,0(?:\.0+)?%?\)", colour):
        return True
    if re.fullmatch(r"#(?:[0-9a-f]{3}0|[0-9a-f]{6}00)", colour):
        return True
    # A box of no size shows nothing only when what overflows it is cut off.
    clipped = any(
        css.get(name, "").split(" ")[0] in ("hidden", "clip")
        for name in ("overflow", "overflow-x", "overflow-y")
    )
    if clipped and any(name in css and _at_most(css[name], _TINY_BOX) for name in _BOX):
        return True
    if _far_negative(css.get("text-indent", "")):
        return True
    indent = _length(css.get("text-indent", ""))
    if clipped and indent is not None and indent[1] == "%" and indent[0] >= 100:
        return True  # pushed out of a box that cuts off what leaves it
    if css.get("content-visibility") == "hidden":
        return True
    if re.search(r"opacity\(0(?:\.0+)?%?\)", css.get("filter", "").replace(" ", "")):
        return True
    if css.get("position") in ("absolute", "fixed") and any(
        _far_negative(css.get(edge, "")) for edge in _EDGES
    ):
        return True
    clip = css.get("clip", "").replace(" ", "").replace(",", "")
    if re.fullmatch(r"rect\((?:[01](?:px)?){4}\)", clip):
        return True
    clip_path = css.get("clip-path", "").replace(" ", "")
    if clip_path.startswith(("inset(100%", "inset(50%", "circle(0")):
        return True
    return bool(re.search(r"scale[xy]?\(0(?:\.0+)?[,)]", css.get("transform", "").replace(" ", "")))


def _is_hidden(attrs: list[tuple[str, str | None]]) -> bool:
    for name, value in attrs:
        if name == "hidden":
            return True
        if name == "style" and value and _style_hides(value):
            return True
    return False


# ── HTML ──────────────────────────────────────────────────────────────────────────────────────────

# Elements whose content is never text for a reader.
_SKIPPED = frozenset(
    {
        "audio", "canvas", "datalist", "embed", "head", "iframe", "math", "noembed", "noframes",
        "object", "rp", "script", "style", "svg", "template", "title", "video",
    }
)  # fmt: skip
# Shown only when they carry the `open` attribute.
_CLOSED_UNLESS_OPEN = frozenset({"details", "dialog"})
# Elements with no end tag: they open nothing, so they are never pushed.
_VOID = frozenset(
    {
        "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
        "source", "track", "wbr",
    }
)  # fmt: skip
_BLOCK = frozenset(
    {
        "address", "article", "aside", "blockquote", "center", "dd", "div", "dl", "dt",
        "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header",
        "hr", "li", "main", "nav", "ol", "p", "pre", "section", "table", "tbody", "tfoot",
        "thead", "tr", "ul",
    }
)  # fmt: skip
_CELL = frozenset({"td", "th"})
# Elements an end tag can't reach across (HTML's "scope"): `</div>` inside a table
# cell does not close a div outside the table. A browser ignores such an end tag,
# so text after it is still inside whatever the outer element hides.
_BOUNDARY = frozenset({"table", "td", "th", "caption", "marquee", "object", "select", "template"})
# What may stand in <head>. Anything else ends the head, whose end tag may be left out.
_HEAD_CONTENT = frozenset(
    {"base", "link", "meta", "noscript", "script", "style", "template", "title"}
)
# Start tags that end an open element of the same kind without an end tag.
_ENDS = {
    "li": ("li",),
    "dt": ("dt", "dd"),
    "dd": ("dt", "dd"),
    "tr": ("tr",),
    "tbody": ("tr", "tbody", "thead", "tfoot"),
    "thead": ("tr", "tbody", "thead", "tfoot"),
    "tfoot": ("tr", "tbody", "thead", "tfoot"),
    "option": ("option",),
    "optgroup": ("option", "optgroup"),
}
_ROW_PARTS = frozenset({"td", "th", "tr", "tbody", "thead", "tfoot"})
_LINK_SCHEMES = ("http://", "https://", "mailto:")
# A link longer than this is almost always a tracking redirect; its text is kept.
MAX_LINK_CHARS = 300


class _TextExtractor(HTMLParser):
    """Collects what a reader of the rendered HTML would see, as plain text.

    It keeps the open elements the way a browser does where that decides what is
    hidden: end tags that may be left out, end tags that reach nothing, and the
    table boundary. Every start tag is pushed once and popped once, and every
    look-up is by count, so the work is linear in the size of the markup.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        # Open elements, innermost last: the tag, and what it adds to the count of
        # suppressing elements around the text: 1 when it hides its content, 0, or
        # -1 for the <summary> of a closed <details>, the one part of it that shows.
        self._open: list[tuple[str, int]] = []
        # One frame per open boundary element (and one for the document): how many
        # of each tag are open directly inside it. An end tag looks only here.
        self._frames: list[dict[str, int]] = [{}]
        # The open boundary elements, innermost last, and how many of each there are.
        self._owner_stack: list[str] = []
        self._owners: dict[str, int] = {}
        self._suppressed = 0
        self._pre = 0
        # The one open link: its href, where in _out its text starts, and the depth
        # of its element in _open.
        self._link: tuple[str, int, int] | None = None

    # ── the stack ────────────────────────────────────────────────────────────────────────────────

    def _push(self, tag: str, suppress: int) -> None:
        self._open.append((tag, suppress))
        frame = self._frames[-1]
        frame[tag] = frame.get(tag, 0) + 1
        if tag in _BOUNDARY:
            self._frames.append({})
            self._owner_stack.append(tag)
            self._owners[tag] = self._owners.get(tag, 0) + 1
        self._suppressed += suppress

    def _pop(self) -> str:
        tag, suppress = self._open.pop()
        if tag in _BOUNDARY:
            self._frames.pop()
            self._owner_stack.pop()
            self._owners[tag] -= 1
        self._frames[-1][tag] -= 1
        self._suppressed -= suppress
        if not suppress and not self._suppressed:
            self._closed(tag)
        if self._link is not None and self._link[2] == len(self._open):
            self._link = None
        return tag

    def _pop_through(self, tag: str) -> None:
        while self._open and self._pop() != tag:
            pass

    def _in_frame(self, tag: str) -> bool:
        return self._frames[-1].get(tag, 0) > 0

    # ── tags ─────────────────────────────────────────────────────────────────────────────────────

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _VOID:
            self._void(tag, attrs)
            return
        self._imply_ends(tag)
        self._push(tag, self._suppression(tag, attrs))
        if self._suppressed:
            return
        if tag == "pre":
            self._pre += 1
        if tag == "li":
            self._out.append("\n- ")
        elif tag in _BLOCK:
            self._out.append("\n")
        elif tag == "a":
            href = next((value for name, value in attrs if name == "href" and value), "")
            self._link = (href.strip(), len(self._out), len(self._open) - 1)

    def _suppression(self, tag: str, attrs: list[tuple[str, str | None]]) -> int:
        """What ``tag`` adds to the count of elements hiding the text inside them."""
        if tag in _SKIPPED or _is_hidden(attrs):
            return 1
        if tag in _CLOSED_UNLESS_OPEN and not any(name == "open" for name, _ in attrs):
            return 1
        if tag == "summary" and self._open and self._open[-1] == ("details", 1):
            return -1 if self._suppressed == 1 else 0  # shown unless something outside hides it
        return 0

    def _imply_ends(self, tag: str) -> None:
        """Close what ``tag`` closes by starting, as a browser does without an end tag."""
        if self._in_frame("head") and tag not in _HEAD_CONTENT:
            self._pop_through("head")
        if tag in _ROW_PARTS and self._owner_stack and self._owner_stack[-1] in _CELL:
            self._pop_through(self._owner_stack[-1])  # a new cell or row ends the open cell
        for name in _ENDS.get(tag, ()):
            if self._in_frame(name):
                self._pop_through(name)
        if (tag in _BLOCK or tag in _CELL) and self._in_frame("p"):
            self._pop_through("p")
        if tag == "a" and self._in_frame("a"):
            self._pop_through("a")  # links don't nest

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # `<div/>` is a start tag: only void elements close themselves, and a
        # browser ignores the slash on the rest.
        self.handle_starttag(tag, attrs)

    def _void(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._in_frame("head") and tag not in _HEAD_CONTENT:
            self._pop_through("head")
        if self._suppressed or _is_hidden(attrs):
            return
        if tag in ("br", "hr"):
            self._out.append("\n")
        elif tag == "img":
            alt = next((value for name, value in attrs if name == "alt" and value), "")
            pixel = any(
                name in ("width", "height") and (value or "").strip() in ("0", "1")
                for name, value in attrs
            )
            if alt.strip() and not pixel:
                self._out.append(f"[image: {alt.strip()}]")

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID:
            return
        if self._in_frame(tag):
            # Unclosed elements inside it are closed with it.
            self._pop_through(tag)
        elif tag in _BOUNDARY and self._owners.get(tag, 0) > 0:
            self._pop_through(tag)
        # Otherwise it matches nothing it can reach, and is ignored: an element
        # outside the table (or select, or marquee) being read stays open.

    def _closed(self, tag: str) -> None:
        """What closing a visible element adds to the text."""
        if tag == "pre":
            self._pre = max(self._pre - 1, 0)
        if tag == "a":
            if self._link is not None and self._link[2] == len(self._open):
                href, start, _ = self._link
                text = "".join(self._out[start:]).strip()
                shown = href.removeprefix("mailto:")
                if (
                    text
                    and href.lower().startswith(_LINK_SCHEMES)
                    and len(href) <= MAX_LINK_CHARS
                    and shown not in text
                ):
                    self._out.append(f" <{href}>")
        elif tag in _CELL:
            self._out.append(" ")
        elif tag in _BLOCK:
            self._out.append("\n")

    def handle_data(self, data: str) -> None:
        if self._open and self._open[-1][0] == "head" and data.strip():
            self._pop()  # text directly in the head ends it, as a start tag does
        if self._suppressed:
            return
        self._out.append(data if self._pre else _ANY_SPACE.sub(" ", data))

    def text(self) -> str:
        return "".join(self._out)


UNREADABLE_HTML = "[This message's HTML could not be turned into text.]"


def html_to_text(html: str) -> str:
    """The text a reader of ``html`` would see: no markup, no hidden text.

    Links keep their text, followed by the address in angle brackets when it is
    an http(s) or mailto link, short enough to be worth reading, and not already
    the text. Images become ``[image: alt text]`` when they have alt text.
    Nothing is fetched.

    Markup the parser can't get through yields a fixed sentence, not the raw
    text: stripping tags by hand would let hidden text out.
    """
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - hostile markup must not fail a read, nor leak
        return UNREADABLE_HTML
    return tidy(parser.text())
