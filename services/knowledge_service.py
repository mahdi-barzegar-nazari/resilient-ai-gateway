"""Academic lookups: Wikipedia (/wiki) and ArXiv (/arxiv, /arxiv_full).

Both use free public APIs, so they work as a bridge for users whose network blocks
those sites: the bot server fetches the content and relays it as plain text.
"""
import logging
import re
import xml.etree.ElementTree as ET

import requests

import config
from core.errors import ToolError
from services import clients
from services.ai_router import process_ai_query
from services.media_service import PDF_NEEDS_GEMINI_TEXT, pdf_input, pdf_too_big_text
from utils.formatters import truncate_text
from utils.security import short_err

log = logging.getLogger(__name__)

BOT_HTTP_HEADERS = {"User-Agent": "ResilientAIGateway/1.0 (student helper bot)"}

# ---------------------------------------------------------------- Wikipedia
PERSIAN_CHARS = re.compile(r'[\u0600-\u06FF]')
WIKI_HEADING_LINE = re.compile(r'^\s*={2,}\s*(.*?)\s*={2,}\s*$')
# Trailing sections that are not worth reading (references, external links ...)
WIKI_TAIL_HEADINGS = {
    "see also", "references", "external links", "further reading", "notes", "sources",
    "bibliography", "citations", "footnotes",
    "جستارهای وابسته", "پانویس", "منابع", "پیوند به بیرون", "پیوندهای بیرونی",
    "یادداشت‌ها", "کتاب‌شناسی", "برای مطالعه بیشتر",
}


def format_wiki_text(extract):
    """Turn ``== Heading ==`` markers into ``▪️ Heading`` and drop the tail sections."""
    lines = []
    for line in (extract or "").splitlines():
        m = WIKI_HEADING_LINE.match(line)
        if m:
            heading = m.group(1).strip()
            if heading.lower() in WIKI_TAIL_HEADINGS:
                break
            lines.append(f"▪️ {heading}")
        else:
            lines.append(line.rstrip())
    return re.sub(r'\n{3,}', '\n\n', "\n".join(lines)).strip()


def wiki_lookup(lang, topic):
    """Search the official MediaWiki API and fetch the full plain-text article.

    Returns ``(title, extract)`` or ``None`` when nothing matches.
    """
    api_url = f"https://{lang}.wikipedia.org/w/api.php"
    res = requests.get(
        api_url,
        params={"action": "query", "list": "search", "srsearch": topic, "srlimit": 1, "format": "json"},
        headers=BOT_HTTP_HEADERS, timeout=10
    )
    res.raise_for_status()
    hits = (res.json().get("query") or {}).get("search") or []
    if not hits:
        return None
    title = hits[0]["title"]
    res = requests.get(
        api_url,
        params={"action": "query", "prop": "extracts", "explaintext": 1, "exsectionformat": "wiki",
                "redirects": 1, "titles": title, "format": "json", "formatversion": 2},
        headers=BOT_HTTP_HEADERS, timeout=15
    )
    res.raise_for_status()
    pages = (res.json().get("query") or {}).get("pages") or []
    if isinstance(pages, dict):
        pages = list(pages.values())
    extract = (pages[0].get("extract") if pages else "") or ""
    return title, extract


def tool_wiki(arg, model_name=None):
    if not arg:
        raise ToolError("موضوع را بعد از دستور بنویسید.\nمثال: /wiki Dijkstra's algorithm\nیا: /wiki الگوریتم دایکسترا")
    langs = ["fa", "en"] if PERSIAN_CHARS.search(arg) else ["en", "fa"]
    failed = 0
    for lang in langs:
        try:
            found = wiki_lookup(lang, arg)
        except Exception as e:
            failed += 1
            log.warning("Wikipedia error (%s): %s", lang, short_err(e))
            continue
        if found:
            title, extract = found
            text = format_wiki_text(extract)
            if text:
                return f"📚 ویکی‌پدیا ({lang}) — {title}\n\n{truncate_text(text, config.WIKI_MAX_CHARS)}"
    if failed == len(langs):
        raise ToolError("ارتباط با ویکی‌پدیا برقرار نشد؛ کمی بعد دوباره امتحان کنید.")
    raise ToolError("مقاله‌ای با این عنوان پیدا نشد. عبارت دقیق‌تر یا انگلیسی را امتحان کنید.")


# ---------------------------------------------------------------- ArXiv
ARXIV_API = "https://export.arxiv.org/api/query"
ARXIV_NS = {"a": "http://www.w3.org/2005/Atom"}
ARXIV_ID_PATTERN = re.compile(r'(\d{4}\.\d{4,5}(?:v\d+)?|[a-z\-]+(?:\.[a-z]{2})?/\d{7}(?:v\d+)?)', re.IGNORECASE)
ARXIV_FULL_PROMPT = (
    "این مقاله‌ی علمی را کامل بخوان و به فارسی یک خلاصه‌ی ساختاریافته بنویس: ۱) مسئله و انگیزه؛ "
    "۲) روش پیشنهادی؛ ۳) آزمایش‌ها و نتایج کلیدی (با عدد در صورت وجود)؛ ۴) محدودیت‌ها؛ "
    "۵) جمع‌بندی در سه جمله. اصطلاحات فنی را به انگلیسی بنویس."
)


def parse_arxiv_id(text):
    """Extract an ArXiv id from a bare id, ``arXiv:ID`` or an abs/pdf URL; ``None`` otherwise.

    The whole input must be an id: a free-text search like "gpt 2023.1234 paper" is not one.
    """
    t = (text or "").strip()
    t = re.sub(r'^(?:https?://)?(?:www\.)?arxiv\.org/(?:abs|pdf)/', '', t, flags=re.IGNORECASE)
    t = re.sub(r'^arxiv:', '', t, flags=re.IGNORECASE)
    t = re.sub(r'\.pdf$', '', t, flags=re.IGNORECASE)
    m = ARXIV_ID_PATTERN.fullmatch(t)
    return m.group(1) if m else None


def _atom_text(node, tag):
    child = node.find(f"a:{tag}", ARXIV_NS)
    return re.sub(r'\s+', ' ', child.text or "").strip() if child is not None else ""


def arxiv_query(**params):
    """Query the ArXiv Atom API and return a list of paper dicts."""
    res = requests.get(ARXIV_API, params=params, headers=BOT_HTTP_HEADERS, timeout=15)
    res.raise_for_status()
    root = ET.fromstring(res.content)
    papers = []
    for entry in root.findall("a:entry", ARXIV_NS):
        title = _atom_text(entry, "title")
        if not title or title.lower() == "error":  # the API reports failures as an <entry>
            continue
        m = re.search(r'arxiv\.org/abs/(.+)$', _atom_text(entry, "id"))
        category = entry.find("a:category", ARXIV_NS)
        papers.append({
            "id": m.group(1) if m else "",
            "title": title,
            "summary": _atom_text(entry, "summary"),
            "authors": [n for n in (_atom_text(a, "name") for a in entry.findall("a:author", ARXIV_NS)) if n],
            "date": _atom_text(entry, "published")[:10],
            "category": category.get("term") if category is not None else "",
        })
    return papers


def format_arxiv_authors(authors):
    return "، ".join(authors[:4]) + (" و همکاران" if len(authors) > 4 else "")


def tool_arxiv(arg, model_name=None):
    if not arg:
        raise ToolError("عبارت جستجو یا شناسه‌ی مقاله را بنویسید.\nمثال: /arxiv attention is all you need\nیا: /arxiv 1706.03762")
    paper_id = parse_arxiv_id(arg)
    terms = [] if paper_id else re.findall(r'[\w\-]+', arg)[:8]
    if not paper_id and not terms:
        raise ToolError("عبارت جستجو معتبر نیست.")
    try:
        if paper_id:
            papers = arxiv_query(id_list=paper_id)
        else:
            papers = arxiv_query(
                search_query=" AND ".join(f"all:{w}" for w in terms), start=0, max_results=config.ARXIV_RESULTS
            )
    except Exception as e:
        log.warning("ArXiv error: %s", short_err(e))
        raise ToolError("ارتباط با ArXiv برقرار نشد؛ کمی بعد دوباره امتحان کنید.")
    if not papers:
        raise ToolError("مقاله‌ای پیدا نشد. عبارت را انگلیسی و دقیق‌تر بنویسید.")

    if paper_id:
        p = papers[0]
        return (
            f"📄 {p['title']}\n"
            f"👤 {format_arxiv_authors(p['authors'])}\n"
            f"🗓 {p['date']} | 🏷 {p['category']} | 🆔 {p['id']}\n\n"
            f"چکیده:\n{p['summary']}\n\n"
            f"🔗 https://arxiv.org/abs/{p['id']}\n"
            f"برای خلاصه‌ی فارسی کل مقاله: /arxiv_full {p['id']}"
        )

    lines = [f"🔎 نتایج ArXiv برای «{arg}»:", ""]
    for i, p in enumerate(papers, 1):
        lines += [f"{i}) {p['title']}", f"   👤 {format_arxiv_authors(p['authors'])}", f"   🗓 {p['date']} | 🆔 {p['id']}", ""]
    lines.append("برای چکیده: /arxiv شناسه\nبرای خلاصه‌ی فارسی کل مقاله: /arxiv_full شناسه")
    return "\n".join(lines)


def tool_arxiv_full(arg, model_name=None):
    """Download the paper's PDF and let Gemini read the whole document."""
    paper_id = parse_arxiv_id(arg)
    if not paper_id:
        raise ToolError("شناسه‌ی مقاله را بنویسید.\nمثال: /arxiv_full 1706.03762")
    if not clients.gemini_client:
        raise ToolError(PDF_NEEDS_GEMINI_TEXT)
    try:
        res = requests.get(f"https://arxiv.org/pdf/{paper_id}", headers=BOT_HTTP_HEADERS, timeout=45)
        res.raise_for_status()
    except Exception as e:
        log.warning("ArXiv PDF error: %s", short_err(e))
        raise ToolError("دریافت فایل مقاله از ArXiv ممکن نشد.")
    pdf = res.content
    if not pdf.startswith(b"%PDF"):
        raise ToolError("فایل دریافتی PDF معتبر نبود.")
    if len(pdf) > config.MAX_PDF_BYTES:
        raise ToolError(pdf_too_big_text())
    text, _ = process_ai_query(ARXIV_FULL_PROMPT, image=pdf_input(pdf))
    return f"📄 ArXiv:{paper_id}\n\n{text}"
