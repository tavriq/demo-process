"""Settings from the environment. The key is never logged or returned."""

from __future__ import annotations

import os
from dataclasses import dataclass
from ipaddress import IPv4Network, IPv6Network, ip_network
from pathlib import Path

# docs/SPEC.md, «Конвейер»: Haiku on both steps; the eval compares it with gemini-3.1-flash-lite.
DEFAULT_MODEL = "anthropic/claude-haiku-5-5"
# docs/SPEC-analyst.md, «Кто что решает»: a cheap draft model, ASK_MODEL = DRAFT_MODEL by default,
# recognition as in demo-voice.
DEFAULT_DRAFT_MODEL = "gemini/gemini-3.1-flash-lite"
DEFAULT_STT_MODEL = "openai/gpt-4o-transcribe"

# Model steps (docs/SPEC.md, «Конвейер»; docs/SPEC-analyst.md): env prefix, max_tokens, timeout in s.
# draft: the whole Process of up to 15 steps plus the numbers of the last answer; ask: one question
# of up to 160 characters in a JSON object.
STEPS = {
    "extract": ("EXTRACT", 6000, 60),
    "improve": ("IMPROVE", 3000, 45),
    "draft": ("DRAFT", 2000, 30),
    "ask": ("ASK", 300, 15),
}


def parse_trusted_proxies(raw: str) -> tuple[IPv4Network | IPv6Network, ...]:
    """TRUSTED_PROXY: comma-separated IPs or CIDR ranges. Empty = trust nobody."""
    return tuple(ip_network(p.strip(), strict=False) for p in raw.split(",") if p.strip())


@dataclass(frozen=True)
class StepConf:
    model: str
    max_tokens: int
    timeout_s: float


@dataclass(frozen=True)
class Settings:
    api_key: str
    base_url: str
    extract_model: str
    improve_model: str
    daily_budget_rub: float
    runs_per_hour: int
    max_concurrent_runs: int
    max_text_chars: int
    run_timeout_s: float
    retention_days: int
    var_dir: Path
    recorded_dir: Path
    trusted_proxies: tuple
    extract_max_tokens: int
    improve_max_tokens: int
    extract_timeout_s: float
    improve_timeout_s: float
    # The interview (docs/SPEC-analyst.md).
    draft_model: str = DEFAULT_DRAFT_MODEL
    ask_model: str = DEFAULT_DRAFT_MODEL
    draft_max_tokens: int = STEPS["draft"][1]
    ask_max_tokens: int = STEPS["ask"][1]
    draft_timeout_s: float = STEPS["draft"][2]
    ask_timeout_s: float = STEPS["ask"][2]
    stt_model: str = DEFAULT_STT_MODEL
    stt_timeout_s: float = 30
    max_questions: int = 8
    min_answers_to_finish: int = 3
    max_answer_chars: int = 500
    max_audio_bytes: int = 600 * 1024
    max_audio_s: int = 30
    interview_idle_s: float = 600
    interviews_per_hour: int = 3
    max_concurrent_interviews: int = 2

    @property
    def live(self) -> bool:
        return bool(self.api_key and self.base_url)

    def step(self, name: str) -> StepConf:
        """Model, max_tokens and timeout of a model step (a key of STEPS)."""
        p = STEPS[name][0].lower()
        return StepConf(getattr(self, f"{p}_model"), getattr(self, f"{p}_max_tokens"), getattr(self, f"{p}_timeout_s"))

    def models(self) -> dict[str, str]:
        return {**{name: self.step(name).model for name in STEPS}, "stt": self.stt_model}

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Settings":
        env = os.environ if env is None else env
        root = Path(__file__).resolve().parent.parent
        draft_model = env.get("DRAFT_MODEL", DEFAULT_DRAFT_MODEL).strip()
        default_models = {"extract": DEFAULT_MODEL, "improve": DEFAULT_MODEL, "draft": DEFAULT_DRAFT_MODEL,
                          "ask": draft_model}
        per_step = {}
        for name, (prefix, max_tokens, timeout_s) in STEPS.items():
            p = prefix.lower()
            per_step[f"{p}_model"] = env.get(f"{prefix}_MODEL", default_models[name]).strip()
            per_step[f"{p}_max_tokens"] = int(env.get(f"{prefix}_MAX_TOKENS", str(max_tokens)))
            per_step[f"{p}_timeout_s"] = float(env.get(f"{prefix}_TIMEOUT_S", str(timeout_s)))
        return cls(
            api_key=env.get("LLM_API_KEY", "").strip(),
            base_url=env.get("LLM_BASE_URL", "").strip(),
            daily_budget_rub=float(env.get("DAILY_BUDGET_RUB", "15")),
            runs_per_hour=int(env.get("RUNS_PER_HOUR", "3")),
            max_concurrent_runs=int(env.get("MAX_CONCURRENT_RUNS", "2")),
            max_text_chars=int(env.get("MAX_TEXT_CHARS", "1500")),
            run_timeout_s=float(env.get("RUN_TIMEOUT_S", "120")),
            retention_days=int(env.get("RETENTION_DAYS", "30")),
            var_dir=Path(env.get("VAR_DIR", str(root / "var"))),
            recorded_dir=Path(env.get("RECORDED_DIR", str(root / "app" / "recorded"))),
            trusted_proxies=parse_trusted_proxies(env.get("TRUSTED_PROXY", "")),
            stt_model=env.get("STT_MODEL", DEFAULT_STT_MODEL).strip(),
            stt_timeout_s=float(env.get("STT_TIMEOUT_S", "30")),
            max_questions=int(env.get("INTERVIEW_MAX_QUESTIONS", "8")),
            min_answers_to_finish=int(env.get("INTERVIEW_MIN_ANSWERS", "3")),
            max_answer_chars=int(env.get("MAX_ANSWER_CHARS", "500")),
            max_audio_bytes=int(env.get("MAX_AUDIO_BYTES", str(600 * 1024))),
            max_audio_s=int(env.get("MAX_AUDIO_S", "30")),
            interview_idle_s=float(env.get("INTERVIEW_IDLE_S", "600")),
            interviews_per_hour=int(env.get("INTERVIEWS_PER_HOUR", "3")),
            max_concurrent_interviews=int(env.get("MAX_CONCURRENT_INTERVIEWS", "2")),
            **per_step,
        )
