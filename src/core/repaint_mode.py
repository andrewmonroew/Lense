"""Deciding how much of the canvas to repaint, per display.

Partial repaints (Qt's SmartViewportUpdate) are what keep dragging fast on a large
floor plan: only the region an item declares gets redrawn, instead of re-scaling a
54-megapixel scan every frame.

They also assume the region Qt computes in logical pixels lands exactly on device
pixels. At a fractional display scale -- Windows' 125% and 150%, which most laptops
ship with -- it does not: logical coordinates map to fractional device pixels, the
rounding leaves a sliver of the old paint unredrawn, and over a drag those slivers
accumulate into the grey boxes trailing behind everything the user moves.

Whole-viewport repaints have no such edge to get wrong. They cost more, which is why
they are used only where the display makes partial ones unreliable (and the floor plan
is cached in device coordinates to blunt that cost -- see FloorPlanItem).

Kept free of Qt so the choice can be tested with plain numbers.
"""

AUTO = "auto"
PARTIAL = "partial"
FULL = "full"

PREFERENCES = (AUTO, PARTIAL, FULL)
DEFAULT_PREFERENCE = AUTO

# How far from a whole number a scale factor may sit and still count as integral.
# Qt reports these as floats, so 2.0 can arrive as 1.9999999.
EPSILON = 0.01


def is_fractional(device_pixel_ratio):
    """True when the display scale is not a whole multiple (125%, 150%, 175%)."""
    try:
        ratio = float(device_pixel_ratio)
    except (TypeError, ValueError):
        return False
    if ratio <= 0:
        return False
    return abs(ratio - round(ratio)) > EPSILON


def choose(device_pixel_ratio, preference=AUTO):
    """Which repaint strategy to use: PARTIAL (fast) or FULL (always correct).

    An explicit preference always wins -- if someone's display still leaves trails, or
    they would rather have the speed back, the setting is the final word over anything
    guessed from the scale factor.
    """
    if preference == PARTIAL:
        return PARTIAL
    if preference == FULL:
        return FULL
    return FULL if is_fractional(device_pixel_ratio) else PARTIAL
