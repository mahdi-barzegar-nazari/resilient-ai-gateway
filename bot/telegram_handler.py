"""Telegram adapter (pyTelegramBotAPI).

Responsibilities: receive updates, enforce the allow-list, download media, render replies
(HTML + chunking + inline keyboards). All decision logic lives in ``core`` and ``services``.
"""
import functools
import html
import logging
import time

import telebot
from telebot.apihelper import ApiTelegramException
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

import config
from bot import messages
from core.pipeline import run_chat
from core.session import PLATFORM_TELEGRAM, SETTING_ACTIONS, sessions
from services import clients, quiz_service
from services.ai_router import quick_web_answer
from services.audio_service import audio_filename, transcribe_audio
from services.media_service import (
    IMAGE_NEEDS_GEMINI_TEXT, PDF_NEEDS_GEMINI_TEXT, PDF_ONLY_TEXT,
    build_image_prompt, build_pdf_prompt, image_input, image_memory_text,
    is_pdf_document, pdf_input, pdf_memory_text, pdf_too_big_text,
)
from services.tools import TOOLS, run_tool, split_command
from services.web_service import fetch_web_context
from utils.formatters import split_message, to_telegram_html
from utils.security import is_authorized, redact_secrets, short_err

log = logging.getLogger(__name__)


def _authorized(handler):
    """Silently ignore messages of users that are not on the allow-list."""
    @functools.wraps(handler)
    def wrapper(self, message):
        if not is_authorized(message.from_user.id):
            return None
        return handler(self, message)
    return wrapper


def quiz_keyboard(quiz_id):
    kb = InlineKeyboardMarkup()
    kb.row(*[InlineKeyboardButton(letter, callback_data=f"quiz:{letter}:{quiz_id}") for letter in "ABCD"])
    return kb


def quiz_next_keyboard():
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton("➡️ سؤال بعدی", callback_data="quiz:next"))
    return kb


def panel_keyboard(session):
    """Quick-access inline keyboard; the active option of every group is marked with ✅."""
    def btn(label, action, active):
        return InlineKeyboardButton(("✅ " if active else "") + label, callback_data=f"cmd:{action}")

    kb = InlineKeyboardMarkup()
    kb.row(
        btn("⚡️ سریع", "fast", session.model == config.MODEL_FAST),
        btn("🧠 هوشمند", "smart", session.model == config.MODEL_SMART),
        btn("🌐 Gemini", "gemini", session.model == config.MODEL_GEMINI),
    )
    kb.row(btn("💾 حافظه روشن", "memory_on", session.memory_on), btn("💾 حافظه خاموش", "memory_off", not session.memory_on))
    kb.row(btn("🎯 آزمون روشن", "exam_on", session.exam_mode), btn("🎯 آزمون خاموش", "exam_off", not session.exam_mode))
    kb.row(btn("📜 بلند روشن", "long_on", session.long_mode), btn("📜 بلند خاموش", "long_off", not session.long_mode))
    return kb


def welcome_text(session):
    mem_state = "✅ روشن" if session.memory_on else "❌ خاموش"
    exam_state = "🎯 فعال" if session.exam_mode else "⚪️ غیرفعال"
    long_state = "📜 فعال" if session.long_mode else "⚪️ غیرفعال"
    return (
        f"سلام! دستیار هوش مصنوعی در تلگرام آماده است.\n\n"
        f"⚙️ <b>موتور فعال:</b> <code>{session.model}</code>\n"
        f"💾 <b>وضعیت حافظه:</b> {mem_state}\n"
        f"🎯 <b>حالت آزمون:</b> {exam_state}\n"
        f"📜 <b>خروجی بلند:</b> {long_state}\n\n"
        f"👇 با دکمه‌های زیر می‌توانید بدون تایپ دستور، وضعیت‌ها را تغییر دهید.\n"
        f"🎙️ ویس، 🖼️ عکس و 🔗 لینک هم بفرستید؛ پاسخ یا خلاصه‌ی تحلیلی می‌دهم.\n\n"
        f"<b>دستورات:</b>\n"
        f"• /fast : فوق‌سریع Qwen\n"
        f"• /smart : فوق‌هوشمند (120B)\n"
        f"• /gemini : مدل Gemini متصل به وب\n"
        f"• /exam_on : فعال‌سازی حل آزمون زبان\n"
        f"• /exam_off : غیرفعال‌سازی آزمون زبان\n"
        f"• /long_on : فعال‌سازی خروجی بلند (کدنویسی کامل)\n"
        f"• /long_off : غیرفعال‌سازی خروجی بلند\n"
        f"• /memory_on : روشن کردن حافظه\n"
        f"• /memory_off : خاموش کردن حافظه\n"
        f"• /clear : پاکسازی سابقه مکالمه\n"
        f"• /panel : نمایش دوباره‌ی این پنل\n\n"
        f"<b>ابزارهای دانشجویی:</b>\n"
        f"• /debug متن خطا : ریشه‌یابی باگ و کد اصلاح‌شده\n"
        f"• /read لینک : متن کامل صفحه‌ی وب\n"
        f"• /wiki موضوع : متن مقاله‌ی ویکی‌پدیا\n"
        f"• /arxiv عبارت یا شناسه : جستجوی مقاله و چکیده\n"
        f"• /arxiv_full شناسه : خلاصه‌ی فارسی کل مقاله\n"
        f"• /quiz : آزمون چهارگزینه‌ای (پایان: /quiz_stop)\n"
        f"• 📄 فایل PDF بفرستید تا خوانده و تحلیل شود"
    )


class TelegramApp:
    def __init__(self, token):
        self.bot = telebot.TeleBot(token)
        self._register_handlers()

    def run(self):
        self.bot.infinity_polling()

    # ------------------------------------------------------------ wiring
    def _register_handlers(self):
        b = self.bot
        b.message_handler(commands=['start', 'panel', 'menu'])(self.on_start)
        b.callback_query_handler(func=lambda call: bool(call.data) and call.data.startswith("cmd:"))(self.on_panel_callback)
        b.message_handler(commands=['help'])(self.on_help)
        for action in SETTING_ACTIONS:
            b.message_handler(commands=[action])(self._make_setting_handler(action))
        b.message_handler(commands=['clear'])(self.on_clear)
        b.message_handler(commands=['testweb'])(self.on_testweb)
        b.message_handler(commands=['web', 'search'])(self.on_web)
        b.message_handler(commands=list(TOOLS))(self.on_tool)
        b.message_handler(commands=['quiz'])(self.on_quiz)
        b.message_handler(commands=['quiz_stop'])(self.on_quiz_stop)
        b.callback_query_handler(func=lambda call: bool(call.data) and call.data.startswith("quiz:"))(self.on_quiz_callback)
        b.message_handler(content_types=['document'])(self.on_document)
        b.message_handler(content_types=['voice', 'audio'])(self.on_voice)
        b.message_handler(content_types=['photo'])(self.on_photo)
        # Catch-all: telebot uses the first matching handler, so this must stay LAST.
        b.message_handler(func=lambda message: True)(self.on_text)

    def _session(self, chat_id):
        return sessions.get(PLATFORM_TELEGRAM, chat_id)

    # ------------------------------------------------------------ sending
    def _send(self, message, text, first, **kwargs):
        # first chunk as a reply, the rest as plain follow-up messages
        if first:
            return self.bot.reply_to(message, text, **kwargs)
        return self.bot.send_message(message.chat.id, text, **kwargs)

    def reply(self, message, text, escape=False, reply_markup=None):
        """Send text as one or more messages: HTML formatting, chunking, plain-text fallback.

        The inline keyboard is attached to the last chunk only.
        """
        if not text or not text.strip():
            self.bot.reply_to(message, messages.NO_RESPONSE)
            return

        chunks = split_message(text, config.TG_CHUNK_SIZE)
        for index, chunk in enumerate(chunks):
            formatted = to_telegram_html(chunk, escape=escape)
            extra = {"reply_markup": reply_markup} if (reply_markup and index == len(chunks) - 1) else {}
            try:
                self._send(message, formatted, index == 0, parse_mode='HTML', **extra)
            except ApiTelegramException:
                # Telegram's HTML parser rejected the text: resend it as plain text
                try:
                    self._send(message, chunk, index == 0, **extra)
                except ApiTelegramException as e:
                    log.error("Telegram send error: %s", short_err(e))
            if index < len(chunks) - 1:
                time.sleep(0.4)

    def answer(self, message, user_text, image=None, memory_text=None):
        """Shared reply path for text, transcribed voice, images and PDFs."""
        chat_id = message.chat.id
        self.bot.send_chat_action(chat_id, 'typing')
        reply = run_chat(self._session(chat_id), user_text, image=image, memory_text=memory_text)
        self.reply(message, reply, escape=True)

    def _answer_callback(self, call, text=None):
        try:
            self.bot.answer_callback_query(call.id, text)
        except ApiTelegramException as e:
            log.warning("Callback answer error: %s", short_err(e))

    def _clear_buttons(self, chat_id, message_id):
        try:
            self.bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
        except ApiTelegramException as e:
            log.warning("Clear buttons error: %s", short_err(e))

    # ------------------------------------------------------------ start / panel / help / settings
    @_authorized
    def on_start(self, message):
        session = self._session(message.chat.id)
        self.reply(message, welcome_text(session), reply_markup=panel_keyboard(session))

    def on_panel_callback(self, call):
        if not is_authorized(call.from_user.id) or call.message is None:
            self._answer_callback(call)
            return
        chat_id = call.message.chat.id
        action = call.data[len("cmd:"):]
        session = self._session(chat_id)
        if not session.apply_action(action):
            self._answer_callback(call)
            return
        self._answer_callback(call, messages.PANEL_TOASTS.get(action, "انجام شد"))
        try:
            self.bot.edit_message_text(
                to_telegram_html(welcome_text(session)),
                chat_id,
                call.message.message_id,
                parse_mode='HTML',
                reply_markup=panel_keyboard(session)
            )
        except ApiTelegramException as e:
            # Pressing the already-active option leaves the text unchanged; Telegram answers with this error.
            if "message is not modified" not in str(e):
                log.warning("Panel update error: %s", short_err(e))

    @_authorized
    def on_help(self, message):
        self.reply(message, messages.TG_HELP_TEXT)

    def _make_setting_handler(self, action):
        """Build the handler of one settings command (/fast, /memory_on, ...)."""
        def handler(message):
            if not is_authorized(message.from_user.id):
                return
            self._session(message.chat.id).apply_action(action)
            self.reply(message, messages.TG_ACTION_REPLIES[action])
        return handler

    @_authorized
    def on_clear(self, message):
        self._session(message.chat.id).clear_history()
        self.reply(message, messages.HISTORY_CLEARED)

    # ------------------------------------------------------------ web tools
    @_authorized
    def on_testweb(self, message):
        _, arg = split_command(message.text)
        query = arg or "قیمت دلار"
        self.bot.send_chat_action(message.chat.id, 'typing')
        data = fetch_web_context(query)
        if data:
            safe_data = html.escape(data[:2000], quote=False)
            self.reply(message, f"🔍 <b>داده‌های وب:</b>\n\n{safe_data}")
        else:
            self.reply(message, "❌ موتور وب‌سرچ پاسخی برنگرداند.")

    @_authorized
    def on_web(self, message):
        _, query = split_command(message.text)
        if not query:
            self.reply(message, "لطفاً عبارت مد نظر را بنویسید.")
            return
        self.bot.send_chat_action(message.chat.id, 'typing')
        try:
            self.reply(message, quick_web_answer(query), escape=True)
        except Exception as e:
            self.reply(message, f"خطا: {redact_secrets(e)}", escape=True)

    # ------------------------------------------------------------ student tools
    @_authorized
    def on_tool(self, message):
        cmd, arg = split_command(message.text)
        chat_id = message.chat.id
        self.bot.send_chat_action(chat_id, 'typing')
        self.reply(message, run_tool(cmd, arg, self._session(chat_id).model), escape=True)

    def _send_quiz(self, message, topic_arg=""):
        chat_id = message.chat.id
        self.bot.send_chat_action(chat_id, 'typing')
        text, quiz_id = quiz_service.start_quiz(
            self._session(chat_id), topic_arg, quiz_service.QUIZ_ANSWER_HINT_BUTTONS
        )
        self.reply(
            message, text, escape=True,
            reply_markup=quiz_keyboard(quiz_id) if quiz_id is not None else None
        )

    @_authorized
    def on_quiz(self, message):
        _, arg = split_command(message.text)
        self._send_quiz(message, arg)

    @_authorized
    def on_quiz_stop(self, message):
        self.reply(message, quiz_service.quiz_summary(self._session(message.chat.id)), escape=True)

    def on_quiz_callback(self, call):
        if not is_authorized(call.from_user.id) or call.message is None:
            self._answer_callback(call)
            return
        chat_id = call.message.chat.id
        parts = call.data.split(":")

        if parts[1] == "next":
            self._answer_callback(call)
            self._clear_buttons(chat_id, call.message.message_id)
            self._send_quiz(call.message)
            return

        if len(parts) != 3 or parts[1] not in ("A", "B", "C", "D") or not parts[2].isdigit():
            self._answer_callback(call)
            return
        result = quiz_service.grade_quiz(self._session(chat_id), parts[1], int(parts[2]))
        if result is None:
            self._answer_callback(call, "این سؤال دیگر فعال نیست؛ /quiz بفرستید.")
            return
        correct, text = result
        self._answer_callback(call, "✅ درست" if correct else "❌ نادرست")
        self._clear_buttons(chat_id, call.message.message_id)
        self.reply(call.message, text, escape=True, reply_markup=quiz_next_keyboard())

    # ------------------------------------------------------------ media
    @_authorized
    def on_document(self, message):
        chat_id = message.chat.id
        doc = message.document
        if not is_pdf_document(doc.file_name, doc.mime_type):
            self.reply(message, PDF_ONLY_TEXT)
            return
        if not clients.gemini_client:
            self.reply(message, PDF_NEEDS_GEMINI_TEXT)
            return
        if (doc.file_size or 0) > config.MAX_PDF_BYTES:
            self.reply(message, pdf_too_big_text())
            return

        self.bot.send_chat_action(chat_id, 'typing')
        try:
            file_info = self.bot.get_file(doc.file_id)
            pdf_bytes = self.bot.download_file(file_info.file_path)
        except Exception as e:
            self.reply(message, f"خطا در دریافت فایل: {redact_secrets(e)}", escape=True)
            return
        if len(pdf_bytes) > config.MAX_PDF_BYTES:
            self.reply(message, pdf_too_big_text())
            return

        caption = (message.caption or "").strip()
        is_exam = self._session(chat_id).exam_mode
        self.answer(
            message,
            build_pdf_prompt(caption, is_exam),
            image=pdf_input(pdf_bytes),
            memory_text=pdf_memory_text(doc.file_name, caption)
        )

    @_authorized
    def on_voice(self, message):
        is_voice = message.voice is not None
        media = message.voice or message.audio
        if media.duration and media.duration > config.MAX_AUDIO_SECONDS:  # Telegram reports seconds
            self.reply(message, messages.voice_too_long_text(config.MAX_AUDIO_SECONDS))
            return

        self.bot.send_chat_action(message.chat.id, 'typing')
        try:
            file_info = self.bot.get_file(media.file_id)
            audio_bytes = self.bot.download_file(file_info.file_path)
            text = transcribe_audio(audio_bytes, audio_filename(file_info.file_path, "ogg" if is_voice else "mp3"))
        except Exception as e:
            self.reply(message, f"خطا در تبدیل صدا به متن: {redact_secrets(e)}", escape=True)
            return

        if not text:
            self.reply(message, "متنی از این پیام صوتی تشخیص داده نشد.")
            return

        self.reply(message, f"🎙️ <b>متن شنیده‌شده:</b>\n{html.escape(text, quote=False)}")
        self.answer(message, text)

    @_authorized
    def on_photo(self, message):
        chat_id = message.chat.id
        if not clients.gemini_client:
            self.reply(message, IMAGE_NEEDS_GEMINI_TEXT)
            return

        self.bot.send_chat_action(chat_id, 'typing')
        try:
            file_info = self.bot.get_file(message.photo[-1].file_id)
            image_bytes = self.bot.download_file(file_info.file_path)
        except Exception as e:
            self.reply(message, f"خطا در دریافت تصویر: {redact_secrets(e)}", escape=True)
            return

        caption = (message.caption or "").strip()
        is_exam = self._session(chat_id).exam_mode
        self.answer(
            message,
            build_image_prompt(caption, is_exam),
            image=image_input(image_bytes, file_info.file_path),
            memory_text=image_memory_text(caption)
        )

    # ------------------------------------------------------------ plain text
    @_authorized
    def on_text(self, message):
        session = self._session(message.chat.id)
        letter = quiz_service.parse_quiz_letter(message.text)
        if letter and quiz_service.has_active_quiz(session):
            result = quiz_service.grade_quiz(session, letter)
            if result is not None:
                self.reply(message, result[1], escape=True, reply_markup=quiz_next_keyboard())
                return
        self.answer(message, message.text)
