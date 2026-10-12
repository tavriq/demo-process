import sqlite3
from datetime import datetime

import pytest

from app.pipeline import initial_state
from app.store import MSK, CapStore, Store
from tests.fakes import FakeClock


@pytest.fixture
def clock():
    return FakeClock(datetime(2026, 10, 9, 12, 0, tzinfo=MSK).timestamp())


@pytest.fixture
def store(tmp_path, clock):
    return Store(tmp_path / "s.sqlite3", 15, 3, retention_days=30, clock=clock)


def test_reserve_and_settle(store):
    a = store.reserve(5)
    assert a.allowed and store.spent_today() == 5
    store.settle(a.reservation_id, 0.4)
    assert store.spent_today() == pytest.approx(0.4) and store.left_today() == pytest.approx(14.6)
    assert store.reserve(14.5).allowed
    refused = store.reserve(0.2)
    assert not refused.allowed and refused.code == "budget_exhausted" and "15 ₽" in refused.message


def test_reserve_moves_money_out_of_a_hold(store):
    hold = store.hold(10)
    assert not store.reserve(6).allowed  # only 5 are free: the hold is spent for everyone else
    call = store.reserve(6, hold=(hold.reservation_id, 4))
    assert call.allowed and store.spent_today() == pytest.approx(16 - 4)
    assert not store.reserve(10, hold=(hold.reservation_id, 6)).allowed  # refused: the hold stays as it was
    assert store.reserve(3, hold=(hold.reservation_id, 100)).allowed  # at most what the hold has left
    store.settle(hold.reservation_id, 0)
    assert store.spent_today() == pytest.approx(9)


def test_budget_day_is_moscow(store, clock):
    store.reserve(15)
    assert not store.reserve(1).allowed
    clock.t = datetime(2026, 10, 9, 23, 59, tzinfo=MSK).timestamp()
    assert not store.reserve(1).allowed
    clock.t = datetime(2026, 10, 10, 0, 1, tzinfo=MSK).timestamp()
    assert store.reserve(1).allowed


def test_cap_store_does_not_reset_at_midnight(tmp_path, clock):
    capped = CapStore(tmp_path / "c.sqlite3", 15, 3, clock=clock)
    capped.reserve(14)
    clock.t = datetime(2026, 10, 10, 0, 1, tzinfo=MSK).timestamp()
    assert not capped.reserve(2).allowed and capped.left_today() == pytest.approx(1)


def test_hourly_starts_per_client(store, clock):
    assert all(store.start("192.0.2.1").allowed for _ in range(3))
    refused = store.start("192.0.2.1")
    assert not refused.allowed and refused.code == "rate_limited" and refused.retry_after_s == 3600
    assert store.start("192.0.2.2").allowed
    clock.advance(3601)
    assert store.start("192.0.2.1").allowed


def test_no_client_key_in_the_database(store):
    store.start("198.51.100.7")
    dump = "\n".join(str(r) for r in sqlite3.connect(store.db_path).execute("SELECT * FROM starts"))
    assert "198.51.100.7" not in dump


def test_holds_released_on_restart(tmp_path, clock):
    path = tmp_path / "s.sqlite3"
    first = Store(path, 15, 3, clock=clock)
    first.hold(10)
    second = Store(path, 15, 3, clock=clock)
    assert second.release_holds() == 1 and second.spent_today() == 0


def test_stale_runs_fail_on_restart(tmp_path, clock):
    path = tmp_path / "s.sqlite3"
    first = Store(path, 15, 3, clock=clock)
    first.create_run("r1", initial_state("r1", "текст", clock()))
    second = Store(path, 15, 3, clock=clock)
    assert second.fail_stale_runs() == 1
    state = second.load_run("r1")
    assert state["status"] == "failed" and state["error"]["code"] == "restart"
    assert second.fail_stale_runs() == 0


def test_retention(store, clock):
    store.create_run("old", initial_state("old", "текст", clock()))
    clock.advance(30 * 86400 + 1)
    assert store.load_run("old") is None  # hidden at once
    store.create_run("new", initial_state("new", "текст", clock()))  # purges: an hour has passed
    db = sqlite3.connect(store.db_path)
    assert [r[0] for r in db.execute("SELECT id FROM runs")] == ["new"]


def interview(status="asking", **extra):
    return {"id": "i1", "status": status, "question": {"n": 1}, "hold_id": None, "error": None, **extra}


def test_interviews_kept_thirty_days(store, clock):
    store.create_interview("i1", interview())
    assert store.load_interview("i1")["status"] == "asking" and store.active_interviews() == 1
    clock.advance(30 * 86400 + 1)
    assert store.load_interview("i1") is None
    store.purge()
    assert sqlite3.connect(store.db_path).execute("SELECT COUNT(*) FROM interviews").fetchone()[0] == 0


def test_idle_and_active_interviews(store, clock):
    store.create_interview("i1", interview())
    store.create_interview("i2", interview("building"))
    store.create_interview("i3", interview("done"))
    assert store.active_interviews() == 2 and store.idle_interviews(600) == []
    clock.advance(600)
    assert store.idle_interviews(600) == ["i1"]  # building is never idle: the final has its own time limit
    store.save_interview("i1", interview())
    assert store.idle_interviews(600) == []


def test_stale_interviews_on_restart(tmp_path, clock):
    path = tmp_path / "s.sqlite3"
    first = Store(path, 15, 3, clock=clock)
    first.create_interview("i1", interview())
    first.create_interview("i2", interview("done"))
    second = Store(path, 15, 3, clock=clock)
    assert second.fail_stale_interviews() == 1
    state = second.load_interview("i1")
    assert state["status"] == "error" and state["error"]["code"] == "restart" and state["question"] is None
    assert second.load_interview("i2")["status"] == "done" and second.active_interviews() == 0


def test_interviews_have_their_own_hourly_count(store):
    assert all(store.start("interview|192.0.2.1", 3, "интервью").allowed for _ in range(3))
    refused = store.start("interview|192.0.2.1", 3, "интервью")
    assert not refused.allowed and "Не больше 3 интервью в час" in refused.message
    assert store.start("192.0.2.1").allowed  # the runs of the same address are counted apart


def test_within_hold_and_the_rest_goes_back(store):
    hold = store.hold(3)
    call = store.reserve(1, hold=(hold.reservation_id, 1), within_hold=True)
    assert call.allowed and store.hold_left(hold.reservation_id) == pytest.approx(2)
    store.settle(call.reservation_id, 0.25, hold.reservation_id)  # 0.75 of the reserve goes back
    assert store.hold_left(hold.reservation_id) == pytest.approx(2.75)
    assert store.spent_today() == pytest.approx(3)  # the hold and the real cost: never more than the hold
    refused = store.reserve(2.8, hold=(hold.reservation_id, 2.8), within_hold=True)
    assert not refused.allowed and "интервью" in refused.message  # the day has room, the hold has not
    store.settle(hold.reservation_id, 0)
    assert not store.reserve(0.1, hold=(hold.reservation_id, 0.1), within_hold=True).allowed  # released
    assert store.hold_left(hold.reservation_id) == 0

