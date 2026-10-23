"""Provider SDK clients, created once at import time from the configured keys.

A client is ``None`` when its API key is missing; callers must check before use.
Other modules access them as ``clients.groq_client`` (module attribute lookup at call
time) so tests can replace them.
"""
import config
from groq import Groq
from google import genai
from google.genai import types

# With a Gemini fallback available, Groq must fail fast (no SDK-level retries) so the
# failover happens immediately instead of after several backoff rounds.
groq_client = (
    Groq(api_key=config.GROQ_API_KEY, max_retries=0 if config.GEMINI_API_KEY else 2, timeout=60.0)
    if config.GROQ_API_KEY else None
)

gemini_client = (
    genai.Client(api_key=config.GEMINI_API_KEY, http_options=types.HttpOptions(timeout=60000))
    if config.GEMINI_API_KEY else None
)

# Large PDFs can take well over 60 s to process, so they get a dedicated long-timeout client.
gemini_pdf_client = (
    genai.Client(api_key=config.GEMINI_API_KEY, http_options=types.HttpOptions(timeout=180000))
    if config.GEMINI_API_KEY else None
)
