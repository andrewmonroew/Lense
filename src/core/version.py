"""The application's version, in one place.

Deliberately separate from the "version" field inside a .lense file: that one describes
the *project file format* and is what a loader checks for compatibility. Bumping the app
must not touch it -- they move for entirely different reasons, and tying them together
would mean every release claimed to be a new file format.
"""

APP_NAME = "Lense"
APP_VERSION = "1.0 alpha"

# Windows' version resource only accepts four integers, with no room for a word like
# "alpha" -- that lives in the string fields instead. See version_info.txt.
VERSION_TUPLE = (1, 0, 0, 0)


def title(suffix=""):
    """'Lense 1.0 alpha' for a window title, optionally with a document after it."""
    base = f"{APP_NAME} {APP_VERSION}"
    return f"{base} — {suffix}" if suffix else base
