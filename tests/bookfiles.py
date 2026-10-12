"""Small ebook files built in memory for the reader tests."""

from __future__ import annotations

import base64
import io
import zipfile

# A 1x1 PNG.
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

PARAS = [
    "The river ran low that summer, and the willows along its banks leaned over the water as if to drink.",
    "Every evening the wind came up from the valley and moved the leaves, and the shade climbed the hills.",
    "There was a path through the sycamores, beaten hard by boys coming down from the farms to swim.",
]


def chapter_xhtml(title: str, paras: list[str], extra: str = "") -> str:
    body = "".join(f"<p>{p}</p>" for p in paras)
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
        f'<head><title>{title}</title><style>p {{ color: red }}</style><script>alert(1)</script></head>'
        f'<body><section id="top"><h2 class="chapter">{title}</h2>{body}{extra}</section></body></html>'
    )


def make_epub(chapters: list[tuple[str, list[str]]] | None = None, title: str = "The Quiet River",
              author: str = "Anna Field", cover: bytes | None = PNG, nav: bool = True,
              extras: dict[int, str] | None = None) -> bytes:
    chapters = chapters or [(f"Chapter {i + 1}", PARAS * 3) for i in range(3)]
    extras = extras or {}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", (
            '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>'
        ))
        items, spine, navlis, ncxpts = [], [], [], []
        for i, (ct, paras) in enumerate(chapters):
            name = f"text/ch{i + 1}.xhtml"
            z.writestr(f"OEBPS/{name}", chapter_xhtml(ct, paras, extras.get(i, "")))
            items.append(f'<item id="ch{i + 1}" href="{name}" media-type="application/xhtml+xml"/>')
            spine.append(f'<itemref idref="ch{i + 1}"/>')
            navlis.append(f'<li><a href="{name}#top">{ct}</a></li>')
            ncxpts.append(f'<navPoint id="n{i}"><navLabel><text>{ct}</text></navLabel><content src="{name}"/></navPoint>')
        if cover:
            z.writestr("OEBPS/images/cover.png", cover)
            items.append('<item id="cover-img" href="images/cover.png" media-type="image/png" properties="cover-image"/>')
        z.writestr("OEBPS/images/pic.png", PNG)
        items.append('<item id="pic" href="images/pic.png" media-type="image/png"/>')
        if nav:
            z.writestr("OEBPS/nav.xhtml", (
                '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
                f'<body><nav epub:type="toc"><ol>{"".join(navlis)}</ol></nav></body></html>'
            ))
            items.append('<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>')
        else:
            z.writestr("OEBPS/toc.ncx", (
                '<?xml version="1.0"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
                f'<navMap>{"".join(ncxpts)}</navMap></ncx>'
            ))
            items.append('<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>')
        spine_attr = "" if nav else ' toc="ncx"'
        z.writestr("OEBPS/content.opf", (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            f'<dc:title>{title}</dc:title><dc:creator>{author}</dc:creator><dc:language>en</dc:language></metadata>'
            f'<manifest>{"".join(items)}</manifest>'
            f'<spine{spine_attr}>{"".join(spine)}</spine></package>'
        ))
    return buf.getvalue()


def make_fb2(title: str = "Тихая река", author: tuple[str, str] = ("Анна", "Полева")) -> bytes:
    pic = base64.b64encode(PNG).decode()
    return f"""<?xml version="1.0" encoding="utf-8"?>
<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0" xmlns:l="http://www.w3.org/1999/xlink">
<description><title-info>
  <author><first-name>{author[0]}</first-name><last-name>{author[1]}</last-name></author>
  <book-title>{title}</book-title><lang>ru</lang>
  <coverpage><image l:href="#cover.png"/></coverpage>
</title-info></description>
<body>
  <title><p>{title}</p></title>
  <section><title><p>Глава первая</p></title>
    <p>Река в то лето обмелела.<a l:href="#n1" type="note">1</a></p>
    <p><emphasis>Ивы</emphasis> склонялись к воде.</p>
    <image l:href="#pic.png"/>
  </section>
  <section><title><p>Глава вторая</p></title><p>Вечером поднимался ветер.</p></section>
</body>
<body name="notes"><section id="n1"><title><p>1</p></title><p>Примечание.</p></section></body>
<binary id="cover.png" content-type="image/png">{pic}</binary>
<binary id="pic.png" content-type="image/png">{pic}</binary>
</FictionBook>""".encode("utf-8")
