"""Build a Word (.docx) file from extracted markdown using only the standard library.

A .docx is a zip of XML parts. We emit the four parts Word needs, converting the
markdown subset the extractor produces: headings, paragraphs, lists, block quotes,
fenced code, GFM tables, and inline bold/italic/code.
"""

from __future__ import annotations

import io
import re
import zipfile
from typing import Iterable, Optional

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>"""

ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""

DOC_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""


def _heading_style(level: int, size_half_points: int) -> str:
    return f"""  <w:style w:type="paragraph" w:styleId="Heading{level}">
    <w:name w:val="heading {level}"/>
    <w:basedOn w:val="Normal"/>
    <w:pPr><w:keepNext/><w:spacing w:before="280" w:after="120"/><w:outlineLvl w:val="{level - 1}"/></w:pPr>
    <w:rPr><w:b/><w:sz w:val="{size_half_points}"/><w:szCs w:val="{size_half_points}"/></w:rPr>
  </w:style>"""


STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="{w}">
  <w:docDefaults>
    <w:rPrDefault><w:rPr>
      <w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri"/>
      <w:sz w:val="22"/><w:szCs w:val="22"/>
    </w:rPr></w:rPrDefault>
    <w:pPrDefault><w:pPr><w:spacing w:after="140" w:line="276" w:lineRule="auto"/></w:pPr></w:pPrDefault>
  </w:docDefaults>
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
{headings}
  <w:style w:type="paragraph" w:styleId="Quote">
    <w:name w:val="Quote"/><w:basedOn w:val="Normal"/>
    <w:pPr><w:ind w:left="480"/></w:pPr>
    <w:rPr><w:i/><w:color w:val="555555"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Code">
    <w:name w:val="HTML Preformatted"/><w:basedOn w:val="Normal"/>
    <w:pPr><w:spacing w:after="60"/><w:ind w:left="240"/></w:pPr>
    <w:rPr><w:rFonts w:ascii="Consolas" w:hAnsi="Consolas"/><w:sz w:val="19"/></w:rPr>
  </w:style>
</w:styles>""".format(
    w=W,
    headings="\n".join(
        _heading_style(level, size)
        for level, size in ((1, 40), (2, 32), (3, 26), (4, 23), (5, 22), (6, 22))
    ),
)


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


INLINE = re.compile(
    r"(\*\*.+?\*\*|__.+?__|(?<!\*)\*[^*\s].*?\*(?!\*)|(?<!_)_[^_\s].*?_(?!_)|`[^`]+`)"
)
LINK = re.compile(r"\[([^\]]*)\]\(([^)]*)\)")
# Markdown escapes a leading backslash before punctuation; the reader must drop it
# rather than print it (extracted forms are full of "\[x]" checkboxes).
ESCAPED = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|~>])")


def _unescape(text: str) -> str:
    return ESCAPED.sub(r"\1", text)


def _runs(text: str, *, bold: bool = False, mono: bool = False) -> str:
    """Convert one line of inline markdown into a sequence of <w:r> runs."""
    text = LINK.sub(lambda m: m.group(1) or m.group(2), text)
    out = []
    for part in INLINE.split(text):
        if not part:
            continue
        is_bold, is_italic, is_mono = bold, False, mono
        if (part.startswith("**") and part.endswith("**") and len(part) > 4) or (
            part.startswith("__") and part.endswith("__") and len(part) > 4
        ):
            part, is_bold = part[2:-2], True
        elif (part.startswith("*") and part.endswith("*") and len(part) > 2) or (
            part.startswith("_") and part.endswith("_") and len(part) > 2
        ):
            part, is_italic = part[1:-1], True
        elif part.startswith("`") and part.endswith("`") and len(part) > 2:
            part, is_mono = part[1:-1], True

        props = []
        if is_bold:
            props.append("<w:b/>")
        if is_italic:
            props.append("<w:i/>")
        if is_mono:
            props.append('<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas"/>')
        rpr = f"<w:rPr>{''.join(props)}</w:rPr>" if props else ""
        out.append(f'<w:r>{rpr}<w:t xml:space="preserve">{_esc(_unescape(part))}</w:t></w:r>')
    return "".join(out) or '<w:r><w:t xml:space="preserve"></w:t></w:r>'


def _para(text: str, *, style: Optional[str] = None, indent: int = 0, bold: bool = False,
          mono: bool = False) -> str:
    props = []
    if style:
        props.append(f'<w:pStyle w:val="{style}"/>')
    if indent:
        props.append(f'<w:ind w:left="{indent}" w:hanging="220"/>')
    ppr = f"<w:pPr>{''.join(props)}</w:pPr>" if props else ""
    return f"<w:p>{ppr}{_runs(text, bold=bold, mono=mono)}</w:p>"


def _split_row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [cell.strip() for cell in line.split("|")]


def _is_divider(line: str) -> bool:
    cells = _split_row(line)
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", c.replace(" ", "")) for c in cells)


def _table(rows: list[list[str]]) -> str:
    width = max(len(r) for r in rows)
    borders = "".join(
        f'<w:{edge} w:val="single" w:sz="4" w:space="0" w:color="D9D9D9"/>'
        for edge in ("top", "left", "bottom", "right", "insideH", "insideV")
    )
    out = [
        "<w:tbl><w:tblPr>"
        '<w:tblW w:w="5000" w:type="pct"/>'
        f"<w:tblBorders>{borders}</w:tblBorders>"
        '<w:tblCellMar><w:top w:w="60" w:type="dxa"/><w:bottom w:w="60" w:type="dxa"/>'
        '<w:left w:w="90" w:type="dxa"/><w:right w:w="90" w:type="dxa"/></w:tblCellMar>'
        "</w:tblPr>"
    ]
    for index, row in enumerate(rows):
        cells = row + [""] * (width - len(row))
        header = index == 0
        out.append("<w:tr>" + ('<w:trPr><w:tblHeader/></w:trPr>' if header else ""))
        for cell in cells:
            shade = '<w:shd w:val="clear" w:color="auto" w:fill="F3F1FA"/>' if header else ""
            out.append(
                f'<w:tc><w:tcPr><w:tcW w:w="{int(5000 / width)}" w:type="pct"/>{shade}'
                "<w:vAlign w:val=\"top\"/></w:tcPr>"
                f'<w:p><w:pPr><w:spacing w:after="0"/></w:pPr>{_runs(cell, bold=header)}</w:p></w:tc>'
            )
        out.append("</w:tr>")
    out.append("</w:tbl>")
    # Word needs a paragraph after a table to separate it from following content.
    out.append('<w:p><w:pPr><w:spacing w:after="0"/></w:pPr></w:p>')
    return "".join(out)


def _body(markdown: str) -> Iterable[str]:
    lines = (markdown or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()

        if not stripped:
            index += 1
            continue

        # Fenced code block
        if stripped.startswith("```"):
            index += 1
            while index < len(lines) and not lines[index].strip().startswith("```"):
                yield _para(lines[index], style="Code", mono=True)
                index += 1
            index += 1
            continue

        # Table: a header row followed by a --- divider
        if (
            "|" in stripped
            and index + 1 < len(lines)
            and "|" in lines[index + 1]
            and _is_divider(lines[index + 1])
        ):
            rows = [_split_row(stripped)]
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                rows.append(_split_row(lines[index]))
                index += 1
            yield _table(rows)
            continue

        # Horizontal rule
        if re.fullmatch(r"(\*\s*){3,}|(-\s*){3,}|(_\s*){3,}", stripped):
            yield (
                '<w:p><w:pPr><w:pBdr><w:bottom w:val="single" w:sz="6" w:space="1" '
                'w:color="D9D9D9"/></w:pBdr></w:pPr></w:p>'
            )
            index += 1
            continue

        heading = re.match(r"(#{1,6})\s+(.*)", stripped)
        if heading:
            level = len(heading.group(1))
            yield _para(heading.group(2), style=f"Heading{level}")
            index += 1
            continue

        if stripped.startswith(">"):
            yield _para(stripped.lstrip("> ").strip(), style="Quote")
            index += 1
            continue

        bullet = re.match(r"[-*+]\s+(.*)", stripped)
        if bullet:
            yield _para(f"•\t{bullet.group(1)}", indent=440)
            index += 1
            continue

        numbered = re.match(r"(\d+)[.)]\s+(.*)", stripped)
        if numbered:
            yield _para(f"{numbered.group(1)}.\t{numbered.group(2)}", indent=440)
            index += 1
            continue

        # Plain paragraph: join soft-wrapped continuation lines.
        buffer = [stripped]
        index += 1
        while index < len(lines):
            nxt = lines[index].strip()
            if not nxt or re.match(r"(#{1,6}\s|[-*+]\s|\d+[.)]\s|>|```)", nxt) or "|" in nxt:
                break
            buffer.append(nxt)
            index += 1
        yield _para(" ".join(buffer))


def build_docx(markdown: str, title: str = "") -> bytes:
    """Render markdown to .docx bytes."""
    blocks = list(_body(markdown))
    if not blocks:
        blocks = [_para("No content was extracted from this document.")]

    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{W}"><w:body>'
        + "".join(blocks)
        + '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134" '
        'w:header="708" w:footer="708" w:gutter="0"/></w:sectPr>'
        "</w:body></w:document>"
    )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("_rels/.rels", ROOT_RELS)
        archive.writestr("word/_rels/document.xml.rels", DOC_RELS)
        archive.writestr("word/styles.xml", STYLES)
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()
