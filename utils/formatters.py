"""Pure text helpers: message chunking, Telegram HTML conversion, truncation."""
import html
import re

import config


def split_message(text, limit=config.TG_CHUNK_SIZE):
    """Split a long text into chunks shorter than ``limit``, breaking on line boundaries.

    The splitter tracks whether it is inside a fenced code block (```). If a chunk ends
    inside a block, the fence is closed in that chunk and re-opened at the top of the
    next one, so syntax highlighting never breaks. Single lines longer than the limit
    are hard-wrapped.
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return [text]

    reserve = 10                      # room for the closing fence
    max_piece = limit - 2 * reserve   # longest allowed single line
    chunks, current, current_len, in_code = [], [], 0, False

    for line in text.split("\n"):
        pieces = [line[i:i + max_piece] for i in range(0, len(line), max_piece)] or [""]
        for piece in pieces:
            if current and current_len + len(piece) + 1 > limit - reserve:
                body = "\n".join(current)
                chunks.append(body + "\n```" if in_code else body)
                current = ["```"] if in_code else []
                current_len = 4 if in_code else 0
            current.append(piece)
            current_len += len(piece) + 1
        if line.count("```") % 2 == 1:
            in_code = not in_code

    if current:
        chunks.append("\n".join(current))
    return [c for c in chunks if c.strip()]


def to_telegram_html(text, escape=False):
    """Convert light Markdown (**bold**, `code`, ```pre```) to Telegram-flavoured HTML.

    ``escape=True`` is used for model output: <, > and & are neutralised so that code
    samples cannot break Telegram's HTML parser. Code spans are stashed before the bold
    pass so that ``**`` inside code (e.g. ``**kwargs``) is left untouched.
    """
    if escape:
        text = html.escape(text, quote=False)
    stash = []

    def keep(template):
        def _inner(m):
            stash.append(template.format(m.group(1)))
            return f"\x00{len(stash) - 1}\x00"
        return _inner

    text = re.sub(r'```[\w+#.-]*\n(.*?)```', keep("<pre>{}</pre>"), text, flags=re.DOTALL)
    text = re.sub(r'```(.*?)```', keep("<pre>{}</pre>"), text, flags=re.DOTALL)
    text = re.sub(r'`([^`\n]+)`', keep("<code>{}</code>"), text)
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    return re.sub(r'\x00(\d+)\x00', lambda m: stash[int(m.group(1))], text)


def truncate_text(text, limit):
    """Keep at most ``limit`` characters, preferring to cut at a line or sentence boundary."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    boundary = max(cut.rfind("\n"), cut.rfind(". "), cut.rfind("؟"), cut.rfind("؛"))
    if boundary > limit * 0.6:
        cut = cut[:boundary + 1]
    return cut.rstrip() + "\n[... ادامه‌ی متن برای کوتاه‌سازی حذف شد]"
