"""Parsers for .docx and .md files. Returns (title, html_body) tuples."""

from __future__ import annotations

import html
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


def w(tag: str) -> str:
    return f"{{{W_NS}}}{tag}"


def r_attr(tag: str) -> str:
    return f"{{{R_NS}}}{tag}"


def _load_docx_rels(z: zipfile.ZipFile) -> dict[str, str]:
    rels_path = "word/_rels/document.xml.rels"
    if rels_path not in z.namelist():
        return {}
    try:
        root = ET.fromstring(z.read(rels_path))
    except ET.ParseError:
        return {}
    out: dict[str, str] = {}
    for rel in root.findall(f"{{{PKG_NS}}}Relationship"):
        rid = rel.get("Id")
        target = rel.get("Target")
        if rid and target:
            out[rid] = target
    return out


# --------------------------------------------------------------------------- #
# .docx
# --------------------------------------------------------------------------- #

def parse_docx(path: Path) -> tuple[str, str]:
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml")
        rels = _load_docx_rels(z)
    root = ET.fromstring(xml)
    body = root.find(w("body"))

    title: str | None = None
    parts: list[str] = []
    open_list = False

    def close_list() -> None:
        nonlocal open_list
        if open_list:
            parts.append("</ul>")
            open_list = False

    for child in body:
        if child.tag == w("tbl"):
            close_list()
            parts.append(_render_docx_table(child, rels))
            continue
        if child.tag != w("p"):
            continue

        p = child
        style = ""
        pPr = p.find(w("pPr"))
        is_list = False
        if pPr is not None:
            pStyle = pPr.find(w("pStyle"))
            if pStyle is not None:
                style = pStyle.get(w("val"), "")
            is_list = pPr.find(w("numPr")) is not None

        text_html = _render_docx_runs(p, rels)
        text_plain = re.sub(r"<[^>]+>", "", text_html).strip()

        if not text_plain:
            close_list()
            continue

        if style == "Heading1" and title is None:
            title = text_plain
            continue

        if is_list:
            if not open_list:
                parts.append("<ul>")
                open_list = True
            parts.append(f"<li>{text_html}</li>")
            continue

        close_list()
        if style == "Heading2":
            parts.append(f"<h2>{text_html}</h2>")
        elif style == "Heading3":
            parts.append(f"<h3>{text_html}</h3>")
        elif style.startswith("Heading"):
            parts.append(f"<h4>{text_html}</h4>")
        else:
            parts.append(f"<p>{text_html}</p>")

    close_list()

    if title is None:
        title = path.stem.replace("_", " ").strip()

    html_body = "\n".join(parts)
    html_body = promote_faq_questions(html_body)
    return title, html_body


def _render_docx_table(tbl, rels: dict[str, str]) -> str:
    rows = tbl.findall(w("tr"))
    if not rows:
        return ""

    def cell_html(tc) -> str:
        cell_parts = []
        for p in tc.findall(w("p")):
            inner = _render_docx_runs(p, rels)
            if re.sub(r"<[^>]+>", "", inner).strip():
                cell_parts.append(inner)
        return "<br>".join(cell_parts)

    out = ["<table>"]
    head_cells = [cell_html(tc) for tc in rows[0].findall(w("tc"))]
    out.append(
        "<thead><tr>" + "".join(f"<th>{c}</th>" for c in head_cells) + "</tr></thead>"
    )
    if len(rows) > 1:
        out.append("<tbody>")
        for tr in rows[1:]:
            cells = [cell_html(tc) for tc in tr.findall(w("tc"))]
            out.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
        out.append("</tbody>")
    out.append("</table>")
    return "".join(out)


def _render_docx_runs(paragraph, rels: dict[str, str]) -> str:
    pieces = []
    for child in paragraph:
        if child.tag == w("hyperlink"):
            inner = "".join(_render_docx_run(rn) for rn in child.findall(w("r")))
            if not inner:
                continue
            rid = child.get(r_attr("id"))
            anchor = child.get(w("anchor"))
            href = rels.get(rid) if rid else None
            if href and anchor:
                href = f"{href}#{anchor}"
            elif not href and anchor:
                href = f"#{anchor}"
            if href:
                pieces.append(f'<a href="{html.escape(href, quote=True)}">{inner}</a>')
            else:
                pieces.append(inner)
        elif child.tag == w("r"):
            pieces.append(_render_docx_run(child))
    return "".join(pieces)


def _render_docx_run(run) -> str:
    bold = italic = False
    rPr = run.find(w("rPr"))
    if rPr is not None:
        if rPr.find(w("b")) is not None:
            bold = True
        if rPr.find(w("i")) is not None:
            italic = True
    text = "".join(t.text or "" for t in run.findall(w("t")))
    if not text:
        return ""
    out = html.escape(text, quote=False)
    if bold:
        out = f"<strong>{out}</strong>"
    if italic:
        out = f"<em>{out}</em>"
    return out


# --------------------------------------------------------------------------- #
# FAQ promotion (shared between docx and md)
# --------------------------------------------------------------------------- #

_FAQ_PATTERN = re.compile(
    r"<p><strong>\s*Q[:\.]?\s*(?P<q>.+?)\s*</strong>\s*A[:\.]?\s*(?P<a>.+?)</p>",
    re.S | re.I,
)


def promote_faq_questions(html_body: str) -> str:
    """`<p><strong>Q: question?</strong> A: answer</p>` → `<h3>question?</h3><p>answer</p>`."""

    def repl(m: re.Match) -> str:
        q = m.group("q").strip()
        a = m.group("a").strip()
        return f"<h3>{q}</h3>\n<p>{a}</p>"

    return _FAQ_PATTERN.sub(repl, html_body)


# --------------------------------------------------------------------------- #
# .md
# --------------------------------------------------------------------------- #

_MD_INLINE_PATTERNS = [
    (re.compile(r"\*\*(.+?)\*\*"), r"<strong>\1</strong>"),
    (re.compile(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)"), r"<em>\1</em>"),
    # `_italic_`: only when both sides are non-word-chars (so `meta_title` stays intact)
    (re.compile(r"(?<![A-Za-z0-9_])_(?!\s)(.+?)(?<!\s)_(?![A-Za-z0-9_])"), r"<em>\1</em>"),
    (re.compile(r"`([^`]+)`"), r"<code>\1</code>"),
    (re.compile(r"\[([^\]]+)\]\(([^)]+)\)"), r'<a href="\2">\1</a>'),
]


def _md_inline(text: str) -> str:
    out = html.escape(text, quote=False)
    for pat, repl in _MD_INLINE_PATTERNS:
        out = pat.sub(repl, out)
    return out


def parse_md(path: Path) -> tuple[str, str]:
    raw = path.read_text(encoding="utf-8")
    lines = raw.splitlines()

    title: str | None = None
    parts: list[str] = []
    i = 0
    n = len(lines)

    def flush_paragraph(buf: list[str]) -> None:
        text = " ".join(s.strip() for s in buf).strip()
        if text:
            parts.append(f"<p>{_md_inline(text)}</p>")

    paragraph_buf: list[str] = []

    while i < n:
        line = lines[i]
        stripped = line.strip()

        # blank line ends paragraph
        if not stripped:
            flush_paragraph(paragraph_buf)
            paragraph_buf = []
            i += 1
            continue

        # ATX headings
        m = re.match(r"^(#{1,6})\s+(.*?)\s*#*$", stripped)
        if m:
            flush_paragraph(paragraph_buf)
            paragraph_buf = []
            level = len(m.group(1))
            text = _md_inline(m.group(2))
            if level == 1 and title is None:
                title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
            else:
                parts.append(f"<h{level}>{text}</h{level}>")
            i += 1
            continue

        # GFM tables: header row, then a separator like |---|---|, then body
        if "|" in stripped and i + 1 < n and re.match(r"^\s*\|?\s*:?-+", lines[i + 1].strip()):
            flush_paragraph(paragraph_buf)
            paragraph_buf = []
            i = _consume_md_table(lines, i, parts)
            continue

        # bullet lists
        if re.match(r"^[-*+]\s+", stripped):
            flush_paragraph(paragraph_buf)
            paragraph_buf = []
            i = _consume_md_list(lines, i, parts, ordered=False)
            continue

        # ordered lists
        if re.match(r"^\d+\.\s+", stripped):
            flush_paragraph(paragraph_buf)
            paragraph_buf = []
            i = _consume_md_list(lines, i, parts, ordered=True)
            continue

        # default: part of a paragraph
        paragraph_buf.append(line)
        i += 1

    flush_paragraph(paragraph_buf)

    if title is None:
        title = path.stem.replace("_", " ").replace("-", " ").strip()

    body = "\n".join(parts)
    body = promote_faq_questions(body)
    return title, body


def _consume_md_list(lines: list[str], i: int, parts: list[str], ordered: bool) -> int:
    tag = "ol" if ordered else "ul"
    pattern = re.compile(r"^\d+\.\s+(.*)") if ordered else re.compile(r"^[-*+]\s+(.*)")
    items = []
    while i < len(lines):
        stripped = lines[i].strip()
        m = pattern.match(stripped)
        if not m:
            break
        items.append(f"<li>{_md_inline(m.group(1))}</li>")
        i += 1
    parts.append(f"<{tag}>{''.join(items)}</{tag}>")
    return i


def _consume_md_table(lines: list[str], i: int, parts: list[str]) -> int:
    def split_row(raw: str) -> list[str]:
        s = raw.strip()
        if s.startswith("|"):
            s = s[1:]
        if s.endswith("|"):
            s = s[:-1]
        return [c.strip() for c in s.split("|")]

    header = split_row(lines[i])
    i += 2  # skip header + separator
    body_rows: list[list[str]] = []
    while i < len(lines):
        s = lines[i].strip()
        if not s or "|" not in s:
            break
        body_rows.append(split_row(lines[i]))
        i += 1

    out = ["<table>"]
    out.append(
        "<thead><tr>" + "".join(f"<th>{_md_inline(c)}</th>" for c in header) + "</tr></thead>"
    )
    if body_rows:
        out.append("<tbody>")
        for row in body_rows:
            out.append("<tr>" + "".join(f"<td>{_md_inline(c)}</td>" for c in row) + "</tr>")
        out.append("</tbody>")
    out.append("</table>")
    parts.append("".join(out))
    return i


# --------------------------------------------------------------------------- #
# Dispatcher
# --------------------------------------------------------------------------- #

SUPPORTED_EXTENSIONS = {".docx", ".md"}


def parse_file(path: Path) -> tuple[str, str]:
    ext = path.suffix.lower()
    if ext == ".docx":
        return parse_docx(path)
    if ext == ".md":
        return parse_md(path)
    raise ValueError(f"Unsupported file extension: {ext}")
