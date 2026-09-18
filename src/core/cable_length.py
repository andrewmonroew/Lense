"""Real cable length: what gets pulled, not what the plan measures.

A floor plan measures the horizontal run and nothing else, but a wall drop costs cable
going *up* from the patch panel to the pathway, across, then back *down* to the outlet,
plus a coil of slack at each end. Multiplying the whole job by a flat percentage gets
the total roughly right and every individual run wrong -- a 15 ft drop and a 200 ft
trunk do not waste proportionally.

So the allowances live on the ENDPOINTS. A drop's 10 ft of rise and 5 ft of loop are
facts about that drop, not about any run that happens to reach it; a run's length is the
distance measured on the plan plus the rise and loop of whatever sits at each end. Two
runs landing on a 2-port drop each pay the allowance once, which is exactly right, with
no special-casing anywhere.

Each object stores None until someone edits it, meaning "follow the default for my
kind". Change the wall-drop default and every untouched drop in the project follows;
anything tuned by hand keeps its own number.
"""

from PySide6.QtCore import QSettings

# QSettings keys, one pair per category. Kept here rather than in the settings dialog so
# the model that consumes them owns them and a typo can't silently read back a default.
KEY_PREFIX = "cabling/"

# category -> (label for the UI, default rise in feet, default service loop in feet)
CATEGORY_DEFAULTS = {
    "rack":         ("Rack / MDF",        10.0, 10.0),
    "patch-panel":  ("Patch panel",       10.0, 10.0),
    "drop":         ("Wall drop",         10.0,  5.0),
    "camera":       ("Camera",             2.0,  5.0),
    "access-point": ("Access point",       1.0,  5.0),
    "switch":       ("Switch",             8.0, 10.0),
    "nvr":          ("NVR",                8.0, 10.0),
    "misc":         ("Other equipment",    2.0,  5.0),
}

FALLBACK = ("Other equipment", 2.0, 5.0)


def category_of(item):
    """The defaults bucket an item belongs to."""
    if item is None:
        return None
    object_type = getattr(item, "object_type", None)
    if object_type == "rack":
        return "rack"
    if object_type == "camera":
        return "camera"
    if object_type == "custom":
        return "misc"
    if object_type != "device":
        return None
    category = getattr(item, "category", None) or (getattr(item, "spec", None) or {}).get("category")
    return category if category in CATEGORY_DEFAULTS else "misc"


# Resolved defaults, so the paint path doesn't open QSettings for every cable end on
# every frame -- that was thousands of lookups per repaint. Only set_defaults() and
# clear_defaults() change the stored values, and both empty this.
_defaults_memo = {}


def _default(category, index):
    key = (category, index)
    if key in _defaults_memo:
        return _defaults_memo[key]
    entry = CATEGORY_DEFAULTS.get(category, FALLBACK)
    value = entry[index]
    stored = QSettings().value(f"{KEY_PREFIX}{category}/{'rise' if index == 1 else 'loop'}")
    if stored is not None:
        try:
            value = max(0.0, float(stored))
        except (TypeError, ValueError):
            pass
    _defaults_memo[key] = value
    return value


def default_rise(category):
    return _default(category, 1)


def default_loop(category):
    return _default(category, 2)


def set_defaults(category, rise, loop):
    settings = QSettings()
    settings.setValue(f"{KEY_PREFIX}{category}/rise", max(0.0, float(rise)))
    settings.setValue(f"{KEY_PREFIX}{category}/loop", max(0.0, float(loop)))
    _defaults_memo.clear()


def clear_defaults():
    """Drops every stored override, so the built-in numbers apply again."""
    settings = QSettings()
    for category in CATEGORY_DEFAULTS:
        settings.remove(f"{KEY_PREFIX}{category}/rise")
        settings.remove(f"{KEY_PREFIX}{category}/loop")
    _defaults_memo.clear()


def _effective(item, attribute, fallback):
    """The item's own value, or the default for its kind when it has none."""
    category = category_of(item)
    if category is None:
        return 0.0
    value = getattr(item, attribute, None)
    if value is None:
        return fallback(category)
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return fallback(category)


def rise_feet(item):
    """Vertical cable this endpoint consumes reaching the pathway, in feet."""
    return _effective(item, "vertical_rise", default_rise)


def loop_feet(item):
    """Service loop coiled at this endpoint, in feet."""
    return _effective(item, "service_loop", default_loop)


def is_inherited(item, attribute):
    return getattr(item, attribute, None) is None and category_of(item) is not None


def endpoint_items(cable, canvas_view):
    """The objects at a cable's two ends; either may be None for a free end."""
    anchors = getattr(cable, "vertex_anchors", None) or []
    if not anchors:
        return (None, None)
    lookup = canvas_view.find_device_or_camera_by_id
    return (lookup(anchors[0]) if anchors[0] else None,
            lookup(anchors[-1]) if anchors[-1] else None)


def breakdown(cable, canvas_view=None, endpoints=None):
    """(horizontal, vertical, loop, total) in feet, or None if uncalibrated.

    `endpoints` lets a caller supply the two objects it already resolved. Finding them is
    the expensive half while the allowances themselves are attribute reads, so a caller
    on the paint path can cache the lookup without the numbers going stale on it.

    Patch cables are excluded from the allowance: they are short in-rack jumpers cut to
    fit, not field runs pulled through a pathway, and are counted by the piece anyway.
    """
    horizontal = cable.horizontal_feet()
    if horizontal is None:
        return None
    if endpoints is None:
        if canvas_view is None:
            return (horizontal, 0.0, 0.0, horizontal)
        endpoints = endpoint_items(cable, canvas_view)
    if getattr(cable, "cable_type", None) == "Patch":
        return (horizontal, 0.0, 0.0, horizontal)

    vertical = 0.0
    loop = 0.0
    for item in endpoints:
        if item is None:
            continue  # a free end runs to nothing, so it costs nothing
        vertical += rise_feet(item)
        loop += loop_feet(item)
    return (horizontal, vertical, loop, horizontal + vertical + loop)
