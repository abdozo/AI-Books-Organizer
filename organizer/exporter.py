from __future__ import annotations

import io
import zipfile
from datetime import datetime, timezone
from html import escape
from typing import Any


BOOK_EXPORT_FIELDS = (
    ("title", "العنوان"),
    ("author", "المؤلف"),
    ("editor", "المحقق"),
    ("publisher", "دار النشر"),
    ("publication_year", "سنة النشر"),
    ("edition_number", "رقم الطبعة"),
    ("volume_number", "رقم المجلد"),
)

FIELD_WEIGHTS = {
    "title": 3.5,
    "author": 2.5,
    "editor": 2.2,
    "publisher": 2.2,
    "publication_year": 1.6,
    "edition_number": 1.3,
    "volume_number": 1.2,
}


def _run(
    text: Any,
    *,
    bold: bool = False,
    size: int | None = None,
    color: str = "000000",
) -> str:
    properties = [
        "<w:rtl/>",
        '<w:rFonts w:ascii="Arial" w:hAnsi="Arial" w:cs="Arial"/>',
        '<w:lang w:val="ar-SA" w:bidi="ar-SA"/>',
        f'<w:color w:val="{color}"/>',
    ]
    if bold:
        properties.append("<w:b/>")
    if size:
        properties.append(f'<w:sz w:val="{size}"/><w:szCs w:val="{size}"/>')
    value = escape(str(text or ""))
    return f'<w:r><w:rPr>{"".join(properties)}</w:rPr><w:t xml:space="preserve">{value}</w:t></w:r>'


def _paragraph(
    text: Any,
    *,
    bold: bool = False,
    size: int | None = None,
    color: str = "000000",
    style: str | None = None,
) -> str:
    style_xml = f'<w:pStyle w:val="{style}"/>' if style else ""
    return (
        f'<w:p><w:pPr>{style_xml}<w:bidi/><w:jc w:val="start"/>'
        '<w:spacing w:after="0" w:line="276" w:lineRule="auto"/></w:pPr>'
        f'{_run(text, bold=bold, size=size, color=color)}</w:p>'
    )


def _cell(
    text: Any,
    *,
    width: int,
    header: bool = False,
    shade: str = "FFFFFF",
) -> str:
    return (
        f'<w:tc><w:tcPr><w:tcW w:w="{width}" w:type="dxa"/><w:vAlign w:val="center"/>'
        f'<w:shd w:val="clear" w:color="auto" w:fill="{shade}"/>'
        '<w:tcMar><w:top w:w="120" w:type="dxa"/><w:start w:w="130" w:type="dxa"/>'
        '<w:bottom w:w="120" w:type="dxa"/><w:end w:w="130" w:type="dxa"/></w:tcMar>'
        f'</w:tcPr>{_paragraph(text, bold=header, size=20, color="FFFFFF" if header else "000000")}</w:tc>'
    )


def _visible_fields(books: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [
        (key, label)
        for key, label in BOOK_EXPORT_FIELDS
        if key == "title" or any(str(book.get(key, "")).strip() for book in books)
    ]


def _column_widths(fields: list[tuple[str, str]]) -> dict[str, int]:
    available_width = 14_500
    index_width = 650
    content_width = available_width - index_width
    total_weight = sum(FIELD_WEIGHTS[key] for key, _label in fields)
    widths = {
        key: round(content_width * FIELD_WEIGHTS[key] / total_weight)
        for key, _label in fields
    }
    widths["_index"] = index_width
    return widths


def _books_table(books: list[dict[str, Any]]) -> str:
    fields = _visible_fields(books)
    widths = _column_widths(fields)
    grid = [widths["_index"], *(widths[key] for key, _label in fields)]
    grid_columns = "".join(f'<w:gridCol w:w="{width}"/>' for width in grid)
    header_cells = [_cell("م", width=widths["_index"], header=True, shade="243B5A")]
    header_cells.extend(
        _cell(label, width=widths[key], header=True, shade="243B5A")
        for key, label in fields
    )
    rows = [f'<w:tr><w:trPr><w:tblHeader/></w:trPr>{"".join(header_cells)}</w:tr>']
    for index, book in enumerate(books, 1):
        shade = "F2F6FA" if index % 2 == 0 else "FFFFFF"
        cells = [_cell(index, width=widths["_index"], shade=shade)]
        cells.extend(
            _cell(str(book.get(key, "") or "").strip(), width=widths[key], shade=shade)
            for key, _label in fields
        )
        rows.append(f'<w:tr><w:trPr><w:cantSplit/></w:trPr>{"".join(cells)}</w:tr>')
    return (
        '<w:tbl><w:tblPr><w:bidiVisual/><w:tblW w:w="14500" w:type="dxa"/>'
        '<w:tblLayout w:type="fixed"/><w:tblCellMar><w:top w:w="120" w:type="dxa"/>'
        '<w:start w:w="130" w:type="dxa"/><w:bottom w:w="120" w:type="dxa"/>'
        '<w:end w:w="130" w:type="dxa"/></w:tblCellMar>'
        '<w:tblBorders><w:top w:val="single" w:sz="4" w:color="D9D9D9"/>'
        '<w:left w:val="single" w:sz="4" w:color="D9D9D9"/>'
        '<w:bottom w:val="single" w:sz="4" w:color="D9D9D9"/>'
        '<w:right w:val="single" w:sz="4" w:color="D9D9D9"/>'
        '<w:insideH w:val="single" w:sz="4" w:color="D9D9D9"/>'
        '<w:insideV w:val="single" w:sz="4" w:color="D9D9D9"/>'
        f'</w:tblBorders></w:tblPr><w:tblGrid>{grid_columns}</w:tblGrid>{"".join(rows)}</w:tbl>'
    )


def build_books_docx(books: list[dict[str, Any]]) -> bytes:
    """Build a small RTL Word document without a heavyweight document dependency."""
    body = [
        _paragraph("فهرس الكتب", bold=True, size=36, style="Title"),
        _paragraph(f"عدد الكتب {len(books)}", size=20),
        _paragraph(""),
        _books_table(books),
    ]
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f'<w:body>{"".join(body)}<w:sectPr><w:bidi/><w:pgSz w:w="16838" w:h="11906" w:orient="landscape"/>'
        '<w:pgMar w:top="720" w:right="720" w:bottom="720" w:left="720"/></w:sectPr></w:body></w:document>'
    )
    content_types = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>'''
    relationships = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>'''
    timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    core = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<dc:title>فهرس الكتب</dc:title><dc:creator>منظم الكتب</dc:creator><dcterms:created xsi:type="dcterms:W3CDTF">{timestamp}</dcterms:created></cp:coreProperties>'''
    app = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"><Application>منظم الكتب</Application></Properties>'''
    styles = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Arial" w:hAnsi="Arial" w:cs="Arial"/><w:lang w:val="ar-SA" w:bidi="ar-SA"/></w:rPr></w:rPrDefault><w:pPrDefault><w:pPr><w:bidi/><w:jc w:val="start"/></w:pPr></w:pPrDefault></w:docDefaults>
<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/><w:pPr><w:bidi/><w:jc w:val="start"/></w:pPr></w:style>
<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/><w:pPr><w:bidi/><w:jc w:val="start"/><w:spacing w:after="180"/></w:pPr><w:rPr><w:b/><w:color w:val="000000"/><w:sz w:val="36"/><w:szCs w:val="36"/></w:rPr></w:style>
</w:styles>'''
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", relationships)
        archive.writestr("word/document.xml", document)
        archive.writestr("word/_rels/document.xml.rels", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
        archive.writestr("word/styles.xml", styles)
        archive.writestr("docProps/core.xml", core)
        archive.writestr("docProps/app.xml", app)
    return output.getvalue()
