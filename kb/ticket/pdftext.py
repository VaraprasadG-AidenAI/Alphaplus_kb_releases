"""
Text out of a PDF, with nothing but the standard library.

## Why this exists rather than a library

The knowledge base ships as one executable onto a locked-down VDI. Every PDF
library available is either native (PyMuPDF, pdfium) or pulls a native
dependency in (pdfminer.six imports `cryptography`, 8.6 MB of compiled code).
Native binaries are exactly what endpoint protection blocks, and a ticket
reader that cannot start is worse than none. So this reads the format
directly, the same way `kb.parse` reads COBOL: no dependencies, and it says
what it could not do.

## What a Jira PDF actually is

Almost always a browser print of the issue page: Chrome and Edge emit
subsetted fonts with Identity-H encoding, where a "character" in the content
stream is a two-byte glyph id that means nothing without the font's own
`ToUnicode` map. A naive extractor that pulls the text between parentheses
gets glyph ids and prints mojibake - which is precisely how a real ticket was
declared "an image with no text layer" when every word was in fact there.

So the reader below does the three things that matter:

    1. decode the object graph        xref-free scan, object streams, filters
    2. build a per-font code -> text map    ToUnicode, then Encoding, then latin-1
    3. replay the content stream      text matrices, so lines and spaces are real

## What it refuses to do

Encrypted PDFs (no decryption without a crypto library), and pages whose text
is a scanned image. Both are reported as such, per page, because the caller
has to be able to say "page 3 is a picture" rather than silently returning
half a ticket.
"""

from __future__ import annotations

import re
import zlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = ["Page", "Document", "extract", "PdfError"]


class PdfError(Exception):
    """The file is not a PDF we can read at all."""


# ===========================================================================
# 1. Object model and lexer
# ===========================================================================

class Name(str):
    """A PDF name (`/Font`). A str subclass so it can key a dict directly."""
    __slots__ = ()


@dataclass
class Ref:
    """An indirect reference: `12 0 R`."""
    num: int
    gen: int = 0

    def __hash__(self) -> int:
        return hash((self.num, self.gen))


@dataclass
class Stream:
    """A dictionary with bytes attached."""
    dict: Dict[str, Any]
    raw: bytes


_DELIM = b"()<>[]{}/%"
_WHITE = b"\x00\t\n\x0c\r "
_NUMBER_RE = re.compile(rb"[+-]?(?:\d+\.?\d*|\.\d+)$")


class Lexer:
    """Tokens and objects out of a byte string.

    Deliberately forgiving: a malformed object should cost one object, not the
    document. Every caller treats a None as "could not read this".
    """

    def __init__(self, data: bytes, pos: int = 0) -> None:
        self.data = data
        self.pos = pos

    # -- low level ----------------------------------------------------------
    def skip_space(self) -> None:
        data, n = self.data, len(self.data)
        while self.pos < n:
            ch = data[self.pos:self.pos + 1]
            if ch in _WHITE:
                self.pos += 1
            elif ch == b"%":                       # comment to end of line
                while self.pos < n and data[self.pos:self.pos + 1] not in b"\r\n":
                    self.pos += 1
            else:
                return

    def token(self) -> Optional[bytes]:
        """The next raw token, or None at the end."""
        self.skip_space()
        data, n = self.data, len(self.data)
        if self.pos >= n:
            return None
        ch = data[self.pos:self.pos + 1]
        if ch == b"<" and data[self.pos + 1:self.pos + 2] == b"<":
            self.pos += 2
            return b"<<"
        if ch == b">" and data[self.pos + 1:self.pos + 2] == b">":
            self.pos += 2
            return b">>"
        if ch in b"[]{}":
            self.pos += 1
            return ch
        if ch == b"/":
            start = self.pos
            self.pos += 1
            while self.pos < n and data[self.pos:self.pos + 1] not in _DELIM \
                    and data[self.pos:self.pos + 1] not in _WHITE:
                self.pos += 1
            return data[start:self.pos]
        if ch == b"(":
            return self._literal_string()
        if ch == b"<":
            return self._hex_string()
        start = self.pos
        while self.pos < n and data[self.pos:self.pos + 1] not in _DELIM \
                and data[self.pos:self.pos + 1] not in _WHITE:
            self.pos += 1
        if self.pos == start:                      # a lone delimiter
            self.pos += 1
        return data[start:self.pos]

    def _literal_string(self) -> bytes:
        """`(text)` with balanced parentheses and backslash escapes."""
        data, n = self.data, len(self.data)
        self.pos += 1
        out = bytearray()
        depth = 1
        while self.pos < n:
            ch = data[self.pos]
            self.pos += 1
            if ch == 0x5C:                          # backslash
                if self.pos >= n:
                    break
                esc = data[self.pos]
                self.pos += 1
                mapped = {0x6E: 10, 0x72: 13, 0x74: 9, 0x62: 8, 0x66: 12}
                if esc in mapped:
                    out.append(mapped[esc])
                elif 0x30 <= esc <= 0x37:           # octal, up to three digits
                    digits = chr(esc)
                    for _ in range(2):
                        if self.pos < n and 0x30 <= data[self.pos] <= 0x37:
                            digits += chr(data[self.pos])
                            self.pos += 1
                    out.append(int(digits, 8) & 0xFF)
                elif esc in (10, 13):               # line continuation
                    if esc == 13 and self.pos < n and data[self.pos] == 10:
                        self.pos += 1
                else:
                    out.append(esc)
                continue
            if ch == 0x28:
                depth += 1
            elif ch == 0x29:
                depth -= 1
                if depth == 0:
                    break
            out.append(ch)
        return b"(" + bytes(out) + b")"

    def _hex_string(self) -> bytes:
        end = self.data.find(b">", self.pos)
        if end == -1:
            end = len(self.data) - 1
        raw = self.data[self.pos + 1:end]
        self.pos = end + 1
        digits = re.sub(rb"[^0-9A-Fa-f]", b"", raw)
        if len(digits) % 2:
            digits += b"0"
        try:
            return b"(" + bytes.fromhex(digits.decode("ascii")) + b")"
        except ValueError:
            return b"()"

    # -- objects ------------------------------------------------------------
    def object(self) -> Any:
        """One object: number, name, string, array, dictionary or keyword."""
        token = self.token()
        return self._object_from(token)

    def _object_from(self, token: Optional[bytes]) -> Any:
        if token is None:
            return None
        if token == b"<<":
            return self._dictionary()
        if token == b"[":
            return self._array()
        if token.startswith(b"/"):
            return Name(token[1:].decode("latin-1"))
        if token.startswith(b"("):
            return token[1:-1]                      # bytes: decoded per font
        if token in (b"true", b"false"):
            return token == b"true"
        if token == b"null":
            return None
        if _NUMBER_RE.match(token):
            return self._number_or_ref(token)
        return Keyword(token.decode("latin-1", "replace"))

    def _number_or_ref(self, token: bytes) -> Any:
        text = token.decode("ascii")
        value: Any = float(text) if ("." in text) else int(text)
        if isinstance(value, int) and value >= 0:
            # `12 0 R` - look ahead, and rewind if it is not a reference.
            save = self.pos
            second = self.token()
            if second is not None and second.isdigit():
                third = self.token()
                if third == b"R":
                    return Ref(value, int(second))
            self.pos = save
        return value

    def _array(self) -> List[Any]:
        out: List[Any] = []
        while True:
            token = self.token()
            if token is None or token == b"]":
                return out
            out.append(self._object_from(token))

    def _dictionary(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        while True:
            token = self.token()
            if token is None or token == b">>":
                return out
            if not token.startswith(b"/"):
                continue                            # skip a malformed key
            key = token[1:].decode("latin-1")
            out[key] = self.object()


class Keyword(str):
    """A bare keyword - a content-stream operator, or `obj` / `endobj`."""
    __slots__ = ()


# ===========================================================================
# 2. Filters
# ===========================================================================

def _apply_predictor(data: bytes, params: Dict[str, Any]) -> bytes:
    """Undo the PNG or TIFF predictor a compressed stream may carry."""
    predictor = int(params.get("Predictor", 1) or 1)
    if predictor < 2:
        return data
    colors = int(params.get("Colors", 1) or 1)
    bpc = int(params.get("BitsPerComponent", 8) or 8)
    columns = int(params.get("Columns", 1) or 1)
    sample = max(1, (colors * bpc + 7) // 8)
    row_len = (columns * colors * bpc + 7) // 8
    if predictor == 2:                              # TIFF
        return data
    out = bytearray()
    previous = bytearray(row_len)
    step = row_len + 1
    for start in range(0, len(data) - row_len, step):
        tag = data[start]
        row = bytearray(data[start + 1:start + 1 + row_len])
        if tag == 1:
            for i in range(sample, len(row)):
                row[i] = (row[i] + row[i - sample]) & 0xFF
        elif tag == 2:
            for i in range(len(row)):
                row[i] = (row[i] + previous[i]) & 0xFF
        elif tag == 3:
            for i in range(len(row)):
                left = row[i - sample] if i >= sample else 0
                row[i] = (row[i] + ((left + previous[i]) >> 1)) & 0xFF
        elif tag == 4:
            for i in range(len(row)):
                left = row[i - sample] if i >= sample else 0
                up = previous[i]
                upleft = previous[i - sample] if i >= sample else 0
                p = left + up - upleft
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - upleft)
                best = left if (pa <= pb and pa <= pc) else (up if pb <= pc else upleft)
                row[i] = (row[i] + best) & 0xFF
        out += row
        previous = row
    return bytes(out)


def _ascii85(data: bytes) -> bytes:
    body = re.sub(rb"\s", b"", data)
    if body.startswith(b"<~"):
        body = body[2:]
    end = body.find(b"~>")
    if end != -1:
        body = body[:end]
    out = bytearray()
    group: List[int] = []
    for byte in body:
        if byte == 0x7A and not group:              # 'z' - four zero bytes
            out += b"\0\0\0\0"
            continue
        if not (0x21 <= byte <= 0x75):
            continue
        group.append(byte - 0x21)
        if len(group) == 5:
            value = 0
            for digit in group:
                value = value * 85 + digit
            out += value.to_bytes(4, "big")
            group = []
    if group:
        padding = 5 - len(group)
        value = 0
        for digit in group + [84] * padding:
            value = value * 85 + digit
        out += value.to_bytes(4, "big")[:4 - padding]
    return bytes(out)


def _runlength(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(data):
        length = data[i]
        i += 1
        if length == 128:
            break
        if length < 128:
            out += data[i:i + length + 1]
            i += length + 1
        else:
            if i < len(data):
                out += bytes([data[i]]) * (257 - length)
                i += 1
    return bytes(out)


def _lzw(data: bytes, early: int = 1) -> bytes:
    out = bytearray()
    table: List[bytes] = [bytes([i]) for i in range(256)] + [b"", b""]
    width, previous = 9, None
    buffer = value = 0
    for byte in data:
        buffer = (buffer << 8) | byte
        value += 8
        while value >= width:
            code = (buffer >> (value - width)) & ((1 << width) - 1)
            value -= width
            if code == 256:
                table = [bytes([i]) for i in range(256)] + [b"", b""]
                width, previous = 9, None
                continue
            if code == 257:
                return bytes(out)
            if previous is None:
                entry = table[code]
            elif code < len(table):
                entry = table[code]
                table.append(previous + entry[:1])
            else:
                entry = previous + previous[:1]
                table.append(entry)
            out += entry
            previous = entry
            if len(table) + early >= (1 << width) and width < 12:
                width += 1
    return bytes(out)


def decode_stream(stream: Stream, resolve) -> bytes:
    """A stream's bytes with every filter applied. Image filters are left."""
    data = stream.raw
    filters = resolve(stream.dict.get("Filter"))
    if filters is None:
        return data
    if not isinstance(filters, list):
        filters = [filters]
    params = resolve(stream.dict.get("DecodeParms")) or {}
    if not isinstance(params, list):
        params = [params]
    for index, name in enumerate(filters):
        name = str(resolve(name) or "")
        parm = resolve(params[index]) if index < len(params) else {}
        parm = parm if isinstance(parm, dict) else {}
        try:
            if name in ("FlateDecode", "Fl"):
                try:
                    data = zlib.decompress(data)
                except zlib.error:
                    data = zlib.decompressobj().decompress(data)  # truncated tail
                data = _apply_predictor(data, {k: resolve(v) for k, v in parm.items()})
            elif name in ("ASCIIHexDecode", "AHx"):
                body = re.sub(rb"[^0-9A-Fa-f]", b"", data.split(b">")[0])
                if len(body) % 2:
                    body += b"0"
                data = bytes.fromhex(body.decode("ascii"))
            elif name in ("ASCII85Decode", "A85"):
                data = _ascii85(data)
            elif name in ("RunLengthDecode", "RL"):
                data = _runlength(data)
            elif name == "LZWDecode":
                data = _lzw(data, int(resolve(parm.get("EarlyChange", 1)) or 1))
                data = _apply_predictor(data, {k: resolve(v) for k, v in parm.items()})
            else:
                return b""                          # an image codec: not text
        except Exception:                           # noqa: BLE001 - one stream
            return b""
    return data


# ===========================================================================
# 3. The document: objects, pages, fonts
# ===========================================================================

_OBJ_RE = re.compile(rb"(?<![0-9])(\d{1,10})\s+(\d{1,5})\s+obj\b")


@dataclass
class Page:
    number: int
    lines: List[str] = field(default_factory=list)
    #: Text drawing operators seen. Zero means the page carries no text at
    #: all - a scan - which is a different answer from "text we could not
    #: decode", and the caller reports them differently.
    text_operators: int = 0

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@dataclass
class Document:
    pages: List[Page]
    encrypted: bool = False
    notes: List[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(page.text for page in self.pages)


class _Pdf:
    """The object graph, read by scanning rather than through the xref table.

    A scan is used on purpose: Jira exports come through browsers, mail
    gateways and SharePoint, and a damaged or rebuilt xref is common. Every
    object that parses is usable, whatever the table says.
    """

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.objects: Dict[int, Any] = {}
        self.trailer: Dict[str, Any] = {}
        self._scan()
        self._expand_object_streams()

    # -- reading ------------------------------------------------------------
    def _scan(self) -> None:
        for match in _OBJ_RE.finditer(self.data):
            number = int(match.group(1))
            lexer = Lexer(self.data, match.end())
            try:
                value = lexer.object()
            except Exception:                       # noqa: BLE001
                continue
            if isinstance(value, dict):
                lexer.skip_space()
                if self.data[lexer.pos:lexer.pos + 6] == b"stream":
                    value = self._read_stream(value, lexer.pos + 6)
            self.objects[number] = value

        for match in re.finditer(rb"trailer", self.data):
            lexer = Lexer(self.data, match.end())
            found = lexer.object()
            if isinstance(found, dict):
                self.trailer.update(found)

    def _read_stream(self, dictionary: Dict[str, Any], pos: int) -> Stream:
        data = self.data
        if data[pos:pos + 2] == b"\r\n":
            pos += 2
        elif data[pos:pos + 1] in (b"\n", b"\r"):
            pos += 1
        length = self.resolve(dictionary.get("Length"))
        raw = b""
        if isinstance(length, int) and 0 <= length <= len(data) - pos:
            raw = data[pos:pos + length]
            tail = data[pos + length:pos + length + 20]
            if b"endstream" not in tail:            # Length lied
                raw = b""
        if not raw:
            end = data.find(b"endstream", pos)
            raw = data[pos:end if end != -1 else len(data)]
            raw = raw.rstrip(b"\r\n")
        return Stream(dictionary, raw)

    def _expand_object_streams(self) -> None:
        """Objects packed inside an /ObjStm, as modern writers emit them."""
        for value in list(self.objects.values()):
            if not isinstance(value, Stream):
                continue
            if str(self.resolve(value.dict.get("Type")) or "") != "ObjStm":
                continue
            body = decode_stream(value, self.resolve)
            if not body:
                continue
            count = int(self.resolve(value.dict.get("N")) or 0)
            first = int(self.resolve(value.dict.get("First")) or 0)
            header = Lexer(body[:first])
            pairs: List[Tuple[int, int]] = []
            for _ in range(count):
                num, offset = header.object(), header.object()
                if not isinstance(num, int) or not isinstance(offset, int):
                    break
                pairs.append((num, offset))
            for num, offset in pairs:
                if num in self.objects:
                    continue
                try:
                    self.objects[num] = Lexer(body, first + offset).object()
                except Exception:                   # noqa: BLE001
                    continue

    # -- graph --------------------------------------------------------------
    def resolve(self, value: Any, depth: int = 0) -> Any:
        while isinstance(value, Ref) and depth < 32:
            value = self.objects.get(value.num)
            depth += 1
        return value

    def get(self, dictionary: Any, key: str, default: Any = None) -> Any:
        dictionary = self.resolve(dictionary)
        if isinstance(dictionary, Stream):
            dictionary = dictionary.dict
        if not isinstance(dictionary, dict):
            return default
        found = self.resolve(dictionary.get(key))
        return default if found is None else found

    @property
    def encrypted(self) -> bool:
        if "Encrypt" in self.trailer:
            return True
        return any(isinstance(v, dict) and "Encrypt" in v
                   for v in [self.trailer])

    def pages(self) -> List[Dict[str, Any]]:
        """Page dictionaries in document order, with Resources inherited."""
        root = None
        for source in (self.trailer.get("Root"),):
            candidate = self.resolve(source)
            if isinstance(candidate, dict) and "Pages" in candidate:
                root = candidate
        if root is None:                            # no usable trailer
            for value in self.objects.values():
                value = self.resolve(value)
                if isinstance(value, dict) and str(value.get("Type") or "") == "Catalog":
                    root = value
                    break
        out: List[Dict[str, Any]] = []
        if root is not None:
            self._walk(self.get(root, "Pages"), {}, out, set())
        if not out:                                 # last resort: every /Page
            for number in sorted(self.objects):
                value = self.resolve(self.objects[number])
                if isinstance(value, dict) and str(value.get("Type") or "") == "Page":
                    out.append(value)
        return out

    def _walk(self, node: Any, inherited: Dict[str, Any],
              out: List[Dict[str, Any]], seen: set, depth: int = 0) -> None:
        node = self.resolve(node)
        if not isinstance(node, dict) or depth > 64:
            return
        marker = id(node)
        if marker in seen:
            return
        seen.add(marker)
        passed = dict(inherited)
        for key in ("Resources", "MediaBox", "CropBox", "Rotate"):
            if key in node:
                passed[key] = node[key]
        kind = str(self.resolve(node.get("Type")) or "")
        kids = self.resolve(node.get("Kids"))
        if kind == "Page" or (kids is None and "Contents" in node):
            merged = dict(passed)
            merged.update(node)
            out.append(merged)
            return
        if isinstance(kids, list):
            for kid in kids:
                self._walk(kid, passed, out, seen, depth + 1)


# ===========================================================================
# 4. Fonts: turning codes into text
# ===========================================================================

#: Glyph names that appear in /Differences arrays often enough to matter.
#: Anything else falls back to the code point, which is right for the Latin-1
#: range a German or English ticket uses.
_GLYPHS = {
    "space": " ", "exclam": "!", "quotedbl": '"', "numbersign": "#",
    "dollar": "$", "percent": "%", "ampersand": "&", "quotesingle": "'",
    "parenleft": "(", "parenright": ")", "asterisk": "*", "plus": "+",
    "comma": ",", "hyphen": "-", "period": ".", "slash": "/", "zero": "0",
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "colon": ":",
    "semicolon": ";", "less": "<", "equal": "=", "greater": ">",
    "question": "?", "at": "@", "bracketleft": "[", "backslash": "\\",
    "bracketright": "]", "asciicircum": "^", "underscore": "_",
    "grave": "`", "braceleft": "{", "bar": "|", "braceright": "}",
    "asciitilde": "~", "quoteleft": "‘", "quoteright": "’",
    "quotedblleft": "“", "quotedblright": "”", "bullet": "•",
    "endash": "–", "emdash": "—", "germandbls": "ß",
    "adieresis": "ä", "odieresis": "ö", "udieresis": "ü",
    "Adieresis": "Ä", "Odieresis": "Ö", "Udieresis": "Ü",
    "agrave": "à", "aacute": "á", "acircumflex": "â",
    "eacute": "é", "egrave": "è", "ecircumflex": "ê",
    "ccedilla": "ç", "ntilde": "ñ", "oacute": "ó",
    "ouml": "ö", "euro": "€", "sterling": "£",
    "section": "§", "degree": "°", "paragraph": "¶",
    "guillemotleft": "«", "guillemotright": "»",
    "quotedblbase": "„", "ellipsis": "…", "trademark": "™",
    "copyright": "©", "registered": "®", "nbspace": " ",
}

_UNI_RE = re.compile(r"^uni([0-9A-Fa-f]{4,6})$")


def _glyph_text(name: str) -> Optional[str]:
    if name in _GLYPHS:
        return _GLYPHS[name]
    match = _UNI_RE.match(name)
    if match:
        try:
            return chr(int(match.group(1), 16))
        except ValueError:
            return None
    return None


@dataclass
class Font:
    """One font, reduced to what text extraction needs."""
    two_byte: bool = False
    to_unicode: Dict[int, str] = field(default_factory=dict)
    encoding: Dict[int, str] = field(default_factory=dict)
    widths: Dict[int, float] = field(default_factory=dict)
    default_width: float = 500.0

    def decode(self, data: bytes) -> List[Tuple[int, str]]:
        """(code, text) for each character code in a PDF string."""
        out: List[Tuple[int, str]] = []
        if self.two_byte:
            for i in range(0, len(data) - 1, 2):
                code = (data[i] << 8) | data[i + 1]
                out.append((code, self._text(code)))
            return out
        for byte in data:
            out.append((byte, self._text(byte)))
        return out

    def _text(self, code: int) -> str:
        if code in self.to_unicode:
            return self.to_unicode[code]
        if code in self.encoding:
            return self.encoding[code]
        if self.two_byte:
            return ""                               # a glyph id with no map
        if code in (9, 10, 13):
            return " "
        if 32 <= code <= 255:
            return bytes([code]).decode("cp1252", "replace")
        return ""

    def width(self, code: int) -> float:
        return self.widths.get(code, self.default_width) / 1000.0


_CMAP_RANGE_RE = re.compile(rb"begincidrange|beginbfrange", re.I)


def _cmap_hex(token: bytes) -> int:
    return int(token.strip(b"<>").decode("ascii", "ignore") or "0", 16)


def _cmap_text(token: bytes) -> str:
    """A bfchar/bfrange destination: UTF-16BE hex, possibly several chars."""
    body = token.strip(b"<>")
    try:
        raw = bytes.fromhex(body.decode("ascii"))
    except ValueError:
        return ""
    try:
        text = raw.decode("utf-16-be", "ignore")
    except Exception:                               # noqa: BLE001
        return ""
    return text.replace("\x00", "")


def parse_tounicode(data: bytes) -> Dict[int, str]:
    """`code -> text` from a ToUnicode CMap stream."""
    out: Dict[int, str] = {}
    for block in re.findall(rb"beginbfchar(.*?)endbfchar", data, re.S):
        tokens = re.findall(rb"<[0-9A-Fa-f]+>", block)
        for i in range(0, len(tokens) - 1, 2):
            out[_cmap_hex(tokens[i])] = _cmap_text(tokens[i + 1])
    for block in re.findall(rb"beginbfrange(.*?)endbfrange", data, re.S):
        # Two shapes: `<lo> <hi> <dst>` and `<lo> <hi> [<d1> <d2> ...]`
        for match in re.finditer(
                rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(\[[^\]]*\]|<[0-9A-Fa-f]*>)",
                block, re.S):
            low, high = int(match.group(1), 16), int(match.group(2), 16)
            target = match.group(3)
            if high < low or high - low > 65535:
                continue
            if target.startswith(b"["):
                items = re.findall(rb"<[0-9A-Fa-f]*>", target)
                for offset, item in enumerate(items):
                    out[low + offset] = _cmap_text(item)
            else:
                text = _cmap_text(target)
                if not text:
                    continue
                first = ord(text[0])
                for offset in range(high - low + 1):
                    out[low + offset] = chr(first + offset) + text[1:]
    return out


def build_font(pdf: _Pdf, font_dict: Any) -> Font:
    font = Font()
    subtype = str(pdf.get(font_dict, "Subtype") or "")
    descendants = pdf.get(font_dict, "DescendantFonts")
    descendant = None
    if isinstance(descendants, list) and descendants:
        descendant = pdf.resolve(descendants[0])

    encoding = pdf.get(font_dict, "Encoding")
    if subtype == "Type0":
        font.two_byte = True                        # Identity-H and friends
        if isinstance(encoding, Stream):            # an embedded CMap
            body = decode_stream(encoding, pdf.resolve)
            if b"begincodespacerange" in body and b"<00> <ff>" in body.lower():
                font.two_byte = False

    # ToUnicode is the reliable map, and the only one a subsetted font has.
    to_unicode = pdf.get(font_dict, "ToUnicode")
    if isinstance(to_unicode, Stream):
        font.to_unicode = parse_tounicode(decode_stream(to_unicode, pdf.resolve))

    # Simple-font encoding: a base name plus /Differences.
    if isinstance(encoding, dict):
        differences = pdf.resolve(encoding.get("Differences"))
        if isinstance(differences, list):
            code = 0
            for item in differences:
                item = pdf.resolve(item)
                if isinstance(item, (int, float)):
                    code = int(item)
                elif isinstance(item, str):
                    text = _glyph_text(str(item))
                    if text:
                        font.encoding[code] = text
                    code += 1

    # Widths, so a gap between two runs can be told from a space.
    if descendant is not None:
        font.default_width = float(pdf.get(descendant, "DW", 1000.0) or 1000.0)
        widths = pdf.get(descendant, "W")
        if isinstance(widths, list):
            index = 0
            while index < len(widths):
                first = pdf.resolve(widths[index])
                second = pdf.resolve(widths[index + 1]) if index + 1 < len(widths) else None
                if isinstance(second, list):
                    for offset, value in enumerate(second):
                        value = pdf.resolve(value)
                        if isinstance(value, (int, float)) and isinstance(first, (int, float)):
                            font.widths[int(first) + offset] = float(value)
                    index += 2
                elif isinstance(second, (int, float)) and index + 2 < len(widths):
                    third = pdf.resolve(widths[index + 2])
                    if isinstance(first, (int, float)) and isinstance(third, (int, float)):
                        for code in range(int(first), min(int(second), int(first) + 65535) + 1):
                            font.widths[code] = float(third)
                    index += 3
                else:
                    break
    else:
        first_char = pdf.get(font_dict, "FirstChar")
        widths = pdf.get(font_dict, "Widths")
        if isinstance(widths, list) and isinstance(first_char, (int, float)):
            for offset, value in enumerate(widths):
                value = pdf.resolve(value)
                if isinstance(value, (int, float)):
                    font.widths[int(first_char) + offset] = float(value)
    return font


# ===========================================================================
# 5. Content streams: where the text actually is
# ===========================================================================

Matrix = Tuple[float, float, float, float, float, float]
_IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _multiply(a: Matrix, b: Matrix) -> Matrix:
    return (a[0] * b[0] + a[1] * b[2], a[0] * b[1] + a[1] * b[3],
            a[2] * b[0] + a[3] * b[2], a[2] * b[1] + a[3] * b[3],
            a[4] * b[0] + a[5] * b[2] + b[4], a[4] * b[1] + a[5] * b[3] + b[5])


@dataclass
class _Run:
    x: float
    y: float
    size: float
    text: str
    width: float                                    # in device units


class _Renderer:
    """Replays a content stream and collects positioned text runs."""

    def __init__(self, pdf: _Pdf) -> None:
        self.pdf = pdf
        self.runs: List[_Run] = []
        self.operators = 0

    def run(self, data: bytes, resources: Any, ctm: Matrix, depth: int = 0) -> None:
        if depth > 8:
            return
        fonts = self.pdf.get(resources, "Font") or {}
        xobjects = self.pdf.get(resources, "XObject") or {}
        lexer = Lexer(data)
        stack: List[Any] = []
        graphics: List[Matrix] = []
        matrix = ctm
        text_matrix = line_matrix = _IDENTITY
        font: Optional[Font] = None
        size = leading = 0.0
        char_space = word_space = 0.0
        horizontal = 1.0
        cache: Dict[str, Font] = {}

        while True:
            token = lexer.token()
            if token is None:
                break
            obj = lexer._object_from(token)
            if not isinstance(obj, Keyword):
                stack.append(obj)
                if len(stack) > 64:
                    del stack[:-32]
                continue
            op = str(obj)

            if op == "q":
                graphics.append(matrix)
            elif op == "Q":
                matrix = graphics.pop() if graphics else matrix
            elif op == "cm" and len(stack) >= 6:
                matrix = _multiply(tuple(float(v) for v in stack[-6:]), matrix)  # type: ignore[arg-type]
            elif op == "BT":
                text_matrix = line_matrix = _IDENTITY
            elif op == "ET":
                pass
            elif op == "Tf" and len(stack) >= 2:
                name = str(stack[-2])
                size = float(stack[-1] or 0)
                if name not in cache:
                    cache[name] = build_font(self.pdf, self.pdf.get(fonts, name))
                font = cache[name]
            elif op == "TL" and stack:
                leading = float(stack[-1] or 0)
            elif op == "Tc" and stack:
                char_space = float(stack[-1] or 0)
            elif op == "Tw" and stack:
                word_space = float(stack[-1] or 0)
            elif op == "Tz" and stack:
                horizontal = float(stack[-1] or 100) / 100.0
            elif op == "Tm" and len(stack) >= 6:
                text_matrix = line_matrix = tuple(float(v) for v in stack[-6:])  # type: ignore[assignment]
            elif op in ("Td", "TD") and len(stack) >= 2:
                if op == "TD":
                    leading = -float(stack[-1] or 0)
                shift = (1.0, 0.0, 0.0, 1.0, float(stack[-2] or 0), float(stack[-1] or 0))
                text_matrix = line_matrix = _multiply(shift, line_matrix)
            elif op == "T*":
                shift = (1.0, 0.0, 0.0, 1.0, 0.0, -leading)
                text_matrix = line_matrix = _multiply(shift, line_matrix)
            elif op in ("Tj", "TJ", "'", '"'):
                self.operators += 1
                if op in ("'", '"'):
                    shift = (1.0, 0.0, 0.0, 1.0, 0.0, -leading)
                    text_matrix = line_matrix = _multiply(shift, line_matrix)
                items = stack[-1] if stack else b""
                if not isinstance(items, list):
                    items = [items]
                if font is not None:
                    text_matrix = self._show(items, font, size, char_space,
                                             word_space, horizontal,
                                             text_matrix, matrix)
            elif op == "Do" and stack:
                name = str(stack[-1])
                form = self.pdf.get(xobjects, name)
                if isinstance(form, Stream) and \
                        str(self.pdf.get(form.dict, "Subtype") or "") == "Form":
                    inner = self.pdf.get(form.dict, "Resources") or resources
                    form_matrix = self.pdf.get(form.dict, "Matrix")
                    local = matrix
                    if isinstance(form_matrix, list) and len(form_matrix) == 6:
                        local = _multiply(tuple(float(self.pdf.resolve(v) or 0)
                                                for v in form_matrix), matrix)  # type: ignore[arg-type]
                    self.run(decode_stream(form, self.pdf.resolve), inner, local,
                             depth + 1)
            if isinstance(obj, Keyword):
                stack = []

    def _show(self, items: Sequence[Any], font: Font, size: float,
              char_space: float, word_space: float, horizontal: float,
              text_matrix: Matrix, ctm: Matrix) -> Matrix:
        """Draw one text-showing operator, advancing the text matrix."""
        for item in items:
            if isinstance(item, (int, float)):
                shift = -float(item) / 1000.0 * size * horizontal
                text_matrix = _multiply((1, 0, 0, 1, shift, 0), text_matrix)
                continue
            if not isinstance(item, bytes):
                continue
            for code, text in font.decode(item):
                device = _multiply(text_matrix, ctm)
                advance = (font.width(code) * size + char_space
                           + (word_space if code == 32 and not font.two_byte else 0)
                           ) * horizontal
                if text and text.strip():
                    self.runs.append(_Run(device[4], device[5], size, text,
                                          abs(advance * ctm[0] or advance)))
                elif text == " ":
                    self.runs.append(_Run(device[4], device[5], size, " ",
                                          abs(advance * ctm[0] or advance)))
                text_matrix = _multiply((1, 0, 0, 1, advance, 0), text_matrix)
        return text_matrix


def _lines_from_runs(runs: List[_Run]) -> List[str]:
    """Positioned runs become lines of text, top to bottom, left to right."""
    if not runs:
        return []
    sizes = sorted(run.size for run in runs if run.size) or [10.0]
    typical = sizes[len(sizes) // 2] or 10.0
    tolerance = max(1.5, typical * 0.45)

    ordered = sorted(runs, key=lambda r: (-r.y, r.x))
    lines: List[List[_Run]] = []
    current: List[_Run] = []
    baseline = None
    for run in ordered:
        if baseline is None or abs(run.y - baseline) <= tolerance:
            current.append(run)
            baseline = run.y if baseline is None else baseline
        else:
            lines.append(current)
            current = [run]
            baseline = run.y
    if current:
        lines.append(current)

    out: List[str] = []
    for line in lines:
        line.sort(key=lambda r: r.x)
        text = ""
        previous_end = None
        for run in line:
            if previous_end is not None:
                gap = run.x - previous_end
                if gap > typical * 0.22 and not text.endswith(" ") \
                        and run.text != " ":
                    text += " "
            text += run.text
            previous_end = run.x + run.width
        text = re.sub(r"[ \t]{2,}", "  ", text).rstrip()
        if text.strip():
            out.append(text)
    return out


# ===========================================================================
# 6. Entry point
# ===========================================================================

def extract(data: bytes) -> Document:
    """Every page's text. Never raises for a readable file; reports instead."""
    if not data[:1024].lstrip().startswith(b"%PDF"):
        raise PdfError("not a PDF (no %PDF header)")

    pdf = _Pdf(data)
    document = Document(pages=[])
    if "Encrypt" in pdf.trailer:
        document.encrypted = True
        document.notes.append(
            "the PDF is encrypted; export it again without protection")
        return document

    page_dicts = pdf.pages()
    if not page_dicts:
        document.notes.append("no pages could be read from the file")
        return document

    for number, page_dict in enumerate(page_dicts, 1):
        page = Page(number=number)
        contents = pdf.get(page_dict, "Contents")
        streams = contents if isinstance(contents, list) else [contents]
        body = b""
        for item in streams:
            item = pdf.resolve(item)
            if isinstance(item, Stream):
                body += decode_stream(item, pdf.resolve) + b"\n"
        if body.strip():
            box = pdf.get(page_dict, "MediaBox")
            height = 792.0
            if isinstance(box, list) and len(box) == 4:
                try:
                    height = abs(float(pdf.resolve(box[3]) or 0)
                                 - float(pdf.resolve(box[1]) or 0)) or 792.0
                except (TypeError, ValueError):
                    height = 792.0
            renderer = _Renderer(pdf)
            try:
                renderer.run(body, pdf.get(page_dict, "Resources"), _IDENTITY)
            except Exception as exc:                # noqa: BLE001
                document.notes.append(f"page {number}: {type(exc).__name__}")
            page.text_operators = renderer.operators
            # A page drawn upside down (a flipped CTM) sorts the same way
            # once the sign of y is normalised against the page height.
            runs = renderer.runs
            if runs and all(run.y < 0 for run in runs):
                for run in runs:
                    run.y += height
            page.lines = _lines_from_runs(runs)
        document.pages.append(page)
    return document
