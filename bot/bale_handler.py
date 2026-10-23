"""Bale adapter.

Bale exposes a Telegram-compatible Bot API but offers no official Python SDK, so this
module talks to it directly with ``requests`` and runs its own long-polling loop
(meant to run on a background thread next to Telegram's polling).
"""
import logging
import time

import requests

import config
from bot import messages
from core.pipeline import run_chat
from core.session import PLATFORM_BALE, sessions
from services import clients, quiz_service
from services.audio_service import audio_filename, transcribe_audio
from services.media_service import (
    IMAGE_NEEDS_GEMINI_TEXT, PDF_NEEDS_GEMINI_TEXT, PDF_ONLY_TEXT,
    build_image_prompt, build_pdf_prompt, image_input, image_memory_text,
    is_pdf_document, pdf_input, pdf_memory_text, pdf_too_big_text,
)
from services.tools import TOOLS, run_tool, split_command
from utils.formatters import split_message
from utils.security import is_authorized_bale, redact_secrets, short_err

log = logging.getLogger(__name__)


def bale_voice_seconds(voice):
    """Bale reports media duration in MILLISECONDS (Telegram: seconds); convert to seconds.

    Comparing the raw value with a limit expressed in seconds made a 3-second voice note
    (``duration == 3000``) look like 50 minutes.
    """
    return (voice.get("duration") or 0) / 1000


def bale_largest_photo(photos):
    return max(photos, key=lambda p: ((p.get("file_size") or 0), (p.get("width") or 0) * (p.get("height") or 0)))


def welcome_text(session):
    mem_state = "✅ روشن" if session.memory_on else "❌ خاموش"
    exam_state = "🎯 فعال" if session.exam_mode else "⚪️ غیرفعال"
    long_state = "📜 فعال" if session.long_mode else "⚪️ غیرفعال"
    return (
        f"سلام! دستیار هوش مصنوعی در بله آماده است.\n"
        f"موتور فعال: {session.model}\n"
        f"وضعیت حافظه: {mem_state}\n"
        f"حالت آزمون: {exam_state}\n"
        f"خروجی بلند: {long_state}\n\n"
        f"🎙️ ویس، 🖼️ عکس و 🔗 لینک هم بفرستید؛ پاسخ یا خلاصه‌ی تحلیلی می‌دهم.\n\n"
        f"دستورات:\n"
        f"• /fast : فوق‌سریع Qwen\n"
        f"• /smart : فوق‌هوشمند (120B)\n"
        f"• /gemini : مدل Gemini متصل به وب\n"
        f"• /exam_on : فعال‌سازی حل آزمون زبان\n"
        f"• /exam_off : غیرفعال‌سازی آزمون زبان\n"
        f"• /long_on : فعال‌سازی خروجی بلند (کدنویسی کامل)\n"
        f"• /long_off : غیرفعال‌سازی خروجی بلند\n"
        f"• /memory_on : روشن کردن حافظه\n"
        f"• /memory_off : خاموش کردن حافظه\n"
        f"• /clear : پاکسازی تاریخچه مکالمه\n\n"
        f"ابزارهای دانشجویی:\n"
        f"• /debug متن خطا : ریشه‌یابی باگ و کد اصلاح‌شده\n"
        f"• /read لینک : متن کامل صفحه‌ی وب\n"
        f"• /wiki موضوع : متن مقاله‌ی ویکی‌پدیا\n"
        f"• /arxiv عبارت یا شناسه : جستجوی مقاله و چکیده\n"
        f"• /arxiv_full شناسه : خلاصه‌ی فارسی کل مقاله\n"
        f"• /quiz : آزمون چهارگزینه‌ای (پایان: /quiz_stop)\n"
        f"• 📄 فایل PDF بفرستید تا خوانده و تحلیل شود"
    )


class BaleApp:
    def __init__(self, token):
        self.token = token
        self.base_url = f"{config.BALE_API_BASE}/bot{token}"
        self.offset = 0

    # ------------------------------------------------------------ transport
    def send(self, chat_id, text):
        """Send a message; long texts are split into several consecutive messages."""
        if not text or not text.strip():
            text = messages.NO_RESPONSE
        chunks = split_message(text, config.BALE_CHUNK_SIZE)
        for index, chunk in enumerate(chunks):
            try:
                requests.post(f"{self.base_url}/sendMessage", json={"chat_id": chat_id, "text": chunk}, timeout=10)
            except Exception as e:
                log.error("Bale send error: %s", short_err(e))
            if index < len(chunks) - 1:
                time.sleep(0.4)

    def download(self, file_id):
        """Download a file (getFile, then the file/bot URL). Returns ``(bytes, file_path)``."""
        meta = requests.get(f"{self.base_url}/getFile", params={"file_id": file_id}, timeout=15)
        meta.raise_for_status()
        file_path = (meta.json().get("result") or {}).get("file_path")
        if not file_path:
            raise RuntimeError("مسیر فایل از بله دریافت نشد.")
        if file_path.startswith("http"):
            file_url = file_path
        else:
            file_url = f"{config.BALE_API_BASE}/file/bot{self.token}/{file_path}"
        resp = requests.get(file_url, timeout=30)
        resp.raise_for_status()
        return resp.content, file_path

    # ------------------------------------------------------------ reply path
    def _session(self, chat_id):
        return sessions.get(PLATFORM_BALE, chat_id)

    def answer(self, chat_id, user_text, image=None, memory_text=None):
        """Shared reply path for text, transcribed voice, images and PDFs."""
        reply = run_chat(self._session(chat_id), user_text, image=image, memory_text=memory_text)
        self.send(chat_id, reply)

    # ------------------------------------------------------------ media handlers
    def handle_voice(self, chat_id, voice):
        if bale_voice_seconds(voice) > config.MAX_AUDIO_SECONDS:
            self.send(chat_id, messages.voice_too_long_text(config.MAX_AUDIO_SECONDS))
            return
        try:
            audio_bytes, file_path = self.download(voice["file_id"])
            text = transcribe_audio(audio_bytes, audio_filename(file_path, "ogg"))
        except Exception as e:
            self.send(chat_id, f"خطا در تبدیل صدا به متن: {redact_secrets(e)}")
            return

        if not text:
            self.send(chat_id, "متنی از این پیام صوتی تشخیص داده نشد.")
            return

        self.send(chat_id, f"🎙️ متن شنیده‌شده:\n{text}")
        self.answer(chat_id, text)

    def handle_photo(self, chat_id, photos, caption):
        if not clients.gemini_client:
            self.send(chat_id, IMAGE_NEEDS_GEMINI_TEXT)
            return
        try:
            image_bytes, file_path = self.download(bale_largest_photo(photos)["file_id"])
        except Exception as e:
            self.send(chat_id, f"خطا در دریافت تصویر: {redact_secrets(e)}")
            return

        is_exam = self._session(chat_id).exam_mode
        self.answer(
            chat_id,
            build_image_prompt(caption, is_exam),
            image=image_input(image_bytes, file_path),
            memory_text=image_memory_text(caption)
        )

    def handle_document(self, chat_id, doc, caption):
        if not is_pdf_document(doc.get("file_name"), doc.get("mime_type")):
            self.send(chat_id, PDF_ONLY_TEXT)
            return
        if not clients.gemini_client:
            self.send(chat_id, PDF_NEEDS_GEMINI_TEXT)
            return
        if (doc.get("file_size") or 0) > config.MAX_PDF_BYTES:
            self.send(chat_id, pdf_too_big_text())
            return
        try:
            pdf_bytes, _ = self.download(doc["file_id"])
        except Exception as e:
            self.send(chat_id, f"خطا در دریافت فایل: {redact_secrets(e)}")
            return
        if len(pdf_bytes) > config.MAX_PDF_BYTES:
            self.send(chat_id, pdf_too_big_text())
            return

        is_exam = self._session(chat_id).exam_mode
        self.answer(
            chat_id,
            build_pdf_prompt(caption, is_exam),
            image=pdf_input(pdf_bytes),
            memory_text=pdf_memory_text(doc.get("file_name"), caption)
        )

    # ------------------------------------------------------------ student tools
    def handle_tools(self, chat_id, user_text):
        """/debug /read /wiki /arxiv /arxiv_full /quiz /quiz_stop and typed quiz answers (A-D).

        Returns True when the message was handled (and a reply has been sent).
        """
        session = self._session(chat_id)

        letter = quiz_service.parse_quiz_letter(user_text)
        if letter and quiz_service.has_active_quiz(session):
            result = quiz_service.grade_quiz(session, letter)
            if result is not None:
                self.send(chat_id, result[1] + "\n\nسؤال بعدی: /quiz")
                return True

        if not user_text.startswith("/"):
            return False
        cmd, arg = split_command(user_text)

        if cmd in TOOLS:
            self.send(chat_id, run_tool(cmd, arg, session.model))
            return True
        if cmd == "quiz":
            text, _ = quiz_service.start_quiz(session, arg, quiz_service.QUIZ_ANSWER_HINT_TEXT)
            self.send(chat_id, text)
            return True
        if cmd == "quiz_stop":
            self.send(chat_id, quiz_service.quiz_summary(session))
            return True
        return False

    # ------------------------------------------------------------ message router
    def handle_message(self, msg):
        chat_id = msg.get("chat", {}).get("id")
        from_user_id = msg.get("from", {}).get("id", chat_id)
        user_text = (msg.get("text") or "").strip()
        voice = msg.get("voice") or msg.get("audio")
        photos = msg.get("photo")
        document = msg.get("document")
        caption = (msg.get("caption") or "").strip()

        if not chat_id or not (user_text or voice or photos or document):
            return

        if not is_authorized_bale(from_user_id, chat_id):
            # The numeric id is shown on purpose: it is what the owner must put in ALLOWED_USERS.
            self.send(chat_id, f"⛔️ دسترسی غیرمجاز!\nشناسه عددی بله شما: {from_user_id}")
            return

        session = self._session(chat_id)

        # Media (only when the message carries no text)
        if not user_text and voice:
            self.handle_voice(chat_id, voice)
            return
        if not user_text and photos:
            self.handle_photo(chat_id, photos, caption)
            return
        if not user_text and document:
            self.handle_document(chat_id, document, caption)
            return

        # Settings commands (Bale matches them exactly, without arguments)
        if user_text == '/start':
            self.send(chat_id, welcome_text(session))
            return
        if user_text == '/clear':
            session.clear_history()
            self.send(chat_id, messages.HISTORY_CLEARED)
            return
        if user_text.startswith('/') and user_text[1:] in messages.BALE_ACTION_REPLIES:
            action = user_text[1:]
            session.apply_action(action)
            self.send(chat_id, messages.BALE_ACTION_REPLIES[action])
            return

        if self.handle_tools(chat_id, user_text):
            return

        self.answer(chat_id, user_text)

    # ------------------------------------------------------------ polling
    def poll_once(self):
        """One getUpdates round-trip; a failing update never blocks the ones after it."""
        resp = requests.get(
            f"{self.base_url}/getUpdates",
            params={"offset": self.offset, "timeout": 12},
            timeout=18
        )
        if resp.status_code != 200:
            return
        for update in resp.json().get("result", []):
            self.offset = update["update_id"] + 1
            try:
                self.handle_message(update.get("message") or {})
            except Exception as e:
                log.error("Bale message error: %s", short_err(e))

    def run(self):
        """Long-polling loop (blocking). Network errors are retried forever."""
        try:
            requests.get(f"{self.base_url}/deleteWebhook", timeout=8)  # polling and webhooks are mutually exclusive
        except Exception:
            pass
        log.info("Bale bot background listener started...")

        failing = False
        while True:
            try:
                self.poll_once()
                if failing:
                    log.info("Bale polling recovered")
                    failing = False
                time.sleep(0.5)
            except Exception as e:
                if not failing:  # log once per outage, not every 2 seconds
                    log.warning("Bale polling error: %s", short_err(e))
                    failing = True
                time.sleep(2)
