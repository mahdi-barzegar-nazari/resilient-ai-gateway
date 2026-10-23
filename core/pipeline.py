"""Platform-agnostic chat pipeline: link handling -> model routing -> memory update.

Telegram and Bale adapters only differ in how they receive and send messages; the
decision logic below is shared, so a behaviour fix lands on both platforms at once.
"""
from core.session import Session
from services.ai_router import process_ai_query
from services.web_service import prepare_link_prompt
from utils.security import redact_secrets


def run_chat(session: Session, user_text, image=None, memory_text=None):
    """Produce the reply text for one user turn. Never raises; failures become readable text.

    - Links in the message are fetched and summarised (skipped in exam mode and for media input).
    - The exchange is stored in memory only when memory is on and exam mode is off. For links and
      media a short note is stored instead of the (large) prompt.
    """
    try:
        use_web = True
        if image is None and not session.exam_mode:
            link_prompt, link_memory, link_error = prepare_link_prompt(user_text)
            if link_error:
                return link_error
            if link_prompt:
                # The prompt already carries the page text, so live web search is switched off.
                user_text, memory_text, use_web = link_prompt, (memory_text or link_memory), False

        answer, note = process_ai_query(
            user_text,
            model_name=session.model,
            use_memory=session.memory_on,
            history_list=list(session.history),  # a copy: the router must never mutate session state
            is_exam=session.exam_mode,
            is_long=session.long_mode,
            image=image,
            use_web=use_web,
        )
    except Exception as e:
        return f"خطا در پردازش: {redact_secrets(e)}"

    if session.memory_on and not session.exam_mode:
        session.remember(memory_text or user_text, answer)
    return answer + note
