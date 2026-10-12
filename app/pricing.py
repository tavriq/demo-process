"""Prices in rubles per 1M tokens and the worst cases: one call, one run and one interview.

Prices: Timeweb AI Gateway cabinet, «Модели». Text models 07.10.2026 18:45 MSK and
08.10.2026 12:05 MSK (flash-lite, 4.1 mini, Haiku), speech models 08.10.2026 11:13 MSK (as in
demo-voice). The page does not say whether VAT is included. The gateway does not return prices in
the API, so they live here and must be updated by hand.

Speech recognition returns usage: 10 audio tokens per second of speech (demo-voice probe 08.10).
Its worst case is the longest answer the contract allows (``max_audio_s``), or less for a small
upload: the duration of N bytes at the lowest bitrate the page records with. The server cannot
measure a recording before recognition, so a crafted long recording at a very low bitrate can
cost more than its reserve; the real cost is settled after the call.

Worst case of an interview (docs/SPEC-analyst.md, «Деньги и лимиты»): ``max_questions`` ×
(recognition + draft + question) + the final extract and improve with a retry each. A text
interview never spends the recognition part, but the formula holds it anyway.

Worst case of a call: the prompt at 2 characters per token (Russian text is never denser, so
the input is overestimated) plus ``max_tokens`` of output. A run starts only if the rest of the
day covers both steps with a retry each (docs/SPEC.md, «Деньги, лимиты, данные»).
"""

from __future__ import annotations

import math

PRICES_RUB_PER_1M: dict[str, tuple[float, float]] = {
    "yandex/yandexgpt-lite": (210, 210),
    "timeweb/gpt-oss-120b": (20, 82),
    "timeweb/gemma4:31b": (189, 540),
    "gemini/gemini-3.1-flash-lite": (34, 203),
    "openai/gpt-4.1-mini": (54, 216),
    "anthropic/claude-haiku-4-5": (135, 1080),
    "anthropic/claude-haiku-5-5": (14, 68),
    "openai/gpt-5.6-terra": (270, 1620),
    "openai/gpt-4o-transcribe": (810, 1350),
    "openai/gpt-4o-mini-transcribe": (405, 675),
}
# Unknown model: the most expensive known input and output prices, so budgets stay on the safe side.
FALLBACK = (max(p[0] for p in PRICES_RUB_PER_1M.values()), max(p[1] for p in PRICES_RUB_PER_1M.values()))

# Recognition (demo-voice probe 08.10): 10 audio tokens per second, text out ~4 per second, 8 assumed;
# the request and the short prompt are ~40 text tokens.
STT_AUDIO_TOKENS_PER_S = 10
STT_TEXT_TOKENS_PER_S = 8
STT_PROMPT_TOKENS = 50
# The lowest bitrate the page records with (demo-voice): the longest an upload of N bytes can be.
MIN_AUDIO_BITRATE = 32_000

CHARS_PER_TOKEN_IN = 2
# ``improve`` gets the checked Process and a retry gets the previous answer back. Their size is
# bounded by max_tokens of the step that wrote them, at most this many characters per token
# (app.prompts.with_errors cuts the previous answer to that).
CHARS_PER_TOKEN_OUT = 4
# Block markers and the remarks around the embedded inputs.
WRAP_CHARS = 1_000
# The list of errors in a retry (app.prompts.MAX_ERRORS lines).
ERRORS_CHARS = 2_500
STEPS = ("extract", "improve")


def call_cost_rub(model: str, tokens_in: int, tokens_out: int) -> float:
    pin, pout = PRICES_RUB_PER_1M.get(model, FALLBACK)
    return (tokens_in * pin + tokens_out * pout) / 1_000_000


def prompt_chars(messages: list[dict]) -> int:
    return sum(len(m.get("content") or "") for m in messages if isinstance(m.get("content"), str))


def call_worst_rub(model: str, chars: int, max_tokens: int) -> float:
    """The most a call can cost: ``chars`` of prompt and ``max_tokens`` of answer."""
    return call_cost_rub(model, math.ceil(chars / CHARS_PER_TOKEN_IN), max_tokens)


def messages_worst_rub(model: str, messages: list[dict], max_tokens: int) -> float:
    return call_worst_rub(model, prompt_chars(messages), max_tokens)


def step_input_chars(settings, step: str, retry: bool = False) -> int:
    """Upper bound of a step's prompt without its system prompt."""
    chars = WRAP_CHARS + {
        "extract": settings.max_text_chars,
        "improve": settings.extract_max_tokens * CHARS_PER_TOKEN_OUT,
    }[step]
    if retry:
        chars += settings.step(step).max_tokens * CHARS_PER_TOKEN_OUT + ERRORS_CHARS
    return chars


def step_worst_rub(settings, step: str, retry: bool = False) -> float:
    from app.prompts import SYSTEM  # prompts import this module; imported late

    conf = settings.step(step)
    return call_worst_rub(conf.model, len(SYSTEM[step]) + step_input_chars(settings, step, retry), conf.max_tokens)


def run_worst_rub(settings) -> float:
    """Both steps, each with its retry: what a run may spend at most."""
    return sum(step_worst_rub(settings, s) + step_worst_rub(settings, s, retry=True) for s in STEPS)


# ---------- the interview (docs/SPEC-analyst.md) ----------

# Bounds of what goes into the interview prompts; app.prompts cuts every part to them.
MAX_QUESTION_CHARS = 160
TURN_WRAP_CHARS = 40       # «Вопрос N:», «Ответ:», line breaks
TOPIC_CHARS = 40
STEP_LINE_CHARS = 150      # a line of the current draft: id, kind, name, role
ROLE_LINE_CHARS = 60
TITLE_CHARS = 100
GAP_CHARS = 400            # what the code asks about: the gap, the step and the roles by name
MAX_DRAFT_STEPS, MAX_DRAFT_ROLES = 15, 6


def stt_seconds(audio_bytes: int, max_s: float) -> float:
    return min(float(max_s), audio_bytes * 8 / MIN_AUDIO_BITRATE)


def stt_worst_rub(model: str, audio_bytes: int, max_s: float) -> float:
    """The most a recognition of ``audio_bytes`` can cost, for a recording of at most ``max_s``."""
    seconds = stt_seconds(audio_bytes, max_s)
    return call_cost_rub(model, STT_PROMPT_TOKENS + math.ceil(seconds * STT_AUDIO_TOKENS_PER_S),
                         math.ceil(seconds * STT_TEXT_TOKENS_PER_S))


def transcript_chars(settings) -> int:
    """The whole interview as the models see it: the topic, the visitor's own phrase and every
    question with its answer."""
    return TOPIC_CHARS + settings.max_answer_chars + settings.max_questions * (
        MAX_QUESTION_CHARS + settings.max_answer_chars + TURN_WRAP_CHARS)


def draft_list_chars() -> int:
    """The current draft as a list of steps and roles (app.prompts.draft_lines)."""
    return TITLE_CHARS + MAX_DRAFT_STEPS * STEP_LINE_CHARS + MAX_DRAFT_ROLES * ROLE_LINE_CHARS


def interview_input_chars(settings, step: str, retry: bool = False) -> int:
    """Upper bound of an interview step's prompt without its system prompt."""
    if step == "improve":
        return step_input_chars(settings, "improve", retry)
    chars = WRAP_CHARS + {
        "draft": transcript_chars(settings) + draft_list_chars(),
        "ask": TOPIC_CHARS + settings.max_answer_chars + TITLE_CHARS + GAP_CHARS,
        "extract": transcript_chars(settings) + draft_list_chars(),
    }[step]
    if retry:
        chars += settings.step(step).max_tokens * CHARS_PER_TOKEN_OUT + ERRORS_CHARS
    return chars


def interview_step_worst_rub(settings, step: str, retry: bool = False) -> float:
    from app.prompts import INTERVIEW_SYSTEM  # prompts import this module; imported late

    conf = settings.step(step)
    return call_worst_rub(conf.model, len(INTERVIEW_SYSTEM[step]) + interview_input_chars(settings, step, retry),
                          conf.max_tokens)


def interview_turn_worst_rub(settings) -> float:
    """One question: recognition of the longest answer, the draft and the next question."""
    return (stt_worst_rub(settings.stt_model, settings.max_audio_bytes, settings.max_audio_s)
            + interview_step_worst_rub(settings, "draft") + interview_step_worst_rub(settings, "ask"))


def interview_final_worst_rub(settings) -> float:
    """The final extract and improve over the transcript, each with its retry."""
    return sum(interview_step_worst_rub(settings, s) + interview_step_worst_rub(settings, s, retry=True)
               for s in STEPS)


def interview_worst_rub(settings) -> float:
    """Worst case of one interview: max_questions × (STT + draft + question) + the final with
    retries (docs/SPEC-analyst.md, «Деньги и лимиты»). An interview starts only if the rest of the
    day covers it, and holds it at once."""
    return settings.max_questions * interview_turn_worst_rub(settings) + interview_final_worst_rub(settings)
