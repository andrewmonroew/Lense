"""Opt-in performance probe.

Some slowness only shows up under a real hand on a real mouse: synthetic drags get
coalesced by Qt, offscreen rendering skips most of the paint path, and neither
reproduces a compositor's frame pacing. Rather than keep inferring from benchmarks that
don't reproduce the problem, this measures the real thing on the machine where it
actually happens.

Off unless LENSE_PERF=1 is set, and it costs nothing at all when off -- the wrappers
are only installed when it's enabled, so the normal path keeps its original functions.

    LENSE_PERF=1 python main.py

Writes a line per second to logs/perf.log for whatever was busy in that second.
"""

import os
import time

from src.core.utils import get_logs_dir

enabled = os.environ.get("LENSE_PERF") == "1"

LOG_FILENAME = "perf.log"
FLUSH_SECONDS = 1.0

_stats = {}          # name -> [count, total_seconds, worst_seconds]
_last_flush = time.monotonic()
_log_path = None


def _path():
    global _log_path
    if _log_path is None:
        _log_path = os.path.join(get_logs_dir(), LOG_FILENAME)
    return _log_path


def record(name, elapsed):
    entry = _stats.get(name)
    if entry is None:
        _stats[name] = [1, elapsed, elapsed]
    else:
        entry[0] += 1
        entry[1] += elapsed
        if elapsed > entry[2]:
            entry[2] = elapsed


def flush(force=False):
    """Writes a summary line if anything happened since the last one."""
    global _last_flush
    now = time.monotonic()
    if not force and now - _last_flush < FLUSH_SECONDS:
        return
    window = now - _last_flush
    _last_flush = now
    if not _stats:
        return
    parts = []
    for name in sorted(_stats):
        count, total, worst = _stats[name]
        parts.append(f"{name} n={count} avg={total / count * 1000:.1f}ms "
                     f"max={worst * 1000:.1f}ms total={total / window * 100:.0f}%")
    _stats.clear()
    try:
        with open(_path(), "a") as handle:
            handle.write(time.strftime("%H:%M:%S ") + "  ".join(parts) + "\n")
    except OSError:
        pass  # diagnostics must never be the thing that breaks the app


def wrap(name, function):
    """Returns `function` with timing around it. Only used when enabled."""
    def timed(*args, **kwargs):
        started = time.perf_counter()
        try:
            return function(*args, **kwargs)
        finally:
            record(name, time.perf_counter() - started)
            flush()
    return timed


def install(canvas_view_cls, scene_cls):
    """Instruments the handlers a drag actually goes through."""
    if not enabled:
        return
    canvas_view_cls.mouseMoveEvent = wrap("mouse_move", canvas_view_cls.mouseMoveEvent)
    canvas_view_cls.mousePressEvent = wrap("mouse_press", canvas_view_cls.mousePressEvent)
    canvas_view_cls.paintEvent = wrap("paint", canvas_view_cls.paintEvent)
    scene_cls.drawBackground = wrap("draw_background", scene_cls.drawBackground)
    try:
        with open(_path(), "a") as handle:
            handle.write("\n=== perf probe started " + time.strftime("%Y-%m-%d %H:%M:%S") + " ===\n")
    except OSError:
        pass
