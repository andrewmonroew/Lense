import sys
import os
import faulthandler
import traceback
import datetime

from src.core.utils import get_logs_dir

CRASH_LOG_FILENAME = "crash.log"

# faulthandler writes into this at crash time, so the handle has to stay open for the
# life of the process -- letting it be garbage collected disarms the handler.
_fault_log = None


def install_crash_logger():
    """Installs a sys.excepthook that appends any uncaught exception's traceback to
    logs/crash.log before the app exits or aborts. PySide6 raises unhandled Python
    exceptions from inside overridden Qt virtual methods (paint(), event handlers, etc.)
    through sys.excepthook and then typically terminates the process, since execution
    can't safely resume across the C++/Python boundary -- without this hook that
    traceback is lost the moment the process dies, leaving no diagnostic trail at all.

    Returns the resolved log file path.
    """
    log_path = os.path.join(get_logs_dir(), CRASH_LOG_FILENAME)

    # A hard crash inside Qt -- a segfault from touching an object C++ has already
    # deleted, which is the classic PySide failure -- never reaches sys.excepthook: the
    # process is simply killed, leaving nothing behind to explain it. faulthandler dumps
    # the Python stack from the signal handler itself, which names the call that went
    # into Qt and is usually enough on its own to identify the offending object.
    global _fault_log
    try:
        _fault_log = open(log_path, "a", buffering=1)
        _fault_log.write(f"\n=== session started {datetime.datetime.now().isoformat(timespec='seconds')} ===\n")
        faulthandler.enable(file=_fault_log, all_threads=True)
    except OSError:
        _fault_log = None  # logging must never be the thing that stops the app starting

    previous_hook = sys.excepthook

    def handle_exception(exc_type, exc_value, exc_tb):
        timestamp = datetime.datetime.now().isoformat(timespec="seconds")
        try:
            with open(log_path, "a") as f:
                f.write(f"\n=== Unhandled exception at {timestamp} ===\n")
                traceback.print_exception(exc_type, exc_value, exc_tb, file=f)
        except OSError:
            pass  # Logging must never itself raise and mask the original crash
        previous_hook(exc_type, exc_value, exc_tb)

    sys.excepthook = handle_exception
    return log_path
