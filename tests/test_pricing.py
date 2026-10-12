from dataclasses import replace

import pytest

from app import prompts
from app.config import Settings
from app.pricing import (CHARS_PER_TOKEN_OUT, call_cost_rub, call_worst_rub, interview_final_worst_rub,
                         interview_step_worst_rub, interview_turn_worst_rub, interview_worst_rub, messages_worst_rub,
                         run_worst_rub, step_worst_rub, stt_worst_rub)
from tests.fakes import extract_answer


def test_prices():
    assert call_cost_rub("gemini/gemini-3.1-flash-lite", 1_000_000, 1_000_000) == 237
    assert round(call_cost_rub("anthropic/claude-haiku-5-5", 2000, 1000), 4) == 0.096


def test_worst_call_rounds_the_prompt_up():
    assert call_worst_rub("gemini/gemini-3.1-flash-lite", 3, 0) == call_cost_rub("gemini/gemini-3.1-flash-lite", 2, 0)
    messages = [{"role": "system", "content": "x" * 1000}, {"role": "user", "content": "y" * 1001}]
    assert messages_worst_rub("gemini/gemini-3.1-flash-lite", messages, 100) == \
        call_cost_rub("gemini/gemini-3.1-flash-lite", 1001, 100)


def test_a_run_is_about_two_rubles():
    s = Settings.from_env({})
    worst = run_worst_rub(s)
    assert 1.0 < worst < 2.5 and worst < s.daily_budget_rub / 5
    assert worst == pytest.approx(sum(step_worst_rub(s, st) + step_worst_rub(s, st, retry=True)
                                      for st in ("extract", "improve")))
    flash = Settings.from_env({"EXTRACT_MODEL": "gemini/gemini-3.1-flash-lite",
                               "IMPROVE_MODEL": "gemini/gemini-3.1-flash-lite"})
    assert run_worst_rub(flash) > worst and run_worst_rub(flash) < s.daily_budget_rub / 2


def test_worst_bounds_the_real_prompts():
    """The reservation estimate covers the longest text and a retry that carries a full answer."""
    s = Settings.from_env({})
    text = "а" * s.max_text_chars
    first = prompts.extract_messages(text)
    retry = prompts.with_errors(first, "x" * 100_000, ["ошибка " * 25] * 30, s.extract_max_tokens)
    assert messages_worst_rub(s.extract_model, first, s.extract_max_tokens) <= step_worst_rub(s, "extract")
    assert messages_worst_rub(s.extract_model, retry, s.extract_max_tokens) <= step_worst_rub(s, "extract", True)
    assert len(retry[2]["content"]) == s.extract_max_tokens * CHARS_PER_TOKEN_OUT
    process = {"title": "т", "roles": extract_answer()["roles"], "steps": extract_answer()["steps"], "flags": {}}
    assert messages_worst_rub(s.improve_model, prompts.improve_messages(process), s.improve_max_tokens) <= \
        step_worst_rub(s, "improve")


def test_worst_follows_settings():
    s = Settings.from_env({})
    assert run_worst_rub(replace(s, extract_max_tokens=1000)) < run_worst_rub(s)
    assert run_worst_rub(replace(s, improve_model="someone/new-model")) > run_worst_rub(s)


def test_settings_from_env():
    s = Settings.from_env({"EXTRACT_MODEL": "a/b", "IMPROVE_TIMEOUT_S": "30", "MAX_TEXT_CHARS": "900"})
    assert s.step("extract").model == "a/b" and s.step("improve").timeout_s == 30 and s.max_text_chars == 900
    d = Settings.from_env({})
    assert (d.max_text_chars, d.run_timeout_s, d.daily_budget_rub, d.runs_per_hour, d.max_concurrent_runs) == \
        (1500, 120, 15, 3, 2)
    assert (d.step("extract").max_tokens, d.step("extract").timeout_s) == (6000, 60)
    assert (d.step("improve").max_tokens, d.step("improve").timeout_s) == (3000, 45)
    assert not d.live


# ---------- the interview (docs/SPEC-analyst.md, «Деньги и лимиты») ----------

def test_interview_worst_is_the_formula():
    """8 × (STT + draft + question) + the final extract and improve with a retry each."""
    s = Settings.from_env({})
    turn = stt_worst_rub(s.stt_model, s.max_audio_bytes, s.max_audio_s) + interview_step_worst_rub(s, "draft") \
        + interview_step_worst_rub(s, "ask")
    final = sum(interview_step_worst_rub(s, st) + interview_step_worst_rub(s, st, retry=True)
                for st in ("extract", "improve"))
    assert interview_worst_rub(s) == pytest.approx(8 * turn + final)
    assert interview_turn_worst_rub(s) == pytest.approx(turn) and interview_final_worst_rub(s) == pytest.approx(final)
    assert interview_step_worst_rub(s, "improve", True) == step_worst_rub(s, "improve", True)  # the same as v1
    # what it is with the defaults, and that it fits the day at all
    assert 10 < interview_worst_rub(s) < s.daily_budget_rub


def test_stt_worst():
    """30 s at most: 10 audio tokens and 8 text tokens a second; a small upload is shorter (32 kbit/s)."""
    assert stt_worst_rub("openai/gpt-4o-transcribe", 600 * 1024, 30) == pytest.approx(
        call_cost_rub("openai/gpt-4o-transcribe", 50 + 300, 240))
    assert stt_worst_rub("openai/gpt-4o-transcribe", 40_000, 30) == pytest.approx(
        call_cost_rub("openai/gpt-4o-transcribe", 50 + 100, 80))
    assert stt_worst_rub("openai/gpt-4o-mini-transcribe", 600 * 1024, 30) < stt_worst_rub(
        "openai/gpt-4o-transcribe", 600 * 1024, 30)


def test_interview_worst_follows_settings():
    s = Settings.from_env({})
    assert interview_worst_rub(Settings.from_env({"STT_MODEL": "openai/gpt-4o-mini-transcribe"})) < interview_worst_rub(s)
    assert interview_worst_rub(Settings.from_env({"INTERVIEW_MAX_QUESTIONS": "5"})) < interview_worst_rub(s)
    assert interview_worst_rub(Settings.from_env({"DRAFT_MAX_TOKENS": "1000"})) < interview_worst_rub(s)
    assert interview_worst_rub(Settings.from_env({"DRAFT_MODEL": "anthropic/claude-haiku-5-5"})) < interview_worst_rub(s)


def test_interview_worst_bounds_the_real_prompts():
    """The longest transcript, a full draft and the longest gap fit what the reservation counts with."""
    s = Settings.from_env({})
    turns = [{"q": "в" * 500, "a": "о" * 2000} for _ in range(s.max_questions)]
    text = prompts.transcript("заявки", "ф" * 2000, turns, s.max_answer_chars)
    process = extract_answer()
    process["steps"] = [{**st, "name": "ш" * 200} for st in process["steps"]] * 2
    process["title"] = "т" * 500
    for step, messages in (("draft", prompts.draft_messages(text, process)),
                           ("extract", prompts.interview_extract_messages(text, process)),
                           ("ask", prompts.ask_messages("заявки", "ф" * 2000, "т" * 500, "г" * 2000,
                                                        s.max_answer_chars))):
        assert messages_worst_rub(s.step(step).model, messages, s.step(step).max_tokens) <= \
            interview_step_worst_rub(s, step), step
    retry = prompts.with_errors(prompts.interview_extract_messages(text, process), "x" * 100_000,
                                ["ошибка " * 25] * 30, s.extract_max_tokens)
    assert messages_worst_rub(s.extract_model, retry, s.extract_max_tokens) <= \
        interview_step_worst_rub(s, "extract", True)


def test_interview_settings_from_env():
    d = Settings.from_env({})
    assert (d.stt_model, d.draft_model, d.ask_model) == ("openai/gpt-4o-transcribe", "gemini/gemini-3.1-flash-lite",
                                                         "gemini/gemini-3.1-flash-lite")
    assert (d.max_questions, d.min_answers_to_finish, d.max_answer_chars, d.max_audio_bytes, d.max_audio_s) == \
        (8, 3, 500, 600 * 1024, 30)
    assert (d.interview_idle_s, d.interviews_per_hour, d.max_concurrent_interviews) == (600, 3, 2)
    # ASK_MODEL follows DRAFT_MODEL unless set
    s = Settings.from_env({"DRAFT_MODEL": "openai/gpt-4.1-mini", "STT_MODEL": "openai/gpt-4o-mini-transcribe"})
    assert s.step("ask").model == "openai/gpt-4.1-mini" and s.stt_model == "openai/gpt-4o-mini-transcribe"
    assert Settings.from_env({"DRAFT_MODEL": "a/b", "ASK_MODEL": "c/d"}).step("ask").model == "c/d"
