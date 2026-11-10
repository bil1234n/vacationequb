"""
Terms Content as a document: staff write the agreement as plain text with simple marks, and the app and
console show it as a formatted letter. The console editor's toolbar inserts every mark.

Blocks (one per line)                         Inline (inside any line, can be combined)
  # Title                                       **bold**   *italic*   __underline__
  ## Section        ### Sub-section             ~~strikethrough~~   ==highlight==
  1.  1.1  1.1.1   numbered clauses
  - point   (indent 2 spaces per level)  bullets
  a)  b)  i)  ii)  lettered / roman list items
  > text           quote
  !! text          important notice (highlighted box)
  :: text          centred line
  ---              divider
  [sign] Member    signature line
  A short line in CAPITALS is a section heading too; anything else is a paragraph.
"""
import re
from html import escape

CLAUSE = re.compile(r"^(\d{1,3}(?:\.\d{1,3})*)[.)]?\s+(.+)$")
BULLET = re.compile(r"^[-•*–]\s+(.+)$")
LETTER = re.compile(r"^\(?((?:[ivx]{1,4})|[a-z])\)\s+(.+)$", re.I)
# Amharic (ሀ. ለ) ሐ.) and Arabic (أ) ب.) list letters
LOCAL_LETTER = re.compile(r"^\(?([\u1200-\u135A\u0621-\u064A])([.)])\s+(.+)$")
# "አንቀጽ 1፡ …", "Article 2 – …", "ምዕራፍ ፫", "المادة 3" are section headings
ARTICLE = re.compile(r"^(?:አንቀጽ|አንቀፅ|ክፍል|ምዕራፍ|ርዕስ|article|section|chapter|part|المادة|الفصل|الباب)\s*[\d\u1369-\u137C]+"
                     r"\s*(?:[:፡፦.\-–]|$)", re.I)
SIGN = re.compile(r"^\[sign(?:ature)?\]\s*(.*)$", re.I)
DIVIDER = re.compile(r"^(?:-{3,}|\*{3,}|_{3,}|={3,})$")
LEAD = re.compile(r"^([^.!?።:፡]{2,60}?)(\s*(?:፡-|፦|:-|:\s+-|፡\s+-))")
WORDS = re.compile(r"[\wሀ-፿؀-ۿ']+")
INLINE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__|~~(.+?)~~|==(.+?)==|(?<![*\w])\*(?!\s)(.+?)(?<!\s)\*(?![*\w])")
MARKS = ("b", "u", "s", "h", "i")  # same order as the INLINE groups


def spans(text: str, style: tuple = ()) -> list[dict]:
    """Inline marks → [{"t": text, "b": True, "i": True, …}] (marks can be nested: **__bold underline__**)."""
    out, pos = [], 0
    for m in INLINE.finditer(text):
        if m.start() > pos:
            out.append(_span(text[pos:m.start()], style))
        idx = next(i for i in range(5) if m.group(i + 1) is not None)
        out += spans(m.group(idx + 1), style + (MARKS[idx],))
        pos = m.end()
    if pos < len(text):
        out.append(_span(text[pos:], style))
    return out


def _span(t: str, style: tuple) -> dict:
    d = {"t": t}
    for k in style:
        d[k] = True
    return d


def plain(text: str) -> str:
    return "".join(s["t"] for s in spans(text))


def _is_caps_heading(line: str) -> bool:
    letters = [c for c in plain(line) if c.isalpha() and c.isascii()]
    return (3 <= len(line) <= 80 and len(letters) >= 3 and all(c.isupper() for c in letters)
            and not line.endswith((".", ",", ";", ":")))


def parse(text: str) -> list[dict]:
    blocks: list[dict] = []

    def add(kind: str, body: str = "", **extra):
        if kind in ("clause", "letter", "bullet", "paragraph") and "**" not in body:
            body = LEAD.sub(r"**\1**\2", body, count=1)  # "Term፡- explanation" → the term in bold
        blocks.append({"type": kind, "text": plain(body), "spans": spans(body), **extra})

    for raw in (text or "").replace("\t", "    ").splitlines():
        line = raw.strip()
        if not line:
            continue
        indent = min((len(raw) - len(raw.lstrip(" "))) // 2, 2)
        if DIVIDER.match(line):
            blocks.append({"type": "divider", "text": "", "spans": []})
        elif line.startswith("# "):
            add("title", line[2:].strip())
        elif line.startswith("## "):
            add("heading", line[3:].strip())
        elif re.match(r"^#{3,6}\s", line):
            add("subheading", line.lstrip("#").strip())
        elif line.startswith("!!"):
            add("notice", line[2:].strip())
        elif line.startswith(">"):
            add("quote", line[1:].strip())
        elif line.startswith("::"):
            add("center", line[2:].strip())
        elif m := SIGN.match(line):
            add("signature", m.group(1).strip() or "Signature")
        elif ARTICLE.match(line) and len(line) <= 140:
            add("heading", line)
        elif m := CLAUSE.match(line):
            number = m.group(1)
            add("clause", m.group(2).strip(), number=number, level=number.count(".") + 1)
        elif m := BULLET.match(line):
            add("bullet", m.group(1).strip(), level=indent + 1)
        elif (m := LETTER.match(line)) and (len(m.group(1)) == 1 or re.fullmatch(r"[ivx]+", m.group(1), re.I)):
            add("letter", m.group(2).strip(), label=f"{m.group(1).lower()})", level=indent + 1)
        elif m := LOCAL_LETTER.match(line):
            add("letter", m.group(3).strip(), label=f"{m.group(1)}{m.group(2)}", level=indent + 1)
        elif _is_caps_heading(line):
            add("heading", line.title())
        else:
            add("paragraph", line)
    return blocks


def document(text: str) -> dict:
    """{blocks, words, minutes, headings} – what the app's agreement reader shows."""
    blocks = parse(text)
    words = sum(len(WORDS.findall(b["text"])) for b in blocks)
    return {"blocks": blocks, "words": words, "minutes": max(1, round(words / 200)) if words else 0,
            "headings": [b["text"] for b in blocks if b["type"] in ("heading", "title")]}


def lines(text: str) -> list[str]:
    """One plain line per block – for older app versions that show the terms as a list."""
    out = []
    for b in parse(text):
        if b["type"] == "divider":
            continue
        t = b["text"]
        if b["type"] == "clause" and "." in b["number"]:
            t = f"{b['number']}. {t}"
        elif b["type"] == "letter":
            t = f"{b['label']} {t}"
        out.append(t)
    return out


TAGS = {"b": "strong", "i": "em", "u": "u", "s": "s", "h": "mark"}


def _inline(block: dict) -> str:
    out = []
    for s in block["spans"]:
        h = escape(s["t"])
        for k in MARKS:
            if s.get(k):
                h = f"<{TAGS[k]}>{h}</{TAGS[k]}>"
        out.append(h)
    return "".join(out)


def to_html(text: str) -> str:
    """The agreement as HTML for the console (same rules as the app)."""
    out = []
    for b in parse(text):
        t, k = _inline(b), b["type"]
        if k == "title":
            out.append(f'<h1 class="terms-title">{t}</h1>')
        elif k == "heading":
            out.append(f'<h2 class="terms-h">{t}</h2>')
        elif k == "subheading":
            out.append(f'<h3 class="terms-h3">{t}</h3>')
        elif k == "clause":
            n = b["number"] + ("" if "." in b["number"] else ".")
            out.append(f'<div class="terms-clause l{min(b["level"], 3)}"><span class="terms-no">{escape(n)}</span><p>{t}</p></div>')
        elif k in ("bullet", "letter"):
            mark = "<i class='terms-dot'></i>" if k == "bullet" else f'<span class="terms-lt">{escape(b["label"])}</span>'
            out.append(f'<div class="terms-li l{b["level"]}">{mark}<p>{t}</p></div>')
        elif k == "quote":
            out.append(f'<blockquote class="terms-quote">{t}</blockquote>')
        elif k == "notice":
            out.append(f'<div class="terms-notice"><b>!</b><p>{t}</p></div>')
        elif k == "center":
            out.append(f'<p class="terms-center">{t}</p>')
        elif k == "divider":
            out.append('<hr class="terms-hr">')
        elif k == "signature":
            out.append(f'<div class="terms-sign"><span></span><small>{t}</small></div>')
        else:
            out.append(f'<p class="terms-p">{t}</p>')
    return "\n".join(out)
