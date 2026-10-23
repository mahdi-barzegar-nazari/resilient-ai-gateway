"""Command-style tools shared by every platform: /debug /read /wiki /arxiv /arxiv_full."""
import config
from core.errors import ToolError
from services.ai_router import process_ai_query
from services.knowledge_service import tool_wiki, tool_arxiv, tool_arxiv_full
from services.web_service import tool_read
from utils.security import short_err

DEBUG_SYSTEM_PROMPT = """
نقش فعلی تو: مهندس نرم‌افزار ارشد و مربی دیباگ (جایگزین Stack Overflow برای زمانی که کاربر به مراجع آنلاین دسترسی ندارد).
کاربر متن خطای کامپایلر/مفسر یا کد معیوب را می‌فرستد (Python، C/C++، Java، JavaScript و ...).
پاسخ را دقیقاً با این ساختار و به فارسی بنویس (نام‌ها، پیام‌های خطا و کدها انگلیسی بمانند):
🔍 ریشه‌ی باگ: علت اصلی را دقیق و کوتاه توضیح بده (کدام خط/مفهوم و چرا).
🛠 راه‌حل گام‌به‌گام: مراحل شماره‌دار.
✅ کد اصلاح‌شده: کد کامل و اجراشدنی داخل بلاک کد با نام زبان.
🛡 پیشگیری: چگونه دوباره دچار این خطا نشویم.
اگر اطلاعات برای تشخیص قطعی کافی نیست، محتمل‌ترین علت را بگو، فرض‌هایت را صریح بنویس و بپرس چه چیزی (نسخه، کد مرتبط) لازم است.
"""


def split_command(text):
    """Split ``'/cmd@bot arguments'`` into ``(cmd, arguments)``; the command is lower-cased."""
    parts = (text or "").strip().split(None, 1)
    cmd = parts[0].lstrip("/").split("@")[0].lower() if parts else ""
    arg = parts[1].strip() if len(parts) > 1 else ""
    return cmd, arg


def tool_debug(arg, model_name=None):
    if not arg:
        raise ToolError("متن خطا یا کد معیوب را بعد از دستور بفرستید.\nمثال: /debug TypeError: 'NoneType' object is not subscriptable")
    text, note = process_ai_query(
        arg, model_name=model_name or config.MODEL_FAST, is_long=True, use_web=False, task_prompt=DEBUG_SYSTEM_PROMPT
    )
    return text + note


TOOLS = {
    "debug": tool_debug,
    "read": tool_read,
    "wiki": tool_wiki,
    "arxiv": tool_arxiv,
    "arxiv_full": tool_arxiv_full,
}


def run_tool(cmd, arg, model_name):
    """Run a tool and turn every failure into displayable text; the result is always a string."""
    try:
        return TOOLS[cmd](arg, model_name)
    except ToolError as e:
        return str(e)
    except Exception as e:
        return f"خطا: {short_err(e)}"
