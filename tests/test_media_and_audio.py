import unittest
from types import SimpleNamespace
from unittest import mock

import config
from services import audio_service, clients
from services import media_service as media


class ImageHelperTests(unittest.TestCase):
    def test_mime_from_extension(self):
        self.assertEqual(media.image_mime("photos/a.PNG"), "image/png")
        self.assertEqual(media.image_mime("a.webp"), "image/webp")
        self.assertEqual(media.image_mime("a.heic"), "image/heic")
        self.assertEqual(media.image_mime("a.jpg"), "image/jpeg")
        self.assertEqual(media.image_mime(None), "image/jpeg")

    def test_prompts(self):
        self.assertEqual(media.build_image_prompt("", False), media.IMAGE_DEFAULT_PROMPT)
        self.assertEqual(media.build_image_prompt(" my caption ", False), "my caption")
        exam = media.build_image_prompt("", True)
        self.assertIn(media.IMAGE_EXAM_DEFAULT_PROMPT, exam)
        self.assertIn("RESULT", exam)

    def test_memory_text(self):
        self.assertEqual(media.image_memory_text(""), "[تصویر ارسال شد]")
        self.assertEqual(media.image_memory_text("cap"), "[تصویر] cap")

    def test_image_input_shape(self):
        self.assertEqual(media.image_input(b"x", "a.png"), {"data": b"x", "mime": "image/png"})


class PdfHelperTests(unittest.TestCase):
    def test_detection_by_mime_or_extension(self):
        self.assertTrue(media.is_pdf_document("x.bin", "application/pdf"))
        self.assertTrue(media.is_pdf_document("NOTES.PDF", None))
        self.assertFalse(media.is_pdf_document("a.docx", "application/msword"))
        self.assertFalse(media.is_pdf_document(None, None))

    def test_prompts(self):
        self.assertEqual(media.build_pdf_prompt("", False), media.PDF_DEFAULT_PROMPT)
        self.assertEqual(media.build_pdf_prompt("حل کن", False), "حل کن")
        self.assertIn("RESULT", media.build_pdf_prompt("", True))

    def test_memory_text_and_input(self):
        self.assertEqual(media.pdf_memory_text("a.pdf", " cap "), "[PDF: a.pdf] cap")
        self.assertEqual(media.pdf_memory_text(None, ""), "[PDF: document]")
        self.assertEqual(media.pdf_input(b"%PDF"), {"data": b"%PDF", "mime": "application/pdf"})

    def test_size_message_uses_the_configured_limit(self):
        with mock.patch.object(config, "MAX_PDF_BYTES", 5 * 1024 * 1024):
            self.assertIn("5 مگابایت", media.pdf_too_big_text())


class AudioTests(unittest.TestCase):
    def test_filename_mapping(self):
        self.assertEqual(audio_service.audio_filename("voice/file_1.oga"), "audio.ogg")
        self.assertEqual(audio_service.audio_filename("a/b.MP3"), "audio.mp3")
        self.assertEqual(audio_service.audio_filename("a/b.xyz", "mp3"), "audio.mp3")
        self.assertEqual(audio_service.audio_filename(None), "audio.ogg")

    def test_transcription_passes_language_and_strips_text(self):
        create = mock.Mock(return_value=SimpleNamespace(text="  سلام  "))
        fake = SimpleNamespace(audio=SimpleNamespace(transcriptions=SimpleNamespace(create=create)))
        with mock.patch.object(clients, "groq_client", fake), mock.patch.object(config, "WHISPER_LANGUAGE", "fa"):
            self.assertEqual(audio_service.transcribe_audio(b"ogg", "audio.ogg"), "سلام")
        self.assertEqual(create.call_args.kwargs["language"], "fa")
        self.assertEqual(create.call_args.kwargs["model"], config.MODEL_WHISPER)

    def test_auto_detect_language_omits_the_hint(self):
        create = mock.Mock(return_value=SimpleNamespace(text="hi"))
        fake = SimpleNamespace(audio=SimpleNamespace(transcriptions=SimpleNamespace(create=create)))
        with mock.patch.object(clients, "groq_client", fake), mock.patch.object(config, "WHISPER_LANGUAGE", None):
            audio_service.transcribe_audio(b"ogg", "audio.ogg")
        self.assertNotIn("language", create.call_args.kwargs)

    def test_missing_key_is_a_clear_error(self):
        with mock.patch.object(clients, "groq_client", None):
            with self.assertRaises(RuntimeError) as ctx:
                audio_service.transcribe_audio(b"x", "audio.ogg")
        self.assertIn("GROQ_API_KEY", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
