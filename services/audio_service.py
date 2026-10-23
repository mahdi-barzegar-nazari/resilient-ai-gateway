"""Speech-to-text with Whisper (served by Groq)."""
import os

import config
from services import clients

# Extensions accepted by the Whisper endpoint
SUPPORTED_AUDIO_EXTS = {"flac", "mp3", "mp4", "mpeg", "mpga", "m4a", "ogg", "opus", "wav", "webm"}


def audio_filename(file_path, default_ext="ogg"):
    """Build a file name with an extension Whisper accepts (Telegram's ``.oga`` becomes ``.ogg``)."""
    ext = os.path.splitext(file_path or "")[1].lower().lstrip(".")
    if ext == "oga":
        ext = "ogg"
    if ext not in SUPPORTED_AUDIO_EXTS:
        ext = default_ext
    return f"audio.{ext}"


def transcribe_audio(audio_bytes, filename):
    """Transcribe audio bytes to text."""
    if not clients.groq_client:
        raise RuntimeError("تبدیل صدا به متن به GROQ_API_KEY نیاز دارد.")
    kwargs = {"file": (filename, audio_bytes), "model": config.MODEL_WHISPER, "temperature": 0.0}
    if config.WHISPER_LANGUAGE:
        kwargs["language"] = config.WHISPER_LANGUAGE
    resp = clients.groq_client.audio.transcriptions.create(**kwargs)
    text = getattr(resp, "text", None)
    if text is None:
        text = str(resp)
    return text.strip()
