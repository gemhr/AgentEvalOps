"""Unit tests for the Celery worker liveness probe (no infra required).

``scripts/worker_health.py`` is intentionally dependency-free — it imports only
the standard library so a config or import error can never make a *healthy*
container fail its own probe.  That means its thresholds cannot import the task
time limits they are derived from, so the coupling is asserted here instead.
"""

import importlib.util
import time
from pathlib import Path
from types import ModuleType

import pytest

from app.infrastructure.queue.tasks import _EVAL_TIME_LIMIT

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "worker_health.py"


def _load_health_module() -> ModuleType:
    """Import the probe script by path (it lives outside the app package)."""
    spec = importlib.util.spec_from_file_location("worker_health", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def health() -> ModuleType:
    return _load_health_module()


def test_heartbeat_grace_exceeds_longest_task_time_limit(health: ModuleType) -> None:
    """The probe must outlast the longest task a healthy worker may run.

    A worker stamps its heartbeat on task prerun and postrun, so a single
    long-running task leaves a gap as wide as its own runtime.  Eval tasks are
    allowed ``_EVAL_TIME_LIMIT`` seconds, so a grace period below that would
    restart containers that are merely busy.  If the eval limit is ever raised,
    this test fails instead of production quietly entering a restart loop.
    """
    assert health._HEARTBEAT_MAX_AGE_S > _EVAL_TIME_LIMIT


def test_beat_is_exempt_from_the_pool_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Beat runs no pool, so a missing heartbeat must not fail its probe."""
    monkeypatch.setenv("ROLE", "beat")
    health = _load_health_module()
    monkeypatch.setattr(health, "_redis_is_reachable", lambda: True)
    monkeypatch.setattr(health, "_heartbeat_age_s", lambda: None)
    # Would be "not consuming" for a worker; beat must still be healthy.
    monkeypatch.setattr(health, "_queue_has_work", lambda: True)

    healthy, _ = health._is_healthy()
    assert healthy is True


def test_unreachable_broker_is_unhealthy(health: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health, "_redis_is_reachable", lambda: False)

    healthy, body = health._is_healthy()
    assert healthy is False
    assert body == b"broker unreachable"


def test_missing_heartbeat_is_healthy_while_the_pool_boots(
    health: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No heartbeat yet means "still starting" — the startup probe governs that."""
    monkeypatch.setattr(health, "_redis_is_reachable", lambda: True)
    monkeypatch.setattr(health, "_heartbeat_age_s", lambda: None)
    monkeypatch.setattr(health, "_queue_has_work", lambda: True)

    healthy, _ = health._is_healthy()
    assert healthy is True


def test_stale_heartbeat_with_empty_queue_is_healthy(health: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """An idle worker has no reason to stamp a heartbeat and must not be killed."""
    monkeypatch.setattr(health, "_redis_is_reachable", lambda: True)
    monkeypatch.setattr(health, "_heartbeat_age_s", lambda: health._HEARTBEAT_MAX_AGE_S + 1_000)
    monkeypatch.setattr(health, "_queue_has_work", lambda: False)

    healthy, _ = health._is_healthy()
    assert healthy is True


def test_stale_heartbeat_with_queued_work_is_unhealthy(health: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """The incident shape: work is waiting and the pool is not consuming it."""
    monkeypatch.setattr(health, "_redis_is_reachable", lambda: True)
    monkeypatch.setattr(health, "_heartbeat_age_s", lambda: health._HEARTBEAT_MAX_AGE_S + 1)
    monkeypatch.setattr(health, "_queue_has_work", lambda: True)

    healthy, body = health._is_healthy()
    assert healthy is False
    assert body == b"celery pool not consuming"


# -- _queue_has_work: prefetch accounting ------------------------------------
#
# A prefetched message leaves the queue list and lands in kombu's ``unacked``
# hash, so a wedged pool can hold work while ``LLEN celery`` reads zero.  These
# tests drive the real RESP parser with recorded replies.


def _redis_reply(*counts: int) -> bytes:
    """Build the pipelined reply for SELECT + N integer commands."""
    return b"+OK\r\n" + b"".join(f":{n}\r\n".encode() for n in counts)


def test_queue_has_work_counts_waiting_messages(health: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health, "_redis_roundtrip", lambda _payload: _redis_reply(3, 0))
    assert health._queue_has_work() is True


def test_queue_has_work_counts_prefetched_unacked_messages(
    health: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression: queue drained to zero but a worker still holds messages.

    Reproduced against a live worker — with the default prefetch multiplier a
    wedged pool checks out its messages, leaving ``LLEN celery`` at 0 while
    ``HLEN unacked`` stays positive.  Counting only the list reported healthy.

    Asserts on the *request* as well as the reply: a probe that never asks for
    ``unacked`` cannot see prefetched work, so dropping that command must fail
    here rather than silently reintroduce the blind spot.
    """
    sent: list[bytes] = []

    def _record(payload: bytes) -> bytes:
        sent.append(payload)
        return _redis_reply(0, 8)

    monkeypatch.setattr(health, "_redis_roundtrip", _record)

    assert health._queue_has_work() is True
    assert b"unacked" in sent[0], "probe must query the unacked hash, not just LLEN"
    assert b"celery" in sent[0], "probe must still query the queue list"


def test_queue_has_work_is_false_when_broker_is_idle(health: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing waiting and nothing checked out — an idle worker must not restart."""
    monkeypatch.setattr(health, "_redis_roundtrip", lambda _payload: _redis_reply(0, 0))
    assert health._queue_has_work() is False


def test_queue_has_work_is_false_when_the_broker_errors(health: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """A broker hiccup must never be the sole reason a container is killed."""

    def _boom(_payload: bytes) -> bytes:
        raise OSError("connection reset")

    monkeypatch.setattr(health, "_redis_roundtrip", _boom)
    assert health._queue_has_work() is False


def test_wedged_pool_holding_prefetched_work_is_unhealthy(health: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end: stale heartbeat + drained queue + unacked messages -> restart."""
    monkeypatch.setattr(health, "_redis_is_reachable", lambda: True)
    monkeypatch.setattr(health, "_heartbeat_age_s", lambda: health._HEARTBEAT_MAX_AGE_S + 1)
    monkeypatch.setattr(health, "_redis_roundtrip", lambda _payload: _redis_reply(0, 8))

    healthy, body = health._is_healthy()
    assert healthy is False
    assert body == b"celery pool not consuming"


def test_busy_worker_with_fresh_heartbeat_is_healthy(health: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health, "_redis_is_reachable", lambda: True)
    monkeypatch.setattr(health, "_heartbeat_age_s", lambda: 1.0)
    monkeypatch.setattr(health, "_queue_has_work", lambda: True)

    healthy, _ = health._is_healthy()
    assert healthy is True


def test_heartbeat_touch_is_observable_by_the_probe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The writer and the reader must agree on the same path.

    ``celery_app`` stamps the file and ``worker_health`` reads its mtime; they
    share only the ``WORKER_HEARTBEAT_PATH`` contract, so assert it end to end.
    """
    hb = tmp_path / "heartbeat"
    monkeypatch.setenv("WORKER_HEARTBEAT_PATH", str(hb))

    import app.infrastructure.queue.celery_app as celery_app

    monkeypatch.setattr(celery_app, "HEARTBEAT_PATH", hb)
    celery_app._touch_heartbeat()

    health = _load_health_module()
    age = health._heartbeat_age_s()
    assert age is not None
    assert age < 5


def test_touch_heartbeat_never_raises_on_an_unwritable_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Heartbeat writes are best-effort — they must never break a task."""
    import app.infrastructure.queue.celery_app as celery_app

    monkeypatch.setattr(celery_app, "HEARTBEAT_PATH", Path("/nonexistent-dir/heartbeat"))
    celery_app._touch_heartbeat()  # must not raise
