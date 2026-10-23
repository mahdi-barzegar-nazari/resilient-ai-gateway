"""Interactive multiple-choice quiz (/quiz).

The LLM is asked for a strict JSON object; the reply is validated (and retried once)
before it is shown. Each question carries an id so that stale inline buttons of an
older question can never be graded against a newer one, and every question can be
answered only once.
"""
import json
import logging
import random
import re

import config
from services.ai_router import process_ai_query
from utils.security import short_err

log = logging.getLogger(__name__)

QUIZ_SYSTEM_PROMPT = """
نقش فعلی تو: طراح آزمون زبان انگلیسی (واژگان و گرامر IELTS / TOEFL).
فقط و فقط یک شیء JSON معتبر برگردان (بدون هیچ توضیح اضافه و بدون بلاک کد) با این کلیدها:
{"question": "...", "options": ["...", "...", "...", "..."], "answer": "A", "explanation": "..."}
قواعد:
- question و options انگلیسی باشند؛ options دقیقاً ۴ گزینه است و پیشوند A) یا B) ندارد.
- answer فقط یکی از حرف‌های A، B، C یا D است و باید دقیقاً با گزینه‌ی درست یکی باشد.
- فقط یک گزینه کاملاً درست باشد؛ سه گزینه‌ی دیگر باید باورپذیر ولی غلط باشند.
- explanation فارسی و آموزشی است: چرا گزینه‌ی درست، درست است و چرا بقیه غلط‌اند (حداکثر ۶ سطر).
"""
# Rotating focus areas keep consecutive questions varied despite the low sampling temperature.
QUIZ_FOCUS_HINTS = [
    "synonyms", "word meaning in context", "collocations", "phrasal verbs", "word forms",
    "prepositions", "verb tenses", "conditionals", "articles and determiners",
    "relative clauses", "academic vocabulary", "idioms",
]
QUIZ_ANSWER_HINT_TEXT = "پاسخ را با ارسال حرف A تا D بدهید."
QUIZ_ANSWER_HINT_BUTTONS = "با دکمه‌ها یا ارسال حرف A تا D پاسخ بدهید."


def parse_quiz_json(text):
    """Extract and validate the quiz JSON from a model reply. Raises ``ValueError`` if invalid."""
    cleaned = re.sub(r'<think>.*?</think>', '', text or "", flags=re.DOTALL).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("JSON not found")
    data = json.loads(cleaned[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("JSON is not an object")
    question = str(data.get("question", "")).strip()
    options = [re.sub(r'^\s*[A-Da-d][\).:]\s+', '', str(o)).strip() for o in (data.get("options") or [])]
    answer = str(data.get("answer", "")).strip().upper()[:1]
    explanation = str(data.get("explanation", "")).strip()
    if not question or len(options) != 4 or not all(options) or answer not in ("A", "B", "C", "D") or not explanation:
        raise ValueError("invalid quiz structure")
    return {"question": question, "options": options, "answer": answer, "explanation": explanation}


def parse_quiz_letter(text):
    """Map a typed answer (A-D, a-d or 1-4) to 'A'..'D'; ``None`` if the text is not an answer."""
    m = re.fullmatch(r'\s*([A-Da-d1-4])\s*[\).]?\s*', text or "")
    if not m:
        return None
    ch = m.group(1).upper()
    return "ABCD"[int(ch) - 1] if ch.isdigit() else ch


def has_active_quiz(session):
    return session.quiz is not None


def _stats(session):
    if session.quiz_stats is None:
        session.quiz_stats = {
            "correct": 0, "total": 0, "seq": 0, "topic": config.QUIZ_DEFAULT_TOPIC, "recent": [],
        }
    return session.quiz_stats


def start_quiz(session, topic_arg, answer_hint):
    """Generate a new question and store it as the session's active quiz.

    Returns ``(text, quiz_id)``. On failure ``quiz_id`` is ``None`` and ``text`` is an error message.
    """
    stats = _stats(session)
    if topic_arg:
        stats["topic"] = topic_arg[:80]
    # The prompt adapts difficulty to the running score and lists recent questions to avoid repeats.
    prompt = (
        f"موضوع آزمون: {stats['topic']}\n"
        f"تمرکز این سؤال: {random.choice(QUIZ_FOCUS_HINTS)} (اگر با موضوع کاربر سازگار نیست نادیده بگیر)\n"
        f"عملکرد کاربر: {stats['correct']} پاسخ درست از {stats['total']} سؤال؛ سطح سختی را متناسب با آن تنظیم کن."
    )
    if stats["recent"]:
        prompt += "\nاین سؤال‌ها را تکرار نکن: " + " | ".join(stats["recent"])

    quiz = None
    for _ in range(2):  # one retry when the model returns malformed JSON
        try:
            text, _note = process_ai_query(prompt, model_name=session.model, use_web=False, task_prompt=QUIZ_SYSTEM_PROMPT)
            quiz = parse_quiz_json(text)
            break
        except ValueError as e:  # includes json.JSONDecodeError
            log.warning("[quiz] invalid model output: %s", short_err(e))
        except Exception as e:
            return f"خطا در ساخت سؤال: {short_err(e)}", None
    if quiz is None:
        return "نتوانستم سؤال معتبر بسازم؛ دوباره /quiz را بفرستید.", None

    stats["seq"] += 1
    quiz["id"] = stats["seq"]
    with session.lock:
        session.quiz = quiz
    stats["recent"] = (stats["recent"] + [quiz["question"][:60]])[-8:]

    lines = [f"🧠 سؤال {stats['total'] + 1} — {stats['topic']}", "", quiz["question"], ""]
    lines += [f"{letter}) {opt}" for letter, opt in zip("ABCD", quiz["options"])]
    lines += ["", answer_hint]
    return "\n".join(lines), quiz["id"]


def grade_quiz(session, letter, quiz_id=None):
    """Grade an answer. Returns ``(is_correct, result_text)`` or ``None``.

    ``None`` means there is no active question (or ``quiz_id`` does not match it). The question is
    consumed atomically, so a double tap cannot be scored twice.
    """
    with session.lock:
        quiz = session.quiz
        if not quiz or (quiz_id is not None and quiz["id"] != quiz_id):
            return None
        session.quiz = None
    stats = _stats(session)
    stats["total"] += 1
    correct = (letter == quiz["answer"])
    if correct:
        stats["correct"] += 1
    right_option = quiz["options"]["ABCD".index(quiz["answer"])]
    head = "✅ درست!" if correct else f"❌ نادرست. پاسخ صحیح: {quiz['answer']}) {right_option}"
    return correct, f"{head}\n\n💡 {quiz['explanation']}\n\n📊 امتیاز: {stats['correct']} از {stats['total']}"


def quiz_summary(session):
    """End the quiz and return the final score text."""
    with session.lock:
        session.quiz = None
    stats, session.quiz_stats = session.quiz_stats, None
    if not stats or not stats["total"]:
        return "آزمونی در جریان نبود. برای شروع /quiz را بفرستید."
    percent = round(100 * stats["correct"] / stats["total"])
    return f"🏁 آزمون پایان یافت.\n📊 امتیاز نهایی: {stats['correct']} از {stats['total']} ({percent}٪)"
