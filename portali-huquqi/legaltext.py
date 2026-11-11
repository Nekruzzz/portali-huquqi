"""Legal texts stored as HTML: sanitising, structure (anchors, table of contents),
links to acts of the previous portal, plain-text conversion and comparison of wordings.

Texts of the previous portal (Ministry of Justice database) look like
    <H4><A class=anchor-ogl name=A000000003></A>БОБИ 1. МУҚАРРАРОТИ УМУМӢ</H4>
    <H6><A class=anchor-ogl name=A000000004></A>Моддаи 1. ...</H6>
    <A class="outer" href="view_qonunhoview.php?showdetail=&asosi_id=25457#A000000064">...</A>
The anchors (A000000064) are kept, because links between acts point to them.
"""
import difflib
import html as html_lib
import re
from html.parser import HTMLParser

import nh3

ALLOWED_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "div", "span", "br", "hr", "a", "b", "strong", "i", "em",
                "u", "s", "sub", "sup", "blockquote", "pre", "code", "ul", "ol", "li", "dl", "dt", "dd", "table",
                "caption", "thead", "tbody", "tfoot", "tr", "th", "td", "colgroup", "col"}
ALLOWED_ATTRS = {"*": {"id", "class"}, "a": {"href", "name", "title"}, "td": {"colspan", "rowspan"},
                 "th": {"colspan", "rowspan"}, "ol": {"start"}}
# formatting classes of the old texts; kept with an "lt-" prefix so they never clash with the site's styles
KEEP_CLASSES = {"inline-comment", "block", "right", "left", "center", "dname", "doc-info-change", "doc-info",
                "comments", "title", "attach-w"}
# ids the document page uses itself
RESERVED_IDS = {"main", "summary", "details", "text", "amendments", "files", "lifecycle", "comments", "comment-form",
                "vote", "versions", "prev-text", "reference"}
ID_RE = re.compile(r"^[A-Za-z][\w-]{0,60}$")
OLD_ACT_LINK = re.compile(r"(?:^|/)view_qonunhoview\.php\?(?:[^#]*&)?asosi_id=(\d+)[^#]*(#[\w-]+)?$")
LEGACY_HREF = re.compile(r'href="/publicadliya/view_qonunhoview\.php\?asosi_id=(\d+)(#[\w-]+)?"')
HEADING_RE = re.compile(r"<h([1-6])([^>]*)>(.*?)</h\1>", re.S)


def legacy_act_href(asosi_id, fragment=""):
    return f"/publicadliya/view_qonunhoview.php?asosi_id={asosi_id}{fragment or ''}"


def _attr_filter(tag, attr, value):
    if attr == "class":
        kept = [f"lt-{c}" for c in value.split() if c in KEEP_CLASSES]
        return " ".join(kept) or None
    if attr in ("id", "name"):  # "el_..." ids belong to the old portal's page, not to the text
        return value if ID_RE.match(value) and value not in RESERVED_IDS and not value.startswith("el_") else None
    if attr == "href":
        value = html_lib.unescape(value).strip()
        m = OLD_ACT_LINK.search(value)
        if m:  # a link to another act of the old portal: keep it as a portal address, resolved when shown
            return legacy_act_href(m.group(1), m.group(2))
        if value.startswith(("#", "/", "http://", "https://", "mailto:")) and not value.startswith("//"):
            return value
        return None  # relative links to pages of the old site that no longer exist
    if attr in ("colspan", "rowspan", "start"):
        return value if value.isdigit() and int(value) < 1000 else None
    return value


ALIGN_STYLE = re.compile(r"<(p|div|h[1-6]|td|th)\b([^>]*?)\sstyle=(\"[^\"]*\"|'[^']*')([^>]*)>", re.I)


def _align_to_class(m):
    """Word and the old editor centre or right-align with inline styles; keep that as a class."""
    tag, before, style, after = m.groups()
    align = re.search(r"text-align\s*:\s*(center|right|justify)", style, re.I)
    cls = {"center": "center", "right": "right"}.get(align.group(1).lower()) if align else None
    attrs = before + after
    if cls:
        if re.search(r"\bclass=", attrs, re.I):
            attrs = re.sub(r"\bclass=([\"']?)", lambda c: f"class={c.group(1)}{cls} ", attrs, count=1, flags=re.I)
        else:
            attrs += f' class="{cls}"'
    return f"<{tag}{attrs}>"


def _balanced_spans(fragment):
    """True when every </span> in the fragment closes a <span> opened inside it."""
    depth = 0
    for closing in re.findall(r"<(/?)span\b", fragment):
        depth += -1 if closing else 1
        if depth < 0:
            return False
    return depth == 0


def sanitize(raw):
    """Clean HTML from any source (old portal, Word, editor) down to the tags a legal text needs."""
    if not raw:
        return ""
    raw = ALIGN_STYLE.sub(_align_to_class, raw)
    out = nh3.clean(raw, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRS, attribute_filter=_attr_filter,
                    clean_content_tags={"script", "style", "title", "head"}, strip_comments=True, link_rel=None,
                    url_schemes={"http", "https", "mailto"})

    # <a name="X"> (old style) becomes <a id="X">, unless the tag already has an id
    def fix_anchor(m):
        tag = m.group(0)
        if ' id="' in tag:
            return re.sub(r'\s+name="[^"]*"', "", tag)
        return tag.replace(' name="', ' id="', 1)
    out = re.sub(r"<a\b[^>]*\bname=\"[^\"]*\"[^>]*>", fix_anchor, out)
    out = re.sub(r"(<p>\s*(?:&nbsp;|\s)*</p>\s*){2,}", "<p>&nbsp;</p>", out)  # runs of empty paragraphs
    out = re.sub(r"<(strong|b|em|i|u|span)>\s*</\1>", "", out)  # empty inline tags
    # wrappers the old portal's page put around a whole text: <span><span> ... </span></span>
    while True:
        m = re.fullmatch(r"\s*<span[^>]*>(.*)</span>\s*", out, re.S)
        if not m or not _balanced_spans(m.group(1)):
            break
        out = m.group(1)
    return out.strip()


def _strip_tags(fragment):
    return " ".join(html_lib.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def structure(body_html, propose_label=None, propose_title=None, link_label=None):
    """Give every heading an anchor and list them for the table of contents.
    With propose_label, a "propose" link is added to each heading (drafts in consultation).
    Returns (html, headings) where headings = [{"id", "text", "level"}]."""
    headings, n = [], 0

    def fix(m):
        nonlocal n
        level, attrs, inner = m.group(1), m.group(2), m.group(3)
        text = _strip_tags(inner)
        if not text:
            return m.group(0)
        n += 1
        anchor = re.search(r'<a\b[^>]*\bid="([^"]+)"', inner)
        own = re.search(r'\bid="([^"]+)"', attrs)
        if own:
            hid = own.group(1)
        elif anchor:
            hid = anchor.group(1)
        else:
            hid = f"s{n}"
            attrs += f' id="{hid}"'
        headings.append({"id": hid, "text": text, "level": int(level)})
        extra = ""
        if link_label:  # a link to this very article, to copy and cite
            extra = (f' <a class="anchor-link" href="#{hid}" aria-label="{html_lib.escape(link_label)}: '
                     f'{html_lib.escape(text)}" title="{html_lib.escape(link_label)}">#</a>')
        if propose_label:
            from urllib.parse import quote
            extra += (f' <a class="propose-here" href="?article={quote(text[:120])}#comment-form" '
                     f'aria-label="{html_lib.escape(propose_title or propose_label)}: {html_lib.escape(text)}">'
                     f"{html_lib.escape(propose_label)}</a>")
        if 'class="' in attrs:  # keep alignment classes of the original heading
            attrs = attrs.replace('class="', 'class="article-h ', 1)
        else:
            attrs += ' class="article-h"'
        return f'<h{level}{attrs}>{inner}{extra}</h{level}>'
    return HEADING_RE.sub(fix, body_html or ""), headings


def legacy_ids_in(body_html):
    return {int(m.group(1)) for m in LEGACY_HREF.finditer(body_html or "")}


PORTAL_HREF = re.compile(r'href="/doc/(\d+)(?:[#?][^"]*)?"')


def references(body_html):
    """(old-portal act numbers, portal document ids) that a text links to."""
    return legacy_ids_in(body_html), {int(m.group(1)) for m in PORTAL_HREF.finditer(body_html or "")}


def resolve_links(body_html, mapping, doc_url):
    """Point links to old-portal acts straight at their new pages (mapping: asosi_id -> document id).
    Acts not imported yet keep the old address, which the portal redirects once they arrive."""
    def repl(m):
        target = mapping.get(int(m.group(1)))
        return f'href="{doc_url(target)}{m.group(2) or ""}"' if target else m.group(0)
    return LEGACY_HREF.sub(repl, body_html or "")


class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "br", "blockquote", "pre", "dt", "dd",
             "table", "hr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCK:
            self.parts.append("\n")
        elif tag in ("td", "th"):
            self.parts.append("\t")

    def handle_endtag(self, tag):
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def to_text(body_html):
    """Plain text with one paragraph per line (search index, .txt download, comparison)."""
    p = _TextExtractor()
    p.feed(body_html or "")
    lines = [" ".join(line.replace("\xa0", " ").split()) for line in "".join(p.parts).split("\n")]
    return "\n".join(line for line in lines if line)


def paragraphs(body, fmt):
    text = to_text(body) if fmt == "html" else (body or "")
    return [p.strip() for p in re.split(r"\n\s*\n|\n", text) if p.strip()]


def _word_diff(a, b):
    wa, wb = a.split(" "), b.split(" ")
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, wa, wb, autojunk=False).get_opcodes():
        if op == "equal":
            out.append(html_lib.escape(" ".join(wa[i1:i2])))
        if op in ("delete", "replace"):
            out.append(f"<del>{html_lib.escape(' '.join(wa[i1:i2]))}</del>")
        if op in ("insert", "replace"):
            out.append(f"<ins>{html_lib.escape(' '.join(wb[j1:j2]))}</ins>")
    return " ".join(out)


def compare(old_pars, new_pars):
    """Paragraph-level comparison with word-level detail for changed paragraphs.
    Returns rows {"op": equal|insert|delete|change, "html": ...} and counts of changes."""
    rows, counts = [], {"insert": 0, "delete": 0, "change": 0}
    sm = difflib.SequenceMatcher(None, old_pars, new_pars, autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            rows += [{"op": "equal", "html": html_lib.escape(p)} for p in old_pars[i1:i2]]
        elif op == "delete":
            rows += [{"op": "delete", "html": f"<del>{html_lib.escape(p)}</del>"} for p in old_pars[i1:i2]]
            counts["delete"] += i2 - i1
        elif op == "insert":
            rows += [{"op": "insert", "html": f"<ins>{html_lib.escape(p)}</ins>"} for p in new_pars[j1:j2]]
            counts["insert"] += j2 - j1
        else:
            old, new = old_pars[i1:i2], new_pars[j1:j2]
            for k in range(max(len(old), len(new))):
                a, b = (old[k] if k < len(old) else None), (new[k] if k < len(new) else None)
                if a is not None and b is not None:
                    rows.append({"op": "change", "html": _word_diff(a, b)})
                    counts["change"] += 1
                elif a is not None:
                    rows.append({"op": "delete", "html": f"<del>{html_lib.escape(a)}</del>"})
                    counts["delete"] += 1
                else:
                    rows.append({"op": "insert", "html": f"<ins>{html_lib.escape(b)}</ins>"})
                    counts["insert"] += 1
    return rows, counts


class _BlockParser(HTMLParser):
    """Sanitised HTML -> simple blocks for export: headings, paragraphs (with alignment) and tables."""
    HEAD = {"h1": 1, "h2": 1, "h3": 2, "h4": 2, "h5": 3, "h6": 3}
    PARA = {"p", "li", "dt", "dd", "blockquote", "pre", "caption"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks, self.buf, self.kind, self.align = [], [], None, []
        self.table, self.row, self.cell = None, None, None

    def _flush(self):
        text = " ".join("".join(self.buf).replace("\xa0", " ").split())
        self.buf = []
        if not text:
            return
        if self.cell is not None:
            self.cell.append(text)
            return
        level = self.HEAD.get(self.kind)
        align = next((a for a in reversed(self.align) if a), None)
        self.blocks.append({"type": "h", "level": level, "text": text} if level else
                           {"type": "p", "text": text, "align": align})

    def handle_starttag(self, tag, attrs):
        classes = (dict(attrs).get("class") or "").split()
        if tag in self.HEAD or tag in self.PARA or tag in ("div", "br", "tr"):
            self._flush()
        if tag in self.HEAD or tag in self.PARA:
            self.kind = tag
        if tag in ("div", "p"):
            self.align.append("right" if "lt-right" in classes else "center" if
                              ("lt-center" in classes or "lt-dname" in classes) else None)
        if tag == "table":
            self.table = []
        elif tag == "tr" and self.table is not None:
            self.row = []
            self.table.append(self.row)
        elif tag in ("td", "th") and self.row is not None:
            self._flush()
            self.cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None:
            self._flush()
            self.row.append(" ".join(self.cell))
            self.cell = None
        elif tag == "table" and self.table is not None:
            self._flush()
            if any(self.table):
                self.blocks.append({"type": "table", "rows": [r for r in self.table if r]})
            self.table, self.row = None, None
        elif tag in self.HEAD or tag in self.PARA or tag == "div":
            self._flush()
            self.kind = None
        if tag in ("div", "p") and self.align:
            self.align.pop()

    def handle_data(self, data):
        self.buf.append(data)

    def close(self):
        super().close()
        self._flush()


def export_blocks(body, fmt, text_blocks=None):
    if fmt == "html":
        p = _BlockParser()
        p.feed(body or "")
        p.close()
        return p.blocks
    return [{"type": "h", "level": 2, "text": b["text"]} if b["type"] == "h" else {"type": "p", "text": b["text"]}
            for b in (text_blocks or [])]


def build_docx(title, meta_line, blocks, source_url):
    """A Word document of the act: title, reference line, text, link back to the portal."""
    import io
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt
    d = Document()
    style = d.styles["Normal"]
    style.font.name = "Times New Roman"
    style.font.size = Pt(12)
    h = d.add_paragraph()
    h.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = h.add_run(title)
    run.bold = True
    run.font.size = Pt(14)
    if meta_line:
        m = d.add_paragraph(meta_line)
        m.alignment = WD_ALIGN_PARAGRAPH.CENTER
    align = {"right": WD_ALIGN_PARAGRAPH.RIGHT, "center": WD_ALIGN_PARAGRAPH.CENTER}
    for b in blocks:
        if b["type"] == "h":
            para = d.add_paragraph()
            r = para.add_run(b["text"])
            r.bold = True
            if b["level"] == 1:
                para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        elif b["type"] == "p":
            para = d.add_paragraph(b["text"])
            if b.get("align") in align:
                para.alignment = align[b["align"]]
            else:
                para.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        else:
            width = max(len(r) for r in b["rows"])
            table = d.add_table(rows=0, cols=width)
            table.style = "Table Grid"
            for row in b["rows"]:
                cells = table.add_row().cells
                for i, value in enumerate(row):
                    cells[i].text = value
    d.add_paragraph()
    d.add_paragraph(source_url).runs[0].font.size = Pt(9)
    out = io.BytesIO()
    d.save(out)
    return out.getvalue()
