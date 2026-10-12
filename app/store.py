"""SQLite: runs and interviews per client per hour, the daily budget in rubles, runs and interviews
for ``retention_days`` (docs/SPEC.md, «Деньги, лимиты, данные»; docs/SPEC-analyst.md, «Деньги и
лимиты»).

Budget flow, as in demo-arena and demo-voice: ``reserve`` checks that today's spend + the call's
worst case fit the daily budget and records the worst case; ``settle`` replaces it with the real
cost. A run holds one reservation for its worst case (``hold``, status "held": both steps with a
retry each) and moves every call's worst case out of it (``reserve(..., hold=…)``), so a second
run cannot take that money. Holds left by a dead process are freed on startup (``release_holds``).
An interview's calls stay inside its hold (``within_hold``) and give back to it what they did not
spend (``settle(..., hold_id=…)``): an interview never spends more than its worst case.

An interview holds its worst case the same way (app.pricing.interview_worst_rub). Its row keeps the
transcript with contacts masked, the drafts and the result for ``retention_days`` — never audio.

Client keys (IPv4 or IPv6 /64) are stored only as HMAC hashes with a salt that lives in memory
and changes on every start, and only for the hourly limit. Runs keep the text with contacts
masked (app.pii), the checked model answers and the steps, no client key.

A connection per call (WAL), so the store is safe to share between threads.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import secrets
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

HOUR = 3600
MSK = timezone(timedelta(hours=3))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS starts (ip_hash TEXT NOT NULL, ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS starts_ip_ts ON starts (ip_hash, ts);
CREATE TABLE IF NOT EXISTS spend (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,
    ts REAL NOT NULL,
    rub REAL NOT NULL,
    status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS spend_day ON spend (day);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    updated REAL NOT NULL,
    status TEXT NOT NULL,
    state TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_ts ON runs (ts);
CREATE INDEX IF NOT EXISTS runs_status ON runs (status);
CREATE TABLE IF NOT EXISTS interviews (
    id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    updated REAL NOT NULL,
    status TEXT NOT NULL,
    state TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS interviews_ts ON interviews (ts);
CREATE INDEX IF NOT EXISTS interviews_status ON interviews (status);
"""

RESTART_MESSAGE = "Сервис перезапустился во время прогона. Запустите прогон ещё раз."
HOLD_MESSAGE = "Лимит денег этого интервью исчерпан. Записанные примеры работают и сейчас."
INTERVIEW_RESTART_MESSAGE = "Сервис перезапустился во время интервью. Начните интервью заново."
# An interview is "active" (it takes one of MAX_CONCURRENT_INTERVIEWS) while it asks or builds.
ACTIVE = ("asking", "building")


@dataclass(frozen=True)
class Decision:
    allowed: bool
    code: str | None = None
    message: str | None = None
    reservation_id: int | None = None
    retry_after_s: int | None = None


def rub_text(value: float) -> str:
    """15 -> «15 ₽», 3.1 -> «3,10 ₽»."""
    return f"{value:.0f} ₽" if value == int(value) else f"{value:.2f} ₽".replace(".", ",")


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


class Store:
    def __init__(self, db_path: str | Path, daily_budget_rub: float, runs_per_hour: int,
                 retention_days: int = 30, clock: Callable[[], float] = time.time):
        self.db_path = Path(db_path)
        self.daily_budget_rub = daily_budget_rub
        self.runs_per_hour = runs_per_hour
        self.retention_s = retention_days * 86400
        self.clock = clock
        self._salt = secrets.token_bytes(16)
        self._purged_at = 0.0
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.executescript(_SCHEMA)
            conn.execute("DELETE FROM starts")  # hashes with an old salt only keep personal data around
        self.purge()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10, isolation_level=None, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _write(self, fn):
        """Run ``fn(conn)`` in one IMMEDIATE transaction and return its result."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = fn(conn)
            conn.execute("COMMIT")
            return result
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def day(self, ts: float | None = None) -> str:
        """Budget day in Moscow time: the page says "до 00:00 МСК"."""
        return datetime.fromtimestamp(self.clock() if ts is None else ts, tz=MSK).date().isoformat()

    def _hash(self, key: str) -> str:
        return hmac.new(self._salt, key.encode(), hashlib.sha256).hexdigest()

    # ---------- limits ----------

    def start(self, client_key: str, limit: int | None = None, what: str = "прогонов") -> Decision:
        """A new run: at most ``runs_per_hour`` (or ``limit``) per client. Interviews pass their own
        limit and a key of their own, so the two do not share the count."""
        now = self.clock()
        key = self._hash(client_key)
        limit = self.runs_per_hour if limit is None else limit

        def go(conn):
            conn.execute("DELETE FROM starts WHERE ts <= ?", (now - HOUR,))
            count, oldest = conn.execute("SELECT COUNT(*), MIN(ts) FROM starts WHERE ip_hash = ?",
                                         (key,)).fetchone()
            if count >= limit:
                retry = max(1, math.ceil(oldest + HOUR - now))
                return Decision(False, "rate_limited",
                                f"Не больше {limit} {what} в час с одного адреса. "
                                f"Попробуйте через {max(1, (retry + 59) // 60)} мин. Записанные примеры "
                                "работают и сейчас.", retry_after_s=retry)
            conn.execute("INSERT INTO starts (ip_hash, ts) VALUES (?, ?)", (key, now))
            return Decision(True)

        return self._write(go)

    def budget_message(self) -> str:
        return (f"Дневной бюджет демо ({rub_text(self.daily_budget_rub)}) на сегодня закончился, живой прогон — "
                "после 00:00 по Москве. Записанные примеры работают и сейчас.")

    def hold(self, worst_rub: float) -> Decision:
        """Hold a run's main pass: like ``reserve``, released with ``settle(id, 0)``."""
        return self.reserve(worst_rub, status="held")

    def reserve(self, worst_rub: float, hold: tuple[int, float] | None = None, status: str = "reserved",
                within_hold: bool = False) -> Decision:
        """Reserve ``worst_rub`` if today's spend leaves room. ``hold``: (id of a hold, rubles) —
        up to that many rubles move out of the hold into this reservation in the same
        transaction; a refusal leaves the hold as it was. ``within_hold``: refuse unless the hold
        still has all of ``worst_rub``."""
        now = self.clock()
        today = self.day(now)

        def go(conn):
            spent = conn.execute("SELECT COALESCE(SUM(rub), 0) FROM spend WHERE day = ?", (today,)).fetchone()[0]
            moved, freed = 0.0, 0.0
            if hold is not None:
                row = conn.execute("SELECT rub, day FROM spend WHERE id = ? AND status = 'held'",
                                   (hold[0],)).fetchone()
                if within_hold and (row is None or float(row[0]) + 1e-9 < worst_rub):
                    return Decision(False, "budget_exhausted", HOLD_MESSAGE)
                if row:
                    moved = min(float(row[0]), max(0.0, float(hold[1])))
                    freed = moved if row[1] == today else 0.0  # a hold from yesterday is not in today's spend
            if spent - freed + worst_rub > self.daily_budget_rub:
                return Decision(False, "budget_exhausted", self.budget_message())
            if moved:
                conn.execute("UPDATE spend SET rub = MAX(0, rub - ?) WHERE id = ?", (moved, hold[0]))
            cur = conn.execute("INSERT INTO spend (day, ts, rub, status) VALUES (?, ?, ?, ?)",
                               (today, now, float(worst_rub), status))
            return Decision(True, reservation_id=cur.lastrowid)

        return self._write(go)

    def settle(self, reservation_id: int, rub: float, hold_id: int | None = None) -> None:
        """The real cost of a reservation. ``hold_id``: a reservation made ``within_hold`` gives what it
        did not spend back to its hold, if the hold is still held."""
        if hold_id is None:
            with closing(self._connect()) as conn:
                conn.execute("UPDATE spend SET rub = ?, status = 'settled' WHERE id = ?", (float(rub), reservation_id))
            return

        def go(conn):
            row = conn.execute("SELECT rub FROM spend WHERE id = ? AND status = 'reserved'", (reservation_id,)).fetchone()
            back = max(0.0, float(row[0]) - float(rub)) if row else 0.0
            if back:
                conn.execute("UPDATE spend SET rub = rub + ? WHERE id = ? AND status = 'held'", (back, hold_id))
            conn.execute("UPDATE spend SET rub = ?, status = 'settled' WHERE id = ?", (float(rub), reservation_id))

        self._write(go)

    def hold_left(self, hold_id: int) -> float:
        """What a hold still has; 0 once it is released."""
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT rub FROM spend WHERE id = ? AND status = 'held'", (hold_id,)).fetchone()
        return float(row[0]) if row else 0.0

    def spent_today(self) -> float:
        with closing(self._connect()) as conn:
            return float(conn.execute("SELECT COALESCE(SUM(rub), 0) FROM spend WHERE day = ?",
                                      (self.day(),)).fetchone()[0])

    def left_today(self) -> float:
        return self.daily_budget_rub - self.spent_today()

    # ---------- runs ----------

    def create_run(self, run_id: str, state: dict) -> None:
        """``state`` must already hold the masked text only (app.main masks before it stores)."""
        now = self.clock()
        with closing(self._connect()) as conn:
            conn.execute("INSERT INTO runs (id, ts, updated, status, state) VALUES (?, ?, ?, ?, ?)",
                         (run_id, now, now, state.get("status", "running"), _dumps(state)))
        self.maybe_purge()

    def save_run(self, run_id: str, state: dict) -> None:
        with closing(self._connect()) as conn:
            conn.execute("UPDATE runs SET state = ?, status = ?, updated = ? WHERE id = ?",
                         (_dumps(state), state.get("status", "running"), self.clock(), run_id))

    def load_run(self, run_id: str) -> dict | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT state FROM runs WHERE id = ? AND ts > ?",
                               (run_id, self.clock() - self.retention_s)).fetchone()
        return json.loads(row[0]) if row else None

    def release_holds(self) -> int:
        """Free the holds of runs a previous process left: nothing was spent from what is left of
        them. Their calls in flight stay reserved at the worst case. Call once on startup."""
        with closing(self._connect()) as conn:
            return conn.execute("UPDATE spend SET rub = 0, status = 'settled' WHERE status = 'held'").rowcount

    def fail_stale_runs(self) -> int:
        """Runs left "running" by a previous process: failed with code "restart", so their pages
        stop waiting. Call once on startup, before any new run."""
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT id, state FROM runs WHERE status = 'running'").fetchall()
        for run_id, raw in rows:
            state = json.loads(raw)
            state.update(status="failed", error={"code": "restart", "message": RESTART_MESSAGE})
            self.save_run(run_id, state)
        return len(rows)

    # ---------- interviews ----------

    def create_interview(self, interview_id: str, state: dict) -> None:
        """``state`` holds masked text only (app.interview masks before it stores)."""
        now = self.clock()
        with closing(self._connect()) as conn:
            conn.execute("INSERT INTO interviews (id, ts, updated, status, state) VALUES (?, ?, ?, ?, ?)",
                         (interview_id, now, now, state["status"], _dumps(state)))
        self.maybe_purge()

    def save_interview(self, interview_id: str, state: dict) -> None:
        with closing(self._connect()) as conn:
            conn.execute("UPDATE interviews SET state = ?, status = ?, updated = ? WHERE id = ?",
                         (_dumps(state), state["status"], self.clock(), interview_id))

    def load_interview(self, interview_id: str) -> dict | None:
        """An interview younger than ``retention_days``: the report link works that long."""
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT state FROM interviews WHERE id = ? AND ts > ?",
                               (interview_id, self.clock() - self.retention_s)).fetchone()
        return json.loads(row[0]) if row else None

    def active_interviews(self) -> int:
        with closing(self._connect()) as conn:
            return conn.execute(f"SELECT COUNT(*) FROM interviews WHERE status IN {ACTIVE}").fetchone()[0]

    def idle_interviews(self, idle_s: float) -> list[str]:
        """Interviews that asked a question and got no answer for ``idle_s``."""
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT id FROM interviews WHERE status = 'asking' AND updated <= ?",
                                (self.clock() - idle_s,)).fetchall()
        return [r[0] for r in rows]

    def fail_stale_interviews(self) -> int:
        """Interviews a previous process left asking or building: "error" with code "restart" (their
        holds are freed by ``release_holds``). Call once on startup."""
        with closing(self._connect()) as conn:
            rows = conn.execute(f"SELECT id, state FROM interviews WHERE status IN {ACTIVE}").fetchall()
        for interview_id, raw in rows:
            state = json.loads(raw)
            state.update(status="error", question=None, hold_id=None,
                         error={"code": "restart", "message": INTERVIEW_RESTART_MESSAGE})
            self.save_interview(interview_id, state)
        return len(rows)

    def maybe_purge(self) -> None:
        if self.clock() - self._purged_at >= HOUR:
            self.purge()

    def purge(self) -> None:
        now = self.clock()
        self._purged_at = now
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM runs WHERE ts <= ?", (now - self.retention_s,))
            conn.execute("DELETE FROM interviews WHERE ts <= ?", (now - self.retention_s,))
            conn.execute("DELETE FROM starts WHERE ts <= ?", (now - HOUR,))
            conn.execute("DELETE FROM spend WHERE ts <= ?", (now - 3 * 86400,))


class CapStore(Store):
    """A store whose budget is one cap for its whole life instead of a day's (scripts and evals
    with --max-rub): a recording or an eval started before midnight does not get a fresh budget
    after it."""

    def day(self, ts: float | None = None) -> str:
        return "cap"
