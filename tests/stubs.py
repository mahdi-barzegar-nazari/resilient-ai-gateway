"""Lightweight stand-ins for third-party SDKs, installed only when the real package is missing.

With ``pip install -r requirements.txt`` the real libraries are used and nothing here runs.
The stubs never touch the network: every network-facing method raises AssertionError, so a
test that forgets to mock a call fails loudly instead of hanging.
"""
import sys
import types as pytypes


def _no_network(name):
    def _raise(*args, **kwargs):
        raise AssertionError(f"unexpected network call in a test: {name}")
    return _raise


def install_groq_stub():
    try:
        import groq  # noqa: F401
        return
    except ImportError:
        pass

    class Groq:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.chat = pytypes.SimpleNamespace(completions=pytypes.SimpleNamespace(create=_no_network("groq.chat")))
            self.audio = pytypes.SimpleNamespace(transcriptions=pytypes.SimpleNamespace(create=_no_network("groq.audio")))

    module = pytypes.ModuleType("groq")
    module.Groq = Groq
    sys.modules["groq"] = module


def install_genai_stub():
    try:
        from google import genai  # noqa: F401
        from google.genai import types  # noqa: F401
        return
    except ImportError:
        pass

    class Client:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.models = pytypes.SimpleNamespace(generate_content=_no_network("gemini.generate_content"))

    class Part:
        def __init__(self, text=None, data=None, mime_type=None):
            self.text, self.data, self.mime_type = text, data, mime_type

        @classmethod
        def from_bytes(cls, data, mime_type):
            return cls(data=data, mime_type=mime_type)

    class Content:
        def __init__(self, role, parts):
            self.role, self.parts = role, parts

    class _Config:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    types_module = pytypes.ModuleType("google.genai.types")
    types_module.HttpOptions = _Config
    types_module.GenerateContentConfig = _Config
    types_module.Part = Part
    types_module.Content = Content

    genai_module = pytypes.ModuleType("google.genai")
    genai_module.Client = Client
    genai_module.types = types_module

    google_module = sys.modules.get("google") or pytypes.ModuleType("google")
    google_module.genai = genai_module
    sys.modules["google"] = google_module
    sys.modules["google.genai"] = genai_module
    sys.modules["google.genai.types"] = types_module


def install_telebot_stub():
    try:
        import telebot  # noqa: F401
        return
    except ImportError:
        pass

    class ApiTelegramException(Exception):
        def __init__(self, function_name="", result=None, result_json=None):
            result_json = result_json or {}
            super().__init__(f"Error code: {result_json.get('error_code')}. Description: {result_json.get('description')}")

    class InlineKeyboardButton:
        def __init__(self, text, callback_data=None, **kwargs):
            self.text, self.callback_data = text, callback_data

    class InlineKeyboardMarkup:
        def __init__(self, row_width=3):
            self.keyboard = []

        def row(self, *buttons):
            self.keyboard.append(list(buttons))
            return self

    class TeleBot:
        def __init__(self, token, **kwargs):
            self.token = token
            self.message_handlers = []
            self.callback_query_handlers = []

        def message_handler(self, **filters):
            def decorator(function):
                self.message_handlers.append({"function": function, "filters": filters})
                return function
            return decorator

        def callback_query_handler(self, func, **kwargs):
            def decorator(function):
                self.callback_query_handlers.append({"function": function, "filters": {"func": func}})
                return function
            return decorator

        reply_to = _no_network("reply_to")
        send_message = _no_network("send_message")
        send_chat_action = _no_network("send_chat_action")
        answer_callback_query = _no_network("answer_callback_query")
        edit_message_text = _no_network("edit_message_text")
        edit_message_reply_markup = _no_network("edit_message_reply_markup")
        get_file = _no_network("get_file")
        download_file = _no_network("download_file")
        infinity_polling = _no_network("infinity_polling")

    apihelper = pytypes.ModuleType("telebot.apihelper")
    apihelper.ApiTelegramException = ApiTelegramException
    types_module = pytypes.ModuleType("telebot.types")
    types_module.InlineKeyboardMarkup = InlineKeyboardMarkup
    types_module.InlineKeyboardButton = InlineKeyboardButton
    module = pytypes.ModuleType("telebot")
    module.TeleBot = TeleBot
    module.apihelper = apihelper
    module.types = types_module
    sys.modules.update({"telebot": module, "telebot.apihelper": apihelper, "telebot.types": types_module})


def install_all():
    install_groq_stub()
    install_genai_stub()
    install_telebot_stub()
