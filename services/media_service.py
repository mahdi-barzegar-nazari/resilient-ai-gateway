"""Prompt and metadata helpers for images and PDF documents."""
import os

import config

# ---------------------------------------------------------------- images
IMAGE_DEFAULT_PROMPT = (
    "این تصویر را با دقت بررسی کن. اگر شامل سؤال یا تست است آن را حل کن؛ "
    "اگر کد، خطا یا اسکرین‌شات است مشکل را توضیح بده و راه‌حل بده؛ "
    "در غیر این صورت محتوای تصویر را توضیح بده."
)
IMAGE_EXAM_DEFAULT_PROMPT = "سؤال‌های داخل تصویر را بخوان و طبق پروتکل آزمون پاسخ بده."
IMAGE_EXAM_EXTRA = "اگر چند سؤال در تصویر هست، برای هر سؤال یک سطر جدا با قالب RESULT بنویس (به ترتیب شماره سؤال)."


def image_mime(file_path):
    ext = os.path.splitext(file_path or "")[1].lower()
    return {
        ".png": "image/png",
        ".webp": "image/webp",
        ".heic": "image/heic",
        ".heif": "image/heif",
    }.get(ext, "image/jpeg")


def build_image_prompt(caption, is_exam):
    caption = (caption or "").strip()
    if is_exam:
        return f"{caption or IMAGE_EXAM_DEFAULT_PROMPT}\n{IMAGE_EXAM_EXTRA}"
    return caption or IMAGE_DEFAULT_PROMPT


def image_memory_text(caption):
    caption = (caption or "").strip()
    return f"[تصویر] {caption}" if caption else "[تصویر ارسال شد]"


def image_input(data, file_path):
    return {"data": data, "mime": image_mime(file_path)}


# ---------------------------------------------------------------- PDF documents
PDF_MIME = "application/pdf"
PDF_DEFAULT_PROMPT = (
    "این فایل PDF را به‌طور کامل بخوان و به فارسی بنویس: ۱) موضوع و هدف سند؛ "
    "۲) نکات و مفاهیم کلیدی (فهرست مرتب)؛ ۳) اصطلاحات، تعریف‌ها و فرمول‌های مهم؛ ۴) جمع‌بندی کوتاه. "
    "اگر سند تمرین یا سؤال پایان فصل دارد، در انتها فهرست کوتاهی از آن‌ها بده. "
    "اصطلاحات فنی را کنار معادل فارسی به انگلیسی هم بنویس."
)
PDF_EXAM_DEFAULT_PROMPT = "سؤال‌های داخل فایل را بخوان و طبق پروتکل آزمون پاسخ بده."
PDF_EXAM_EXTRA = "اگر چند سؤال در فایل هست، برای هر سؤال یک سطر جدا با قالب RESULT بنویس (به ترتیب شماره سؤال)."
PDF_ONLY_TEXT = "📄 فعلاً فقط فایل PDF پشتیبانی می‌شود."
PDF_NEEDS_GEMINI_TEXT = "خواندن PDF به GEMINI_API_KEY نیاز دارد."
IMAGE_NEEDS_GEMINI_TEXT = "تحلیل تصویر به GEMINI_API_KEY نیاز دارد."


def pdf_too_big_text():
    return (
        f"📄 حجم فایل بیش از {config.MAX_PDF_BYTES // (1024 * 1024)} مگابایت است؛ "
        "فایل کوچک‌تر یا بخشی از آن را بفرستید."
    )


def is_pdf_document(file_name, mime_type):
    return (mime_type or "").lower() == PDF_MIME or (file_name or "").lower().endswith(".pdf")


def build_pdf_prompt(caption, is_exam):
    caption = (caption or "").strip()
    if is_exam:
        return f"{caption or PDF_EXAM_DEFAULT_PROMPT}\n{PDF_EXAM_EXTRA}"
    return caption or PDF_DEFAULT_PROMPT


def pdf_memory_text(file_name, caption):
    return f"[PDF: {file_name or 'document'}] {(caption or '').strip()}".strip()


def pdf_input(data):
    # A PDF travels through the same "image" channel of the router; only the MIME type differs.
    return {"data": data, "mime": PDF_MIME}
