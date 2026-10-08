"""Text extraction for course files: PDF, Word, PowerPoint, HTML, RTF and text.

Pure functions (bytes in, text out) that run in a worker thread and need no
Canvas access. Word and PowerPoint files are read straight from their XML so
that no text container is dropped: content controls, tables, text boxes,
headers, footers, footnotes, endnotes, comments, charts, SmartArt, speaker
notes and hidden slides are all included. Pictures cannot be read without OCR;
they are counted in a warning instead of being skipped silently.
"""

from __future__ import annotations

import codecs
import email
import io
import posixpath
import re
import time
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import PurePosixPath
from typing import Callable, Iterable, Iterator

MAX_EXPANDED_BYTES = 100 * 1024 * 1024  # decompressed parts, including embedded Office files
MAX_ARCHIVE_ENTRIES = 20_000
MAX_CHART_POINTS = 500
MAX_EMBED_DEPTH = 2

SUPPORTED_FORMATS = "PDF, Word (.docx), PowerPoint (.pptx), HTML, RTF and plain-text files"


class DocumentError(Exception):
    """A reportable outcome with a stable status; messages never carry secrets."""

    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Extracted:
    """Text blocks in reading order, plus what could not be read.

    ``errors`` are failures (a part that could not be parsed, damaged XML):
    the text is a partial result. ``warnings`` are known limitations that are
    not errors (images without OCR, embedded objects, hidden slides).
    """

    blocks: list[str]
    warnings: list[str] = field(default_factory=list)
    complete: bool = True
    units: int | None = None  # slides, for presentations
    errors: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Formats
# --------------------------------------------------------------------------

_EXTENSIONS = {
    ".pdf": "pdf",
    ".docx": "docx", ".docm": "docx", ".dotx": "docx", ".dotm": "docx",
    ".pptx": "pptx", ".pptm": "pptx", ".ppsx": "pptx", ".ppsm": "pptx",
    ".potx": "pptx", ".potm": "pptx",
    ".html": "html", ".htm": "html", ".xhtml": "html",
    ".rtf": "rtf",
    ".txt": "text", ".text": "text", ".md": "text", ".markdown": "text",
    ".csv": "text", ".tsv": "text",
}
_MIME_TYPES = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.template": "docx",
    "application/vnd.ms-word.document.macroenabled.12": "docx",
    "application/vnd.ms-word.template.macroenabled.12": "docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/vnd.openxmlformats-officedocument.presentationml.slideshow": "pptx",
    "application/vnd.openxmlformats-officedocument.presentationml.template": "pptx",
    "application/vnd.ms-powerpoint.presentation.macroenabled.12": "pptx",
    "application/vnd.ms-powerpoint.slideshow.macroenabled.12": "pptx",
    "text/html": "html",
    "application/xhtml+xml": "html",
    "application/rtf": "rtf",
    "text/rtf": "rtf",
}


def document_kind(filename: str, mime_type: str) -> str:
    """Choose an extractor from the file extension, then the MIME type."""
    extension = PurePosixPath(filename).suffix.lower()
    if extension in _EXTENSIONS:
        return _EXTENSIONS[extension]
    mime = mime_type.split(";", 1)[0].strip().lower()
    if mime in _MIME_TYPES:
        return _MIME_TYPES[mime]
    if mime.startswith("text/"):
        return "text"
    raise DocumentError("unsupported", f"This file format is not supported. Supported: {SUPPORTED_FORMATS}.")


def extract_document(data: bytes, kind: str) -> Extracted:
    """Extract every non-PDF format. PDFs use ``pdf_pages`` so long files can resume."""
    if kind == "docx":
        return word_text(data)
    if kind == "pptx":
        return slides_text(data)
    text, warnings = decode_text(data)
    if kind == "html":
        text = html_text(text)
    elif kind == "rtf":
        text = rtf_text(text)
    return Extracted([text.strip()], warnings)


# --------------------------------------------------------------------------
# Plain text, HTML, RTF
# --------------------------------------------------------------------------

def decode_text(data: bytes) -> tuple[str, list[str]]:
    warnings: list[str] = []
    if data.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        text = data.decode("utf-32")
    elif data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        text = data.decode("utf-16")
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("cp1252", errors="replace")
            warnings.append("Decoded text as Windows-1252 because it was not UTF-8.")
    if any(ord(c) < 32 and c not in "\n\r\t\f" for c in text):
        raise DocumentError("unreadable", "The file contains binary data instead of readable text.")
    return text.replace("\r\n", "\n").replace("\r", "\n"), warnings


class _HTMLText(HTMLParser):
    BLOCKS = {"p", "div", "section", "article", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}
    HIDDEN = {"script", "style", "template", "noscript"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.HIDDEN:
            self.hidden.append(tag)
        if not self.hidden and tag in self.BLOCKS:
            self.parts.append("\n")
        if not self.hidden and tag in {"td", "th"}:
            self.parts.append("\t")

    def handle_endtag(self, tag: str) -> None:
        if self.hidden:
            if tag == self.hidden[-1]:
                self.hidden.pop()
            return
        if tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def html_text(markup: str) -> str:
    """Visible text of an HTML document; scripts, styles and templates are dropped."""
    parser = _HTMLText()
    parser.feed(markup)
    parser.close()
    text = re.sub(r"[ \t]+", " ", "".join(parser.parts))
    return re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", text)


_RTF_TOKEN = re.compile(
    r"\\([a-z]{1,32})(-?\d{1,10})?[ ]?|\\'([0-9a-f]{2})|\\([^a-z])|([{}])|[\r\n]+|(.)",
    re.IGNORECASE | re.DOTALL,
)
# Destinations that hold formatting, metadata or field codes, not visible text.
_RTF_IGNORED = {
    "fonttbl", "colortbl", "stylesheet", "info", "pict", "object", "themedata",
    "colorschememapping", "datastore", "latentstyles", "listtable", "listoverridetable",
    "rsidtbl", "xmlnstbl", "generator", "filetbl", "revtbl", "mmathPr", "fldinst",
    "nonshppict", "pgdsctbl", "listtext", "pntext", "pntxtb", "pntxta", "sp", "sn",
}
_RTF_SPECIAL = {
    "par": "\n", "line": "\n", "sect": "\n", "page": "\n", "row": "\n",
    "tab": "\t", "cell": "\t", "emdash": "\u2014", "endash": "\u2013", "bullet": "\u2022",
    "lquote": "\u2018", "rquote": "\u2019", "ldblquote": "\u201c", "rdblquote": "\u201d",
}


def rtf_text(raw: str) -> str:
    """Visible text of an RTF document (formatting groups and field codes removed)."""
    stack: list[tuple[int, bool]] = []
    ignorable, uc_skip, skip, out = False, 1, 0, []
    for match in _RTF_TOKEN.finditer(raw):
        word, argument, hexcode, symbol, brace, char = match.groups()
        if brace:
            skip = 0
            if brace == "{":
                stack.append((uc_skip, ignorable))
            elif stack:
                uc_skip, ignorable = stack.pop()
        elif symbol:
            skip = 0
            if symbol == "*":
                ignorable = True
            elif not ignorable:
                if symbol in "\r\n":
                    out.append("\n")
                else:
                    out.append({"~": "\u00a0", "_": "-", "-": ""}.get(symbol, symbol if symbol in "{}\\" else ""))
        elif word:
            skip = 0
            if word in _RTF_IGNORED:
                ignorable = True
            elif ignorable:
                continue
            elif word in _RTF_SPECIAL:
                out.append(_RTF_SPECIAL[word])
            elif word == "uc":
                uc_skip = int(argument or 1)
            elif word == "u" and argument:
                code = int(argument)
                # RTF Unicode escapes contain signed UTF-16 code units. Emoji
                # use two escapes; keep the units until they can be decoded together.
                code = code + 65536 if code < 0 else code
                out.append(chr(code) if 0 <= code <= 65535 else "\ufffd")
                skip = uc_skip
        elif hexcode:
            if skip:
                skip -= 1
            elif not ignorable:
                out.append(bytes([int(hexcode, 16)]).decode("cp1252", errors="replace"))
        elif char:
            if skip:
                skip -= 1
            elif not ignorable:
                out.append(char)
    # Combine surrogate pairs and replace malformed isolated units so MCP can
    # always serialize the result as valid Unicode/UTF-8.
    return "".join(out).encode("utf-16-le", errors="surrogatepass").decode("utf-16-le", errors="replace")


def _mht_text(data: bytes) -> str:
    message = email.message_from_bytes(data)
    for part in message.walk():
        if part.get_content_type() in ("text/html", "text/plain"):
            payload = part.get_payload(decode=True) or b""
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            return html_text(text) if part.get_content_type() == "text/html" else text
    raise DocumentError("unreadable", "The embedded web archive has no readable text.")


_SENSITIVE_QUERY = re.compile(
    r"(?i)([?&](?:access_token|api_token|api_key|token|verifier|signature|sig|"
    r"password|secret|authorization|x-amz-[a-z0-9-]+|awsaccesskeyid|credential|key-pair-id)=)[^\s&#<>]+"
)


def redact_urls(text: str) -> str:
    """Remove credentials from URLs that appear inside document text."""
    text = re.sub(r"(?i)(https?://)[^\s/@]+@", r"\1[redacted]@", text)
    return _SENSITIVE_QUERY.sub(r"\1[redacted]", text)


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------

@dataclass
class PdfBatch:
    """Pages read in one step of a (possibly long) PDF."""

    blocks: list[str]
    next_page: int | None  # 0-based page to continue from, or None when finished
    page_count: int
    empty_pages: list[int]  # 1-based pages without extractable text (blank or scanned)
    failed_pages: list[int]  # 1-based pages whose content could not be decoded


def pdf_pages(
    data: bytes, start: int, deadline: float, clock: Callable[[], float] = time.monotonic,
    progress: dict | None = None,
) -> PdfBatch:
    """Read pages from ``start`` until the end or ``deadline``.

    A page that fails to decode is listed in ``failed_pages`` and skipped, so
    one damaged page never costs the rest of the document. ``progress`` (if
    given) is updated with the page being read, for status messages.
    """
    from pypdf import PdfReader

    if b"%PDF-" not in data[:1_024]:
        raise DocumentError("unreadable", "This file does not contain a valid PDF document.")
    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted and not reader.decrypt(""):
        raise DocumentError("locked", "This PDF is encrypted and cannot be read without a password.")
    count = len(reader.pages)
    batch = PdfBatch([], None, count, [], [])
    page = start
    while page < count:
        if page > start and clock() >= deadline:
            batch.next_page = page
            return batch
        if progress is not None:
            progress.update(page=page + 1, pages=count)
        try:
            text = (reader.pages[page].extract_text() or "").strip()
        except Exception:
            batch.failed_pages.append(page + 1)
        else:
            if text:
                batch.blocks.append(f"[Page {page + 1}]\n{text}")
            else:
                batch.empty_pages.append(page + 1)
        page += 1
    return batch


# --------------------------------------------------------------------------
# Office Open XML (Word and PowerPoint)
# --------------------------------------------------------------------------

_NAMESPACES = {
    "w": ("http://schemas.openxmlformats.org/wordprocessingml/2006/main",
          "http://purl.oclc.org/ooxml/wordprocessingml/main"),
    "a": ("http://schemas.openxmlformats.org/drawingml/2006/main",
          "http://purl.oclc.org/ooxml/drawingml/main"),
    "p": ("http://schemas.openxmlformats.org/presentationml/2006/main",
          "http://purl.oclc.org/ooxml/presentationml/main"),
    "r": ("http://schemas.openxmlformats.org/officeDocument/2006/relationships",
          "http://purl.oclc.org/ooxml/officeDocument/relationships"),
    "m": ("http://schemas.openxmlformats.org/officeDocument/2006/math",
          "http://purl.oclc.org/ooxml/officeDocument/math"),
    "c": ("http://schemas.openxmlformats.org/drawingml/2006/chart",
          "http://purl.oclc.org/ooxml/drawingml/chart"),
    "dgm": ("http://schemas.openxmlformats.org/drawingml/2006/diagram",
            "http://purl.oclc.org/ooxml/drawingml/diagram"),
    "pic": ("http://schemas.openxmlformats.org/drawingml/2006/picture",
            "http://purl.oclc.org/ooxml/drawingml/picture"),
    "mc": ("http://schemas.openxmlformats.org/markup-compatibility/2006",),
    "v": ("urn:schemas-microsoft-com:vml",),
}
_FAMILY = {uri: family for family, uris in _NAMESPACES.items() for uri in uris}


def _name(element) -> tuple[str, str]:
    """(namespace family, local name); unknown namespaces have family ''."""
    tag = element.tag
    if not isinstance(tag, str):
        return "", ""
    if tag.startswith("{"):
        uri, _, local = tag[1:].partition("}")
        return _FAMILY.get(uri, ""), local
    return "", tag


def _attr(element, family: str, local: str) -> str | None:
    for uri in _NAMESPACES[family]:
        value = element.get(f"{{{uri}}}{local}")
        if value is not None:
            return value
    return None


def _child(element, family: str, local: str):
    return next((child for child in element if _name(child) == (family, local)), None)


def _descendant(element, family: str, local: str):
    return next((node for node in element.iter() if _name(node) == (family, local)), None)


def _local_texts(element, names: tuple[str, ...]) -> list[str]:
    return [node.text.strip() for node in element.iter()
            if _name(node)[1] in names and node.text and node.text.strip()]


def _branch(alternate):
    """mc:AlternateContent stores one object in several encodings; read only one."""
    return (_child(alternate, "mc", "Choice") if _child(alternate, "mc", "Choice") is not None
            else _child(alternate, "mc", "Fallback"))


def _unique(texts: Iterable[str]) -> list[str]:
    seen: list[str] = []
    for text in texts:
        if text and text not in seen:
            seen.append(text)
    return seen


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" + ("" if count == 1 else "s")


@dataclass
class _ExpansionBudget:
    used: int = 0

    def consume(self, size: int) -> None:
        if size > MAX_EXPANDED_BYTES - self.used:
            raise DocumentError("too_large", "This Office file expands beyond the safety limit and was not read.")
        self.used += size


class _Package:
    """XML parts of an Office file, read under a decompressed-size budget."""

    def __init__(self, data: bytes, budget: _ExpansionBudget | None = None):
        from lxml import etree

        self._etree = etree
        # No DTDs, entities or network access; the input is untrusted.
        options = {"resolve_entities": False, "no_network": True, "load_dtd": False, "huge_tree": False,
                   "remove_comments": True, "remove_pis": True}
        self._parser = etree.XMLParser(**options)
        self._recovering = etree.XMLParser(recover=True, **options)
        self.damaged: list[str] = []  # parts whose XML had to be repaired
        try:
            self._zip = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            raise DocumentError("unreadable", "This Office file is damaged or is not a .docx/.pptx file.") from None
        infos = self._zip.infolist()
        if len(infos) > MAX_ARCHIVE_ENTRIES:
            raise DocumentError("too_large", "This Office file has too many parts to read safely.")
        self._infos = {info.filename: info for info in infos}
        self.budget = budget if budget is not None else _ExpansionBudget()
        self._relationships: dict[str, dict[str, tuple[str, str, bool]]] = {}

    def names(self) -> Iterable[str]:
        return self._infos.keys()

    def read(self, name: str | None) -> bytes | None:
        info = self._infos.get(name) if name else None
        if info is None or info.is_dir():
            return None
        self.budget.consume(info.file_size)
        return self._zip.read(info)

    def xml(self, name: str | None, *, required: bool = False):
        """Parse a part; damaged XML is repaired where possible and noted in ``damaged``."""
        data = self.read(name)
        if data is None:
            if required:
                raise DocumentError("unreadable", "A referenced Office document part is missing.")
            return None
        try:
            return self._etree.fromstring(data, self._parser)
        except self._etree.XMLSyntaxError:
            self.damaged.append(posixpath.basename(name))
            try:
                return self._etree.fromstring(data, self._recovering)
            except self._etree.XMLSyntaxError:
                return None

    def rels(self, part: str) -> dict[str, tuple[str, str, bool]]:
        """Relationship ID -> (type suffix, target part or URL, is external)."""
        if part not in self._relationships:
            directory, base = posixpath.split(part)
            root = self.xml(posixpath.join(directory, "_rels", f"{base}.rels"))
            found: dict[str, tuple[str, str, bool]] = {}
            for rel in root if root is not None else ():
                if _name(rel)[1] != "Relationship" or not rel.get("Id"):
                    continue
                kind = (rel.get("Type") or "").rstrip("/").rsplit("/", 1)[-1]
                target = rel.get("Target") or ""
                external = rel.get("TargetMode") == "External"
                if not external:
                    target = posixpath.normpath(
                        target.lstrip("/") if target.startswith("/") else posixpath.join(directory, target))
                found[rel.get("Id")] = (kind, target, external)
            self._relationships[part] = found
        return self._relationships[part]

    def main_part(self, default: str) -> str:
        for kind, target, external in self.rels("").values():
            if kind == "officeDocument" and not external:
                return target
        return default


def _isolated(unread: list[str], name: str, function: Callable, *args, default=None):
    """Run one part's extraction; if it fails, note the part and keep going.

    The size budget is the only failure that stops the whole file.
    """
    try:
        return function(*args)
    except DocumentError as error:
        if error.status == "too_large":
            raise
    except Exception:
        pass
    unread.append(name)
    return default


def _chart_lines(package: _Package, part: str | None) -> list[str]:
    """Title, series and data points of a chart part (classic or chartex)."""
    root = package.xml(part, required=True)
    if root is None:
        return []
    chart = next((node for node in root.iter() if _name(node)[1] == "chart"), root)
    title = next((child for child in chart if _name(child)[1] == "title"), None)
    lines = [f"[Chart] {' '.join(_local_texts(title, ('t', 'v'))) if title is not None else ''}".rstrip()]
    series_list = [node for node in root.iter() if _name(node)[1] == "ser"]
    for number, series in enumerate(series_list, 1):
        name_node = next((child for child in series if _name(child)[1] == "tx"), None)
        name = " ".join(_local_texts(name_node, ("t", "v"))) if name_node is not None else f"Series {number}"
        categories = _chart_points(next((c for c in series if _name(c)[1] in ("cat", "xVal")), None))
        values = _chart_points(next((c for c in series if _name(c)[1] in ("val", "yVal")), None))
        pairs = [f"{categories.get(index, index + 1)} = {value}" for index, value in sorted(values.items())]
        note = " (more points not shown)" if len(pairs) > MAX_CHART_POINTS else ""
        lines.append(f"{name}: " + "; ".join(pairs[:MAX_CHART_POINTS]) + note)
    if not series_list:
        values = _local_texts(root, ("pt", "v"))
        if values:
            lines.append("Values: " + "; ".join(values[:MAX_CHART_POINTS]))
    return lines


def _chart_points(element) -> dict[int, str]:
    points: dict[int, str] = {}
    if element is None:
        return points
    for point in element.iter():
        if _name(point)[1] != "pt":
            continue
        value = next((child.text for child in point if _name(child)[1] == "v"), point.text)
        try:
            index = int(point.get("idx", len(points)))
        except ValueError:
            index = len(points)
        if value is not None and value.strip():
            points.setdefault(index, value.strip())
    return points


def _diagram_lines(package: _Package, part: str | None) -> list[str]:
    """Node text of a SmartArt diagram, from its data part."""
    root = package.xml(part, required=True)
    if root is None:
        return []
    texts = []
    for point in root.iter():
        if _name(point) != ("dgm", "pt"):
            continue
        body = _child(point, "dgm", "t")
        text = " ".join(_local_texts(body, ("t",))) if body is not None else ""
        if text:
            texts.append(text)
    return ["[Diagram] " + " | ".join(texts)] if texts else []


class _Word:
    """Text of one WordprocessingML part (body, header, footer, notes, comments)."""

    _SKIP = {
        "delText", "delInstrText", "instrText", "del", "moveFrom", "fldChar", "softHyphen",
        "lastRenderedPageBreak", "annotationRef", "footnoteRef", "endnoteRef",
        "commentReference", "separator", "continuationSeparator",
    }

    def __init__(self, package: _Package, part: str, depth: int):
        self.package, self.part, self.depth = package, part, depth
        self.rels = package.rels(part)
        self.pictures = 0
        self.objects = 0
        self.unread: list[str] = []
        self._in_object = 0

    def _target(self, rid: str | None, external: bool) -> str | None:
        found = self.rels.get(rid or "")
        return found[1] if found and found[2] == external else None

    def blocks(self, element) -> list[str]:
        """Block-level content as lines: paragraphs, table rows, text boxes."""
        lines: list[str] = []
        for child in element:
            family, local = _name(child)
            if family == "w" and local == "p":
                text, extra = self.inline(child)
                lines.append(text)
                lines.extend(extra)
            elif family == "w" and local == "tbl":
                lines.extend(self.table(child))
            elif family == "w" and (local in ("del", "moveFrom") or local.endswith("Pr")):
                continue  # deleted tracked changes and formatting properties
            elif family == "w" and local == "altChunk":
                lines.extend(self.alt_chunk(child))
            elif family == "w" and local in ("sdt", "sdtContent", "customXml", "ins", "moveTo", "smartTag"):
                lines.extend(self.blocks(child))  # content controls and other wrappers
            elif (family, local) == ("mc", "AlternateContent"):
                branch = _branch(child)
                if branch is not None:
                    lines.extend(self.blocks(branch))
            else:
                # Unknown containers keep whatever text they hold.
                text, extra = self.inline(child)
                if text.strip():
                    lines.append(text)
                lines.extend(extra)
        return lines

    def inline(self, element) -> tuple[str, list[str]]:
        """Paragraph text, plus lines for text boxes, tables, charts and diagrams inside it."""
        parts: list[str] = []
        extra: list[str] = []
        self._inline(element, parts, extra)
        return "".join(parts), extra

    def _inline(self, element, parts: list[str], extra: list[str]) -> None:
        for child in element:
            family, local = _name(child)
            if family == "w":
                if local == "t":
                    parts.append(child.text or "")
                elif local in ("tab", "ptab"):
                    parts.append("\t")
                elif local in ("br", "cr"):
                    parts.append("\n")
                elif local == "noBreakHyphen":
                    parts.append("-")
                elif local == "sym":
                    parts.append(_symbol(_attr(child, "w", "char")))
                elif local in ("footnoteReference", "endnoteReference"):
                    parts.append(f"[{local[:-9]} {_attr(child, 'w', 'id')}]")
                elif local == "hyperlink":
                    start = len(parts)
                    self._inline(child, parts, extra)
                    url = self._target(_attr(child, "r", "id"), external=True)
                    if url and url not in "".join(parts[start:]):
                        parts.append(f" <{url}>")
                elif local == "txbxContent":
                    extra.extend(self.blocks(child))
                elif local == "tbl":
                    extra.extend(self.table(child))
                elif local == "p":
                    if parts and not parts[-1].endswith("\n"):
                        parts.append("\n")
                    self._inline(child, parts, extra)
                elif local == "altChunk":
                    extra.extend(self.alt_chunk(child))
                elif local == "object":
                    self.objects += 1
                    self._in_object += 1
                    self._inline(child, parts, extra)
                    self._in_object -= 1
                elif local in self._SKIP or local.endswith("Pr"):
                    continue
                else:
                    self._inline(child, parts, extra)  # runs, fields, insertions, content controls
            elif (family, local) == ("mc", "AlternateContent"):
                branch = _branch(child)
                if branch is not None:
                    self._inline(branch, parts, extra)
            elif (family, local) in (("m", "t"), ("a", "t")):
                parts.append(child.text or "")
            elif (family, local) == ("v", "textpath") and child.get("string"):
                parts.append(child.get("string"))  # WordArt
            elif (family, local) in (("pic", "pic"), ("v", "imagedata")):
                if not self._in_object:
                    self.pictures += 1
            elif (family, local) == ("c", "chart") or (local == "chart" and _attr(child, "r", "id")):
                target = self._target(_attr(child, "r", "id"), external=False)
                extra.extend(_isolated(self.unread, "a chart", _chart_lines, self.package, target, default=[]))
            elif (family, local) == ("dgm", "relIds"):
                target = self._target(_attr(child, "r", "dm"), external=False)
                extra.extend(_isolated(self.unread, "a SmartArt diagram", _diagram_lines, self.package, target,
                                       default=[]))
            else:
                self._inline(child, parts, extra)

    def table(self, table) -> list[str]:
        """One line per row; cells separated by tabs, nested tables kept inside cells."""
        return ["\t".join("\n".join(self.blocks(cell)) for cell in _word_cells(row))
                for row in _word_rows(table)]

    def alt_chunk(self, element) -> list[str]:
        """Embedded HTML, RTF, text, web-archive or Word content (altChunk)."""
        target = self._target(_attr(element, "r", "id"), external=False)
        data = self.package.read(target)
        name = posixpath.basename(target or "embedded content")
        if data is None:
            self.unread.append(name)
            return []
        extension = PurePosixPath(name).suffix.lower()
        try:
            if extension in (".docx", ".docm", ".dotx", ".dotm"):
                if self.depth >= MAX_EMBED_DEPTH:
                    self.unread.append(name)
                    return []
                nested = word_text(data, self.depth + 1, _budget=self.package.budget)
                self.unread.extend(nested.unread)
                self.package.damaged.extend(f"{name}: {part}" for part in nested.damaged)
                self.pictures += nested.pictures
                self.objects += nested.objects
                return "\n\n".join(nested.blocks).splitlines()
            if extension in (".mht", ".mhtml"):
                text = _mht_text(data)
            else:
                text, _ = decode_text(data)
                if extension in (".htm", ".html", ".xhtml", ".xml"):
                    text = html_text(text)
                elif extension == ".rtf":
                    text = rtf_text(text)
        except DocumentError as error:
            if error.status == "too_large":
                raise
            self.unread.append(name)
            return []
        except Exception:
            self.unread.append(name)
            return []
        return text.strip().splitlines()


def _word_rows(table) -> Iterator:
    for child in table:
        family, local = _name(child)
        if family == "w" and local == "tr":
            yield child
        elif family == "w" and local in ("sdt", "sdtContent", "customXml", "ins", "moveTo"):
            yield from _word_rows(child)


def _word_cells(row) -> Iterator:
    for child in row:
        family, local = _name(child)
        if family == "w" and local == "tc":
            yield child
        elif family == "w" and local in ("sdt", "sdtContent", "customXml", "ins", "moveTo"):
            yield from _word_cells(child)


def _symbol(code: str | None) -> str:
    """w:sym characters; symbol-font private-use codes become a bullet."""
    try:
        value = int(code or "", 16)
    except ValueError:
        return ""
    return "\u2022" if 0xF000 <= value <= 0xF0FF else chr(value)


@dataclass
class _WordResult:
    blocks: list[str]
    pictures: int
    objects: int
    unread: list[str]
    damaged: list[str]


def word_text(data: bytes, depth: int = 0, *, _budget: _ExpansionBudget | None = None) -> _WordResult | Extracted:
    """Every text-bearing part of a Word document, in reading order.

    Sections: body, then [Header], [Footer], [Footnotes], [Endnotes], [Comments].
    Returns ``Extracted`` at the top level and the raw parts when nested.
    """
    package = _Package(data, _budget)
    main = package.main_part("word/document.xml")
    root = package.xml(main)
    if root is None:
        raise DocumentError("unreadable", "This Word file has no document body.")
    body_reader = _Word(package, main, depth)
    readers = [body_reader]

    def part_lines(target: str, select=None) -> list[str]:
        part_root = package.xml(target, required=True)
        if part_root is None:
            return []
        reader = _Word(package, target, depth)
        readers.append(reader)
        if select is None:
            return ["\n".join(reader.blocks(part_root)).strip()]
        return [entry for item in part_root if (entry := select(reader, item))]

    def referenced(kind: str) -> list[str]:
        targets = []
        for node in root.iter():
            if _name(node) == ("w", f"{kind}Reference"):
                target = body_reader._target(_attr(node, "r", "id"), external=False)
                if target and target not in targets:
                    targets.append(target)
                elif target is None:
                    unread.append(f"a {kind} (missing relationship)")
        return targets

    def related(kind: str) -> str | None:
        return next((target for rel_kind, target, external in body_reader.rels.values()
                     if rel_kind == kind and not external), None)

    def note(tag: str):
        def select(reader: _Word, item) -> str | None:
            if _name(item) != ("w", tag) or (_attr(item, "w", "type") or "normal") != "normal":
                return None
            text = "\n".join(reader.blocks(item)).strip()
            return f"[{tag} {_attr(item, 'w', 'id')}] {text}" if text else None
        return select

    def comment(reader: _Word, item) -> str | None:
        if _name(item) != ("w", "comment"):
            return None
        text = "\n".join(reader.blocks(item)).strip()
        return f"- {text}" if text else None

    unread: list[str] = []

    def section(target: str, label: str, select=None) -> list[str]:
        return _isolated(unread, f"{label} ({posixpath.basename(target)})", part_lines, target, select, default=[])

    body_element = _child(root, "w", "body")
    body = "\n".join(body_reader.blocks(body_element) if body_element is not None else []).strip()
    headers = _unique(text for target in referenced("header") for text in section(target, "a header"))
    footers = _unique(text for target in referenced("footer") for text in section(target, "a footer"))
    blocks = [body] if body else []
    if headers:
        blocks.append("[Header]\n" + "\n".join(headers))
    if footers:
        blocks.append("[Footer]\n" + "\n".join(footers))
    for kind, label, tag in (("footnotes", "Footnotes", "footnote"), ("endnotes", "Endnotes", "endnote")):
        target = related(kind)
        if target is None and any(_name(node) == ("w", f"{tag}Reference") for node in root.iter()):
            unread.append(f"the {kind} (missing relationship)")
        entries = section(target, f"the {kind}", note(tag)) if target else []
        if entries:
            blocks.append(f"[{label}]\n" + "\n".join(entries))
    target = related("comments")
    if target is None and any(_name(node) == ("w", "commentReference") for node in root.iter()):
        unread.append("the comments (missing relationship)")
    entries = section(target, "the comments", comment) if target else []
    if entries:
        blocks.append("[Comments]\n" + "\n".join(entries))

    result = _WordResult(blocks, sum(r.pictures for r in readers), sum(r.objects for r in readers),
                         unread + [name for r in readers for name in r.unread], list(package.damaged))
    if depth:
        return result
    warnings = []
    if result.pictures:
        warnings.append(f"The document contains {_plural(result.pictures, 'image')}; images are not read "
                        "(no OCR), and they may contain text such as tables or screenshots.")
    if result.objects:
        warnings.append(f"{_plural(result.objects, 'embedded object')} (for example a spreadsheet) "
                        "could not be read.")
    errors = []
    if result.unread:
        errors.append("Could not read " + ", ".join(_unique(result.unread)) + "; skipped.")
    if result.damaged:
        errors.append("Damaged content in " + ", ".join(_unique(result.damaged))
                      + " was repaired; some of its text may be missing.")
    return Extracted(blocks, warnings, complete=not (result.objects or errors), errors=errors)


class _Slide:
    """Text of one slide or notes page."""

    _NOTES_SKIP = {"sldImg", "sldNum", "hdr", "ftr", "dt"}

    def __init__(self, package: _Package, part: str):
        self.package, self.part = package, part
        self.rels = package.rels(part)
        self.pictures = 0
        self.objects = 0
        self.unread: list[str] = []

    def _target(self, rid: str | None) -> str | None:
        found = self.rels.get(rid or "")
        return found[1] if found and not found[2] else None

    def shapes(self, tree, notes: bool = False) -> tuple[list[str], list[str]]:
        """(title lines, other lines) in shape order; groups are flattened."""
        titles: list[str] = []
        lines: list[str] = []
        for shape in tree:
            family, local = _name(shape)
            if family == "p" and local in ("sp", "cxnSp"):
                placeholder = _descendant(shape, "p", "ph")
                kind = placeholder.get("type", "body") if placeholder is not None else ""
                if notes and kind in self._NOTES_SKIP:
                    continue
                text = self.text(_child(shape, "p", "txBody"))
                (titles if kind in ("title", "ctrTitle") else lines).extend(text)
            elif family == "p" and local == "grpSp":
                group_titles, group_lines = self.shapes(shape, notes)
                titles.extend(group_titles)
                lines.extend(group_lines)
            elif family == "p" and local == "graphicFrame":
                lines.extend(self.frame(shape))
            elif family == "p" and local in ("pic", "contentPart"):
                self.pictures += 1
            elif (family, local) == ("mc", "AlternateContent"):
                branch = _branch(shape)
                if branch is not None:
                    group_titles, group_lines = self.shapes(branch, notes)
                    titles.extend(group_titles)
                    lines.extend(group_lines)
            elif family == "p" and local.startswith(("nv", "grpSpPr", "extLst")):
                continue
            else:
                text = " ".join(_drawing_text(shape).split())
                if text:
                    lines.append(text)
        return titles, lines

    def text(self, body) -> list[str]:
        """Non-empty paragraphs of a text body."""
        if body is None:
            return []
        lines = []
        for paragraph in body:
            if _name(paragraph) == ("a", "p"):
                text = _drawing_text(paragraph)
                if text.strip():
                    lines.append(text)
        return lines

    def frame(self, frame) -> list[str]:
        """Tables, charts, SmartArt and embedded objects in a graphic frame."""
        data = _descendant(frame, "a", "graphicData")
        lines: list[str] = []
        for child in data if data is not None else ():
            family, local = _name(child)
            if family == "a" and local == "tbl":
                for row in (node for node in child if _name(node) == ("a", "tr")):
                    cells = [" ".join(self.text(_child(cell, "a", "txBody")))
                             for cell in row if _name(cell) == ("a", "tc")]
                    lines.append("\t".join(cells))
            elif local == "chart" and _attr(child, "r", "id"):
                lines.extend(_isolated(self.unread, "a chart", _chart_lines, self.package,
                                       self._target(_attr(child, "r", "id")), default=[]))
            elif (family, local) == ("dgm", "relIds"):
                lines.extend(_isolated(self.unread, "a SmartArt diagram", _diagram_lines, self.package,
                                       self._target(_attr(child, "r", "dm")), default=[]))
            elif local in ("oleObj", "oleObject"):
                self.objects += 1
            elif (family, local) == ("mc", "AlternateContent"):
                branch = _branch(child)
                if branch is not None and _descendant(branch, "p", "oleObj") is not None:
                    self.objects += 1
            else:
                text = " ".join(_drawing_text(child).split())
                if text:
                    lines.append(text)
        return lines


def _drawing_text(element) -> str:
    """DrawingML text with line breaks; one branch of alternate content only."""
    parts: list[str] = []

    def walk(node) -> None:
        for child in node:
            family, local = _name(child)
            if (family, local) in (("a", "t"), ("m", "t")):
                parts.append(child.text or "")
            elif (family, local) == ("a", "br"):
                parts.append("\n")
            elif (family, local) == ("mc", "AlternateContent"):
                branch = _branch(child)
                if branch is not None:
                    walk(branch)
            elif family == "a" and local.endswith("Pr"):
                continue
            else:
                walk(child)

    walk(element)
    return "".join(parts)


def _load_slide(package: _Package, target: str) -> tuple:
    return package.xml(target), _Slide(package, target)


def slides_text(data: bytes) -> Extracted:
    """Every slide in presentation order, with tables, charts, SmartArt, notes and comments."""
    package = _Package(data)
    main = package.main_part("ppt/presentation.xml")
    presentation = package.xml(main)
    if presentation is None:
        raise DocumentError("unreadable", "This PowerPoint file has no presentation part.")
    rels = package.rels(main)
    slide_list = _descendant(presentation, "p", "sldIdLst")
    targets = []
    for slide_id in slide_list if slide_list is not None else ():
        found = rels.get(_attr(slide_id, "r", "id") or "")
        if found and found[0] == "slide" and not found[2]:
            targets.append(found[1])
    if not targets:
        numbered = (name for name in package.names() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name))
        targets = sorted(numbered, key=lambda name: int(re.findall(r"\d+", name)[-1]))

    blocks, pictures, objects, hidden, unread, has_text = [], 0, 0, 0, [], False
    for number, target in enumerate(targets, 1):
        root, slide = _isolated(unread, f"slide {number}", _load_slide, package, target, default=(None, None))
        if root is None:
            if not unread or unread[-1] != f"slide {number}":
                unread.append(f"slide {number}")
            blocks.append(f"[Slide {number}]\n(this slide could not be read)")
            continue
        is_hidden = root.get("show") in ("0", "false")
        hidden += is_hidden
        tree = _descendant(root, "p", "spTree")
        shapes = _isolated(unread, f"slide {number}", slide.shapes, tree, default=None) if tree is not None else ([], [])
        if shapes is None:
            blocks.append(f"[Slide {number}]\n(this slide could not be read)")
            continue
        titles, lines = shapes
        parts = [f"[Slide {number}{' (hidden)' if is_hidden else ''}]", *titles, *lines]
        notes: list[str] = []
        comments: list[str] = []
        for kind, part, external in slide.rels.values():
            if external:
                continue
            if kind == "notesSlide":
                notes.extend(_isolated(unread, f"the notes of slide {number}", _notes_lines, package, part,
                                       default=[]))
            elif kind == "comments":
                comments.extend(_isolated(unread, f"the comments on slide {number}", _comment_lines, package, part,
                                          default=[]))
        if notes:
            parts += ["[Notes]", *notes]
        if comments:
            parts += ["[Comments]", *comments]
        has_text = has_text or len(parts) > 1
        pictures += slide.pictures
        objects += slide.objects
        unread.extend(slide.unread)
        blocks.append("\n".join(parts))

    if not has_text:
        raise DocumentError(
            "scanned_or_empty",
            "No readable text was found. The slides may contain only images; OCR is not supported.",
        )
    warnings = []
    if pictures:
        warnings.append(f"The slides contain {_plural(pictures, 'picture or media item')}; these are not "
                        "read (no OCR), and they may contain text.")
    if hidden:
        warnings.append(f"{_plural(hidden, 'hidden slide')} included and marked (hidden).")
    if objects:
        warnings.append(f"{_plural(objects, 'embedded object')} (for example a spreadsheet) could not be read.")
    errors = []
    if unread:
        errors.append("Could not read " + ", ".join(_unique(unread)) + "; skipped.")
    if package.damaged:
        errors.append("Damaged content in " + ", ".join(_unique(package.damaged))
                      + " was repaired; some of its text may be missing.")
    return Extracted(blocks, warnings, complete=not (objects or errors), units=len(targets), errors=errors)


def _notes_lines(package: _Package, part: str) -> list[str]:
    root = package.xml(part, required=True)
    tree = _descendant(root, "p", "spTree") if root is not None else None
    if tree is None:
        return []
    titles, lines = _Slide(package, part).shapes(tree, notes=True)
    return titles + lines


def _comment_lines(package: _Package, part: str) -> list[str]:
    root = package.xml(part, required=True)
    found = []
    for entry in root.iter() if root is not None else ():
        if _name(entry)[1] == "cm":
            text = " / ".join(_local_texts(entry, ("t", "text")))
            if text:
                found.append(f"- {text}")
    return found
