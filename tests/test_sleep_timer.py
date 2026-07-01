"""Tests for the reader sleep timer (pure logic)."""
from ficary.reader.sleep_timer import MAX_MINUTES, MIN_MINUTES, SleepTimer


def test_start_sets_active_and_remaining():
    t = SleepTimer(lambda: None)
    assert t.start(30) == 30
    assert t.active
    assert 1795 <= t.remaining_seconds() <= 1800
    t.cancel()


def test_clamps_to_bounds():
    t = SleepTimer(lambda: None)
    assert t.start(1) == MIN_MINUTES
    assert t.start(9999) == MAX_MINUTES
    t.cancel()


def test_cancel_clears():
    t = SleepTimer(lambda: None)
    t.start(30)
    t.cancel()
    assert not t.active
    assert t.remaining_seconds() == 0


def test_fire_calls_on_expire_and_clears():
    fired = []
    t = SleepTimer(lambda: fired.append(True))
    t.start(30)
    t._fire()
    assert fired == [True]
    assert not t.active
