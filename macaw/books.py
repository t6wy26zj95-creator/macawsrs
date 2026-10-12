"""Ebooks for the reader Mini App: EPUB, FB2 and plain text.

A book file is turned into one simple shape the reader can show: a title, an
author, a cover image, a table of contents, and a list of chapters made of
clean HTML (only plain formatting tags; no scripts, styles or outside links).
Pictures inside the book are written next to it and referenced as "res/<name>".

On disk each book is a folder: the original file, book.json, the cover and res/.
"""

from __future__ import annotations

import base64
import html
import io
import json
import posixpath
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET

EXTENSIONS = (".epub", ".fb2", ".fb2.zip", ".txt")
MAX_SIZE = 100 * 1024 * 1024
IMAGE_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif",
    ".webp": "image/webp", ".svg": "image/svg+xml", ".bmp": "image/bmp",
}


class BookError(Exception):
    """A file that can't be read as a book; the message is safe to show the user."""


@dataclass
class Chapter:
    html: str
    title: str = ""

    @property
    def chars(self) -> int:
        return len(_TAG.sub("", self.html))


@dataclass
class Book:
    title: str
    author: str = ""
    language: str = ""
    chapters: list[Chapter] = field(default_factory=list)
    # {"title", "c" (chapter index), "a" (anchor id or ""), "level"}
    toc: list[dict] = field(default_factory=list)
    cover: tuple[str, bytes] | None = None  # (file extension, data)
    resources: dict[str, bytes] = field(default_factory=dict)  # stored name -> data

    def to_json(self) -> dict:
        return {
            "title": self.title,
            "author": self.author,
            "language": self.language,
            "toc": self.toc,
            "chapters": [{"html": c.html, "title": c.title, "chars": c.chars} for c in self.chapters],
        }


def is_book_name(name: str) -> bool:
    return name.lower().endswith(EXTENSIONS)


def read_book(data: bytes, filename: str) -> Book:
    name = filename.lower()
    try:
        if name.endswith(".epub") or (data[:2] == b"PK" and _zip_has(data, "META-INF/container.xml")):
            book = _read_epub(data)
        elif name.endswith(".fb2.zip") or (name.endswith(".zip") and data[:2] == b"PK"):
            book = _read_fb2(_fb2_from_zip(data))
        elif name.endswith(".fb2") or data.lstrip()[:200].find(b"FictionBook") >= 0:
            book = _read_fb2(data)
        elif name.endswith(".txt"):
            book = _read_txt(data, filename)
        else:
            raise BookError("I can open EPUB, FB2 and TXT books.")
    except BookError:
        raise
    except (zipfile.BadZipFile, ET.ParseError, KeyError, UnicodeDecodeError) as e:
        raise BookError(f"That file looks damaged, so I couldn't open it ({type(e).__name__}).") from e
    book.chapters = [c for c in book.chapters if c.html.strip()]
    if not book.chapters:
        raise BookError("That book has no text I can show.")
    if not book.title.strip():
        book.title = _title_from_filename(filename)
    if not book.toc:
        book.toc = [{"title": c.title or f"Chapter {i + 1}", "c": i, "a": "", "level": 0}
                    for i, c in enumerate(book.chapters) if c.title] or [{"title": "Start", "c": 0, "a": "", "level": 0}]
    return book


def save_book(book: Book, folder: Path, original: bytes, filename: str) -> str | None:
    """Writes the book folder. Returns the cover file name, if there is one."""
    if folder.exists():
        shutil.rmtree(folder)
    (folder / "res").mkdir(parents=True)
    ext = ".fb2.zip" if filename.lower().endswith(".fb2.zip") else (Path(filename).suffix.lower() or ".bin")
    (folder / f"original{ext}").write_bytes(original)
    for res_name, res_data in book.resources.items():
        (folder / "res" / res_name).write_bytes(res_data)
    (folder / "book.json").write_text(json.dumps(book.to_json(), ensure_ascii=False))
    if book.cover:
        cover_name = "cover" + book.cover[0]
        (folder / cover_name).write_bytes(book.cover[1])
        return cover_name
    return None


def _title_from_filename(filename: str) -> str:
    stem = re.sub(r"(\.fb2)?\.(epub|fb2|txt|zip)$", "", Path(filename).name, flags=re.I)
    return re.sub(r"[_]+", " ", stem).strip() or "Untitled"


def _zip_has(data: bytes, member: str) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return member in z.namelist()
    except zipfile.BadZipFile:
        return False


# ======================= HTML cleaning =======================

_TAG = re.compile(r"<[^>]+>")
KEEP = {
    "p", "div", "span", "h1", "h2", "h3", "h4", "h5", "h6", "em", "i", "b", "strong", "u", "s", "del",
    "sub", "sup", "small", "big", "br", "hr", "blockquote", "ul", "ol", "li", "a", "img", "pre", "code",
    "table", "thead", "tbody", "tfoot", "tr", "td", "th", "caption", "figure", "figcaption", "section",
    "article", "aside", "header", "footer", "cite", "q", "dl", "dt", "dd", "abbr", "mark",
}
VOID = {"br", "hr", "img"}
DROP_WITH_CONTENT = {"head", "script", "style", "title", "noscript", "template", "iframe", "object", "video", "audio"}
BLOCKISH = {"section": "div", "article": "div", "header": "div", "footer": "div", "aside": "div"}


class _Cleaner(HTMLParser):
    """Keeps only harmless formatting. Links and pictures are rewritten by the
    callbacks so they point inside the book."""

    def __init__(self, id_prefix: str, image_for, link_for):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.stack: list[str] = []
        self.drop = 0
        self.in_body = False
        self.saw_body = False
        self.id_prefix = id_prefix
        self.image_for = image_for
        self.link_for = link_for
        self.first_heading = ""
        self._heading: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "body":
            self.in_body = self.saw_body = True
            return
        if tag in DROP_WITH_CONTENT:
            self.drop += 1
            return
        if self.drop:
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "image":  # a picture inside an <svg>, often the cover page
            tag, a = "img", {"src": a.get("xlink:href") or a.get("href", "")}
        if tag not in KEEP:
            if a.get("id"):  # keep anchors that links and the contents point at
                self.out.append(f'<span id="{self.id_prefix}{html.escape(a["id"], quote=True)}"></span>')
            return
        out_tag = BLOCKISH.get(tag, tag)
        parts = [out_tag]
        if a.get("id"):
            parts.append(f'id="{self.id_prefix}{html.escape(a["id"], quote=True)}"')
        if tag == "img":
            src = self.image_for(a.get("src", ""))
            if not src:
                return
            parts.append(f'src="{html.escape(src, quote=True)}"')
            if a.get("alt"):
                parts.append(f'alt="{html.escape(a["alt"], quote=True)}"')
        elif tag == "a":
            for k, v in self.link_for(a.get("href", "")).items():
                parts.append(f'{k}="{html.escape(v, quote=True)}"')
        elif tag in ("td", "th"):
            for k in ("colspan", "rowspan"):
                if a.get(k, "").isdigit():
                    parts.append(f'{k}="{a[k]}"')
        elif tag in ("p", "div") and _is_centered(a):
            parts.append('class="center"')
        self.out.append("<" + " ".join(parts) + ">")
        if tag in ("h1", "h2", "h3") and not self.first_heading and self._heading is None:
            self._heading = []
        if out_tag not in VOID:
            self.stack.append(out_tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        tag = BLOCKISH.get(tag.lower(), tag.lower())
        if tag not in VOID and self.stack and self.stack[-1] == tag:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "body":
            self.in_body = False
            return
        if tag in DROP_WITH_CONTENT:
            self.drop = max(0, self.drop - 1)
            return
        if self.drop:
            return
        tag = BLOCKISH.get(tag, tag)
        if tag not in KEEP and tag not in BLOCKISH.values():
            return
        if tag in self.stack:
            while self.stack:
                t = self.stack.pop()
                self.out.append(f"</{t}>")
                if t == tag:
                    break
        if tag in ("h1", "h2", "h3") and self._heading is not None:
            self.first_heading = re.sub(r"\s+", " ", "".join(self._heading)).strip()
            self._heading = None

    def handle_data(self, data):
        if self.drop or (self.saw_body and not self.in_body):
            return
        self.out.append(html.escape(data, quote=False))
        if self._heading is not None:
            self._heading.append(data)

    def result(self) -> str:
        while self.stack:
            self.out.append(f"</{self.stack.pop()}>")
        return "".join(self.out).strip()


def _is_centered(attrs: dict) -> bool:
    return "center" in attrs.get("class", "").lower() or "text-align:center" in attrs.get("style", "").replace(" ", "").lower()


def clean_html(markup: str, id_prefix: str = "", image_for=lambda s: "", link_for=lambda h: {}) -> tuple[str, str]:
    """Returns (clean HTML, first heading text)."""
    # Drop the XML prolog and doctype; HTMLParser reads the rest of XHTML fine.
    markup = re.sub(r"^\s*<\?xml[^>]*\?>", "", markup)
    p = _Cleaner(id_prefix, image_for, link_for)
    p.feed(markup)
    p.close()
    body = p.result()
    # Empty wrappers left after dropping things.
    body = re.sub(r"<(span|div|p)>\s*</\1>", "", body)
    return body, p.first_heading


# ======================= EPUB =======================

NS = {
    "c": "urn:oasis:names:tc:opendocument:xmlns:container",
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
    "ncx": "http://www.daisy.org/z3986/2005/ncx/",
    "x": "http://www.w3.org/1999/xhtml",
    "epub": "http://www.idpf.org/2007/ops",
}


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else ("utf-8-sig",):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    m = re.search(rb'encoding=["\']([\w-]+)', raw[:200])
    try:
        return raw.decode(m.group(1).decode() if m else "cp1252", errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _resolve(base_dir: str, href: str) -> str:
    path = unquote(urlsplit(href).path)
    return posixpath.normpath(posixpath.join(base_dir, path)) if path else ""


def _read_epub(data: bytes) -> Book:
    z = zipfile.ZipFile(io.BytesIO(data))
    names = set(z.namelist())
    try:
        container = ET.fromstring(z.read("META-INF/container.xml"))
        opf_path = container.find(".//c:rootfile", NS).get("full-path")
    except (KeyError, AttributeError):
        opf_path = next((n for n in names if n.lower().endswith(".opf")), None)
    if not opf_path or opf_path not in names:
        raise BookError("That EPUB is missing its table of parts, so I couldn't open it.")
    opf = ET.fromstring(z.read(opf_path))
    opf_dir = posixpath.dirname(opf_path)

    def text(path: str) -> str:
        el = opf.find(path, NS)
        return (el.text or "").strip() if el is not None and el.text else ""

    title = text(".//dc:title")
    authors = [(el.text or "").strip() for el in opf.findall(".//dc:creator", NS) if el.text]
    language = text(".//dc:language")

    manifest: dict[str, dict] = {}
    for item in opf.findall(".//opf:manifest/opf:item", NS):
        manifest[item.get("id", "")] = {
            "path": _resolve(opf_dir, item.get("href", "")),
            "type": item.get("media-type", ""),
            "props": item.get("properties", ""),
        }
    spine_el = opf.find(".//opf:spine", NS)
    spine = [manifest[i.get("idref")]["path"] for i in opf.findall(".//opf:spine/opf:itemref", NS)
             if i.get("idref") in manifest]
    spine = [p for p in spine if p in names]
    chapter_of = {p: n for n, p in enumerate(spine)}

    book = Book(title=title, author=", ".join(a for a in authors if a), language=language)

    # Pictures: stored once each under a short name.
    stored: dict[str, str] = {}

    def image_for(base_dir: str, src: str) -> str:
        if not src or src.startswith(("http:", "https:", "data:")):
            return ""
        path = _resolve(base_dir, src)
        if path not in names:
            return ""
        if path not in stored:
            ext = posixpath.splitext(path)[1].lower()
            if ext not in IMAGE_TYPES:
                return ""
            name = f"{len(stored) + 1}{ext}"
            book.resources[name] = z.read(path)
            stored[path] = name
        return "res/" + stored[path]

    def link_for(base_dir: str, href: str) -> dict:
        if href.startswith(("http:", "https:", "mailto:")):
            return {"data-ext": href}
        parts = urlsplit(href)
        target = _resolve(base_dir, href) if parts.path else None
        c = chapter_of.get(target) if target else None
        if parts.path and c is None:
            return {}
        out = {"data-a": f"c{c}-{parts.fragment}" if c is not None and parts.fragment else ""}
        if c is not None:
            out["data-c"] = str(c)
        return {k: v for k, v in out.items() if v}

    for n, path in enumerate(spine):
        base_dir = posixpath.dirname(path)
        body, heading = clean_html(
            _decode(z.read(path)),
            id_prefix=f"c{n}-",
            image_for=lambda s, b=base_dir: image_for(b, s),
            link_for=lambda h, b=base_dir, n=n: _fix_same_chapter(link_for(b, h), n, h),
        )
        book.chapters.append(Chapter(html=body, title=heading))

    # Table of contents: EPUB 3 nav first, then the older NCX.
    nav = next((m for m in manifest.values() if "nav" in m["props"].split()), None)
    if nav and nav["path"] in names:
        book.toc = _nav_toc(z.read(nav["path"]), posixpath.dirname(nav["path"]), chapter_of)
    if not book.toc:
        ncx_id = spine_el.get("toc") if spine_el is not None else None
        ncx = manifest.get(ncx_id or "") or next((m for m in manifest.values() if m["type"] == "application/x-dtbncx+xml"), None)
        if ncx and ncx["path"] in names:
            book.toc = _ncx_toc(z.read(ncx["path"]), posixpath.dirname(ncx["path"]), chapter_of)

    cover_path = _epub_cover(opf, manifest, names)
    if cover_path:
        ext = posixpath.splitext(cover_path)[1].lower()
        if ext in IMAGE_TYPES and ext != ".svg":
            book.cover = (".jpg" if ext == ".jpeg" else ext, z.read(cover_path))
    return book


def _fix_same_chapter(link: dict, n: int, href: str) -> dict:
    # "#note1" inside the same file: point at this chapter.
    if href.startswith("#") and href[1:]:
        return {"data-c": str(n), "data-a": f"c{n}-{href[1:]}"}
    return link


def _epub_cover(opf, manifest: dict, names: set) -> str | None:
    for m in manifest.values():
        if "cover-image" in m["props"].split() and m["path"] in names:
            return m["path"]
    meta = opf.find(".//opf:metadata/opf:meta[@name='cover']", NS)
    if meta is not None and meta.get("content") in manifest:
        path = manifest[meta.get("content")]["path"]
        if path in names and manifest[meta.get("content")]["type"].startswith("image/"):
            return path
    for m in manifest.values():
        if m["type"].startswith("image/") and "cover" in posixpath.basename(m["path"]).lower() and m["path"] in names:
            return m["path"]
    return None


def _toc_entry(title: str, href: str, base_dir: str, chapter_of: dict, level: int) -> dict | None:
    parts = urlsplit(href)
    c = chapter_of.get(_resolve(base_dir, href))
    title = re.sub(r"\s+", " ", title).strip()
    if c is None or not title:
        return None
    return {"title": title, "c": c, "a": f"c{c}-{parts.fragment}" if parts.fragment else "", "level": level}


def _nav_toc(raw: bytes, base_dir: str, chapter_of: dict) -> list[dict]:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    navs = root.iter(f"{{{NS['x']}}}nav")
    toc_nav = None
    for nav in navs:
        if nav.get(f"{{{NS['epub']}}}type", "") == "toc" or toc_nav is None:
            toc_nav = nav
    if toc_nav is None:
        return []
    out: list[dict] = []

    def walk(ol, level):
        for li in ol.findall(f"{{{NS['x']}}}li"):
            a = li.find(f"{{{NS['x']}}}a")
            if a is not None and a.get("href"):
                e = _toc_entry("".join(a.itertext()), a.get("href"), base_dir, chapter_of, level)
                if e:
                    out.append(e)
            sub = li.find(f"{{{NS['x']}}}ol")
            if sub is not None:
                walk(sub, level + 1)

    ol = toc_nav.find(f"{{{NS['x']}}}ol")
    if ol is not None:
        walk(ol, 0)
    return out


def _ncx_toc(raw: bytes, base_dir: str, chapter_of: dict) -> list[dict]:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    out: list[dict] = []

    def walk(parent, level):
        for point in parent.findall("ncx:navPoint", NS):
            label = point.find("ncx:navLabel/ncx:text", NS)
            content = point.find("ncx:content", NS)
            if label is not None and content is not None:
                e = _toc_entry(label.text or "", content.get("src", ""), base_dir, chapter_of, level)
                if e:
                    out.append(e)
            walk(point, level + 1)

    nav_map = root.find("ncx:navMap", NS)
    if nav_map is not None:
        walk(nav_map, 0)
    return out


# ======================= FB2 =======================

FB = "http://www.gribuser.ru/xml/fictionbook/2.0"
XLINK = "http://www.w3.org/1999/xlink"


def _fb2_from_zip(data: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        name = next((n for n in z.namelist() if n.lower().endswith(".fb2")), None)
        if not name:
            raise BookError("That zip file has no .fb2 book inside.")
        return z.read(name)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _read_fb2(data: bytes) -> Book:
    root = ET.fromstring(data)
    info = next((el for el in root.iter() if _local(el.tag) == "title-info"), None)
    title, author, language, cover_id = "", "", "", ""
    if info is not None:
        for el in info:
            name = _local(el.tag)
            if name == "book-title":
                title = "".join(el.itertext()).strip()
            elif name == "author" and not author:
                parts = {_local(p.tag): "".join(p.itertext()).strip() for p in el}
                author = " ".join(x for x in (parts.get("first-name"), parts.get("middle-name"), parts.get("last-name")) if x) \
                    or parts.get("nickname", "")
            elif name == "lang":
                language = (el.text or "").strip()
            elif name == "coverpage":
                img = next((i for i in el.iter() if _local(i.tag) == "image"), None)
                if img is not None:
                    cover_id = (img.get(f"{{{XLINK}}}href") or img.get("href") or "").lstrip("#")

    binaries: dict[str, tuple[str, bytes]] = {}
    for el in root:
        if _local(el.tag) == "binary" and el.get("id"):
            ctype = el.get("content-type", "image/jpeg")
            ext = next((e for e, t in IMAGE_TYPES.items() if t == ctype), ".jpg")
            try:
                binaries[el.get("id")] = (ext, base64.b64decode("".join(el.itertext()), validate=False))
            except (ValueError, TypeError):
                pass

    book = Book(title=title, author=author, language=language)
    if cover_id in binaries:
        book.cover = binaries[cover_id]

    stored: dict[str, str] = {}

    def image(el) -> str:
        ref = (el.get(f"{{{XLINK}}}href") or el.get("href") or "").lstrip("#")
        if ref not in binaries:
            return ""
        if ref not in stored:
            ext, raw = binaries[ref]
            name = f"{len(stored) + 1}{ext}"
            book.resources[name] = raw
            stored[ref] = name
        return f'<img src="res/{stored[ref]}">'

    bodies = [el for el in root if _local(el.tag) == "body"]
    main = [b for b in bodies if b.get("name") not in ("notes", "comments", "footnotes")]
    notes = [b for b in bodies if b.get("name") in ("notes", "comments", "footnotes")]
    note_chapter: dict[str, int] = {}

    def render(el, level: int, ch: int) -> str:
        name = _local(el.tag)
        inner = (html.escape(el.text or "", quote=False)
                 + "".join(render(child, level, ch) + html.escape(child.tail or "", quote=False) for child in el))
        ident = el.get("id")
        id_attr = f' id="c{ch}-{html.escape(ident, quote=True)}"' if ident else ""
        if name == "p":
            return f"<p{id_attr}>{inner}</p>"
        if name == "title":
            h = min(level + 1, 6)
            return f"<h{h}{id_attr}>{inner}</h{h}>"
        if name in ("subtitle",):
            return f'<p class="center"{id_attr}><b>{inner}</b></p>'
        if name == "empty-line":
            return "<br>"
        if name in ("emphasis",):
            return f"<em>{inner}</em>"
        if name == "strong":
            return f"<strong>{inner}</strong>"
        if name == "strikethrough":
            return f"<s>{inner}</s>"
        if name in ("sup", "sub", "code"):
            return f"<{name}>{inner}</{name}>"
        if name == "a":
            href = el.get(f"{{{XLINK}}}href") or el.get("href") or ""
            if href.startswith("#"):
                target = href[1:]
                return f'<a data-note="{html.escape(target, quote=True)}">{inner}</a>'
            if href.startswith(("http:", "https:")):
                return f'<a data-ext="{html.escape(href, quote=True)}">{inner}</a>'
            return inner
        if name == "image":
            return image(el)
        if name in ("epigraph", "cite"):
            return f'<blockquote{id_attr}>{inner}</blockquote>'
        if name in ("poem", "stanza"):
            return f'<div class="poem"{id_attr}>{inner}</div>'
        if name == "v":
            return f"<p class=\"verse\">{inner}</p>"
        if name == "text-author":
            return f'<p class="right"><em>{inner}</em></p>'
        if name == "section":
            return f"<div{id_attr}>{inner}</div>"
        if name == "table":
            return f"<table>{inner}</table>"
        if name in ("tr", "td", "th"):
            return f"<{name}>{inner}</{name}>"
        return inner

    def section_title(sec) -> str:
        t = next((c for c in sec if _local(c.tag) == "title"), None)
        return re.sub(r"\s+", " ", " ".join("".join(p.itertext()) for p in t)).strip() if t is not None else ""

    for body in main:
        # Text before the first section (the book's own title, an epigraph).
        intro = [c for c in body if _local(c.tag) != "section"]
        sections = [c for c in body if _local(c.tag) == "section"]
        if intro and any("".join(c.itertext()).strip() for c in intro):
            ch = len(book.chapters)
            book.chapters.append(Chapter(html="".join(render(c, 0, ch) for c in intro)))
        for sec in sections:
            ch = len(book.chapters)
            book.chapters.append(Chapter(html=render(sec, 0, ch), title=section_title(sec)))
            if section_title(sec):
                book.toc.append({"title": section_title(sec), "c": ch, "a": "", "level": 0})
    for body in notes:
        ch = len(book.chapters)
        for el in body.iter():
            if el.get("id"):
                note_chapter[el.get("id")] = ch
        book.chapters.append(Chapter(html=render(body, 0, ch), title="Notes"))
        book.toc.append({"title": "Notes", "c": ch, "a": "", "level": 0})

    # Footnote links now know which chapter their note is in.
    def fix_note(m: re.Match) -> str:
        target = html.unescape(m.group(1))
        ch = note_chapter.get(target)
        if ch is None:
            return "<a>"
        return f'<a data-c="{ch}" data-a="c{ch}-{html.escape(target, quote=True)}">'

    for c in book.chapters:
        c.html = re.sub(r'<a data-note="([^"]*)">', fix_note, c.html)
    return book


# ======================= plain text =======================

CHAPTER_LINE = re.compile(
    r"^\s*((chapter|part|book|глава|часть|prologue|epilogue|пролог|эпилог)\b[^\n]{0,60}|[IVXLC]+\.?|\d{1,3}\.?)\s*$",
    re.I,
)


def _read_txt(data: bytes, filename: str) -> Book:
    for enc in ("utf-8-sig", "cp1251"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = data.decode("latin-1")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Paragraphs: blank-line separated, or one per line when the file has no blank lines.
    paras = [p.strip() for p in re.split(r"\n\s*\n", text)] if "\n\n" in text else [ln.strip() for ln in text.split("\n")]
    paras = [re.sub(r"\s*\n\s*", " ", p) for p in paras if p.strip()]
    book = Book(title=_title_from_filename(filename))
    current: list[str] = []
    title = ""
    size = 0

    def flush():
        nonlocal current, title, size
        if current:
            book.chapters.append(Chapter(html="".join(current), title=title))
            if title:
                book.toc.append({"title": title, "c": len(book.chapters) - 1, "a": "", "level": 0})
        current, title, size = [], "", 0

    for p in paras:
        if CHAPTER_LINE.match(p) and len(p) < 70:
            flush()
            title = p
            current.append(f"<h2>{html.escape(p)}</h2>")
            continue
        current.append(f"<p>{html.escape(p)}</p>")
        size += len(p)
        if size > 60000:  # no chapter headings: cut long text into parts
            flush()
    flush()
    return book
