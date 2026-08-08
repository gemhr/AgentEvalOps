"""Lightweight HTTP health server for Celery worker / beat containers.

Runs alongside the Celery process (started in the background via ``&``)
and exposes a ``GET /health`` endpoint that Cloud Run liveness and
startup probes can hit.

The check has two parts:

1. **Broker reachability** — a raw Redis PING.  A worker cannot be healthy
   without it.
2. **Pool liveness** (worker only) — the pool stamps a local heartbeat file on
   every task (see ``celery_app._touch_heartbeat``).  If that heartbeat goes
   stale *while work is queued*, the pool has stopped consuming and Cloud Run
   should restart this container.

Part 2 exists because an earlier version checked only Redis: it reported healthy
for six hours while the pool was wedged and no task was being consumed, because
Redis itself was perfectly fine.  A Celery control ping cannot substitute for
it — every Cloud Run instance has hostname ``localhost``, so all instances share
the node name ``celery@localhost`` and a healthy sibling answers a wedged
instance's ping.

The "while work is queued" condition is what makes this safe: an idle worker
with an empty queue has no reason to stamp a heartbeat and must not be
restarted for it.  We only declare failure when there is work to do and the
pool demonstrably is not doing it.

Beat has no pool, so it keeps the broker-only check (``ROLE=beat``).

Usage (CI deploy command)::

    python /app/scripts/worker_health.py & exec celery -A ... worker ...
"""

import http.server
import os
import socket
import tempfile
import time

_REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
_REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
_REDIS_DB = os.environ.get("REDIS_DB", "0")
_PING_TIMEOUT = 5
_ROLE = os.environ.get("ROLE", "worker")

#: Grace period for the pool heartbeat.  Tasks stamp it on prerun and postrun,
#: so the worst legitimate gap is one long task running start to finish: eval
#: tasks are allowed 3600s by their own hard ``time_limit`` (session eval runs
#: have been observed at 1201s in production), and every other task is capped at
#: 360s by the global limit.  This must therefore sit above 3600s or the probe
#: would kill a container that is merely busy with a slow eval.  3900s keeps that
#: margin and still trips ~6x sooner than the six hours the old broker-only check
#: slept through.
_HEARTBEAT_MAX_AGE_S = 3900

#: Must match ``celery_app.HEARTBEAT_PATH``.
_HEARTBEAT_PATH = os.environ.get(
    "WORKER_HEARTBEAT_PATH",
    os.path.join(tempfile.gettempdir(), "pp-worker-heartbeat"),
)


def _resp(*args: str) -> bytes:
    """Encode one command in the Redis serialisation protocol."""
    out = f"*{len(args)}\r\n".encode()
    for arg in args:
        out += f"${len(arg)}\r\n{arg}\r\n".encode()
    return out


def _redis_roundtrip(payload: bytes) -> bytes:
    """Send raw RESP bytes on a fresh connection and return the reply."""
    with socket.create_connection((_REDIS_HOST, _REDIS_PORT), timeout=_PING_TIMEOUT) as sock:
        sock.sendall(payload)
        return sock.recv(256)


def _redis_is_reachable() -> bool:
    """Send a raw Redis PING and check for PONG — no third-party deps."""
    try:
        return b"PONG" in _redis_roundtrip(_resp("PING"))
    except (OSError, ConnectionError):
        return False


def _queue_has_work() -> bool:
    """True when tasks are waiting on the default queue.

    The Redis broker stores the default queue as a list named after the queue,
    so ``LLEN celery`` is the backlog.  ``SELECT`` is per-connection, so both
    commands are pipelined on one socket.  On any error we assume *no* backlog,
    so a broker hiccup can never by itself get a healthy container killed.
    """
    try:
        reply = _redis_roundtrip(_resp("SELECT", _REDIS_DB) + _resp("LLEN", "celery"))
        # Replies arrive in order: "+OK\r\n" for SELECT, then ":<n>\r\n" for LLEN.
        for line in reply.split(b"\r\n"):
            if line.startswith(b":"):
                return int(line[1:]) > 0
        return False
    except (OSError, ConnectionError, ValueError):
        return False


def _heartbeat_age_s() -> float | None:
    """Seconds since the pool last stamped its heartbeat, or None if absent."""
    try:
        return time.time() - os.path.getmtime(_HEARTBEAT_PATH)
    except OSError:
        return None


def _is_healthy() -> tuple[bool, bytes]:
    if not _redis_is_reachable():
        return False, b"broker unreachable"

    if _ROLE == "beat":
        return True, b"ok"

    age = _heartbeat_age_s()
    if age is None:
        # Worker has not finished booting yet; the startup probe's budget
        # governs how long that may take.
        return True, b"ok (pool starting)"

    if age > _HEARTBEAT_MAX_AGE_S and _queue_has_work():
        return False, b"celery pool not consuming"

    return True, b"ok"


class _HealthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/health":
            healthy, body = _is_healthy()
            self.send_response(200 if healthy else 503)
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    server = http.server.HTTPServer(("0.0.0.0", port), _HealthHandler)
    server.serve_forever()
