"""Turning wheel events into zoom, the same way for a mouse and a touchpad.

The canvas used to apply one fixed zoom step per wheel EVENT. A mouse wheel sends one
event per notch, so that felt right. A touchpad sends a stream of tiny events for a
single swipe -- dozens of them -- and each was still handed a full step, which is why
the touchpad zoomed far too fast while the mouse felt fine.

Zoom now follows the size of each event instead: a full notch (120 in Qt's units) is one
step, and a touchpad's small increments are proportionally small fractions of one. That
alone fixes most of it. On top of that each kind of device has its own sensitivity, so
the touchpad can be slowed down without touching a wheel that already feels right.

Kept free of Qt event objects so it can be tested with plain numbers.
"""

# Qt reports wheel motion in eighths of a degree; a standard mouse detent is 15 degrees.
MOUSE_NOTCH = 120.0

# Some devices report only a pixel delta and no angle delta. This is how many pixels are
# treated as one notch's worth of zoom in that case.
PIXELS_PER_NOTCH = 60.0

# A single event is never allowed to zoom more than this many notches' worth. Guards
# against a driver hiccup or a violent flick jumping the view clean off the drawing.
MAX_NOTCHES_PER_EVENT = 4.0

DEFAULT_SENSITIVITY = 1.0
MIN_SENSITIVITY = 0.1
MAX_SENSITIVITY = 3.0


def is_touchpad(angle_y, pixel_y, in_gesture, reported_touchpad):
    """Best guess at whether a wheel event came from a touchpad.

    The device type the OS reports can't be relied on by itself -- many touchpads,
    especially on Windows, identify as an ordinary mouse. So the event's own shape is
    read as well: touchpads tend to carry a pixel delta, report a scroll phase
    (begin / update / end of a gesture), or move in increments that aren't whole
    notches. Any one of those is taken as a touchpad.

    Known limit: a high-resolution "free-spin" mouse wheel also sends sub-notch
    increments, so it will be treated as a touchpad and use that sensitivity.
    """
    if reported_touchpad:
        return True
    if pixel_y:
        return True
    if in_gesture:
        return True
    return bool(angle_y) and abs(angle_y) % MOUSE_NOTCH != 0


def notches(angle_y, pixel_y):
    """How many notches' worth of zoom one event represents (signed, clamped)."""
    if angle_y:
        amount = angle_y / MOUSE_NOTCH
    elif pixel_y:
        amount = pixel_y / PIXELS_PER_NOTCH
    else:
        return 0.0
    return max(-MAX_NOTCHES_PER_EVENT, min(MAX_NOTCHES_PER_EVENT, amount))


def clamp_sensitivity(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return DEFAULT_SENSITIVITY
    return max(MIN_SENSITIVITY, min(MAX_SENSITIVITY, value))


def zoom_factor(step, amount, sensitivity):
    """The scale to apply: `step` raised to (notches x sensitivity).

    An exponent rather than a multiplier so that half a notch twice zooms exactly as far
    as one notch once, and zooming in then out by the same amount lands back where it
    started.
    """
    return step ** (amount * clamp_sensitivity(sensitivity))
