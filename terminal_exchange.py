"""Bounded, cancellable PTY observations used by the single MCP I/O tool."""
from __future__ import annotations

import base64
import threading
import time

import regex

from terminal_sessions import ACTIVE_STATES, STATUS_LABELS, RECONNECTING_MESSAGE, TerminalError


def send_and_read(session, raw: bytes, *, actor: str, wait: dict | None = None,
                  read: dict | None = None, include_base64: bool = False,
                  cancel_event: threading.Event | None = None) -> dict:
    wait = wait or {"mode": "time", "wait_ms": 0, "max_wait_ms": 0}
    read = read or {"mode": "all"}
    mode = wait["mode"]
    pattern = None
    if mode == "match" and wait.get("regex", False):
        try:
            pattern = regex.compile(wait["pattern"])
        except regex.error:
            raise TerminalError("wait.pattern 不是有效的正则表达式。") from None
    cancel = cancel_event or threading.Event()
    baseline = session.output.bounds()["output_end"]
    sent = 0
    if raw and not cancel.is_set():
        receipt = session.send(raw, actor=actor, cancel_event=cancel)
        baseline = receipt["input_output_start"]
        sent = receipt["bytes_sent"]
    started = time.monotonic()
    deadline = started + wait["max_wait_ms"] / 1000
    matched_at = None
    scanned_end = baseline
    resumed_at = started
    previous_status = None
    reason = "max_wait"
    while True:
        # Receive holds session._lock before appending. Always keep this order.
        with session._lock:
            status = session.status
            with session.output.condition:
                base, end = session.output.base, session.output.end
                content = bytes(session.output.data)
                last_append = session.output.last_append_at
                closed = session.output.closed
        now = time.monotonic()
        if previous_status == "reconnecting" and status == "connected":
            resumed_at = now
        previous_status = status
        if cancel.is_set():
            reason = "cancelled"
            break
        if closed or status not in ACTIVE_STATES:
            reason = "closed"
            break
        if mode == "time" and now >= started + wait["wait_ms"] / 1000:
            reason = "time"
            break
        if now >= deadline:
            break
        next_check = deadline
        if mode == "time":
            next_check = min(next_check, started + wait["wait_ms"] / 1000)
        elif status == "connected":
            if mode == "idle" and end > baseline:
                quiet_at = max(last_append, resumed_at) + wait["idle_ms"] / 1000
                if now >= quiet_at:
                    reason = "idle"
                    break
                next_check = min(next_check, quiet_at)
            elif mode == "match":
                if matched_at is None and end != scanned_end:
                    observed = content[max(0, baseline - base):].decode("utf-8", errors="replace")
                    try:
                        found = (pattern.search(observed, timeout=max(0.0001, min(0.02, deadline - now)))
                                 if pattern is not None else wait["pattern"] in observed)
                    except TimeoutError:
                        reason = "pattern_timeout"
                        break
                    scanned_end = end
                    if found:
                        matched_at = time.monotonic()
                if matched_at is not None:
                    ready_at = matched_at + wait["after_ms"] / 1000
                    if time.monotonic() >= ready_at:
                        reason = "match"
                        break
                    next_check = min(next_check, ready_at)
        # Cancellation and state changes also wake within 25 ms, without taking
        # a condition lock while inspecting the session or holding a send lock.
        cancel.wait(max(0, min(0.025, next_check - time.monotonic())))

    # One final snapshot includes bytes that arrived while matching/cancelling.
    with session._lock:
        status = session.status
        with session.output.condition:
            base, end = session.output.base, session.output.end
            content = bytes(session.output.data)
            closed = session.output.closed
    selection = read["mode"]
    offset = 0
    if selection == "new":
        offset = max(0, baseline - base)
    elif selection == "tail_bytes":
        offset = max(0, len(content) - read["count"])
    elif selection == "tail_lines":
        # LF is a line boundary. A trailing LF terminates its line; it does not
        # create a phantom empty line. Long lines are not silently byte-clipped.
        position = len(content) - (1 if content.endswith(b"\n") else 0)
        for _ in range(read["count"]):
            position = content.rfind(b"\n", 0, position)
            if position < 0:
                break
        offset = position + 1
    data = content[offset:]
    result = dict(connection_id=session.connection_id, status=status,
                  status_message=RECONNECTING_MESSAGE if status == "reconnecting" else STATUS_LABELS.get(status, status),
                  wait_reason=reason, bytes_sent=sent, appended_enter=False,
                  input_output_start=baseline, cursor=base + offset, next_cursor=end,
                  buffer_start=base, output_end=end, bytes_returned=len(data),
                  dropped_bytes=max(0, base - baseline), closed=closed,
                  text=data.decode("utf-8", errors="replace"), encoding="utf-8",
                  stream="raw-pty (stdout/stderr merged; ANSI preserved)")
    if include_base64:
        result["data_base64"] = base64.b64encode(data).decode("ascii")
    return result
