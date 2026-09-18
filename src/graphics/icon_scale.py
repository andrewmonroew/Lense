"""Canvas icon scaling.

Equipment icons are drawn at fixed sizes in scene coordinates, but an imported floor
plan can be any resolution at all -- put a 12000px architectural scan behind a 32px
switch icon and the icon is a speck you can barely click, let alone read. The canvas
carries a `global_icon_scale` that every icon multiplies into its own geometry, so a
design stays legible over an image of any size.

Deliberately NOT done with QGraphicsItem.setScale(), which scales an item's entire
local coordinate space: on a camera that would take the FOV wedge and IR ring with it,
and those are real-world measurements derived from the calibration ratio. Stretching
them would turn a coverage diagram into a lie. Only the icon glyph scales; anything
measured in feet stays measured in feet.
"""

DEFAULT_ICON_SCALE = 1.0
MIN_ICON_SCALE = 0.25
MAX_ICON_SCALE = 8.0


def icon_scale_of(item):
    """The canvas icon scale in effect for `item` (1.0 when it isn't on a canvas)."""
    scene = item.scene()
    if scene is None:
        return DEFAULT_ICON_SCALE
    return getattr(scene, "global_icon_scale", DEFAULT_ICON_SCALE)


def clamp_icon_scale(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return DEFAULT_ICON_SCALE
    return max(MIN_ICON_SCALE, min(MAX_ICON_SCALE, value))


def scaled_size(name):
    """Declares a size attribute that reads back multiplied by the canvas icon scale.

    Assignment stores the natural (1x) size, so an item's existing
    `self.width = 32.0` in __init__ keeps meaning exactly what it always did, while
    every read -- in boundingRect(), shape() and paint() alike -- follows the scale
    without those methods needing to know it exists.
    """
    private = "_base_" + name

    def getter(self):
        return getattr(self, private, 0.0) * icon_scale_of(self)

    def setter(self, value):
        setattr(self, private, value)

    return property(getter, setter, doc=f"{name}, scaled by the canvas icon scale.")


def natural_size(name):
    """Read-only companion to scaled_size: the design-time size, ignoring icon scale.

    Paint methods draw an icon's *artwork* in natural coordinates with the painter
    scaled (see paint_at_icon_scale), so they need the unscaled numbers; anything
    positioned around the outside of the icon -- a label, a warning badge, a selection
    ring -- uses the scaled ones so it sits at the icon's real edge.
    """
    private = "_base_" + name

    def getter(self):
        return getattr(self, private, 0.0)

    return property(getter, doc=f"{name} at its natural size, ignoring icon scale.")


def paint_at_icon_scale(painter, item):
    """Scales `painter` so artwork drawn at natural coordinates matches the icon size.

    An icon's outer shell is drawn from self.width/self.height, which already follow
    the canvas scale -- but the detail *inside* it (a Wi-Fi fan, a row of port squares,
    a plug) is drawn at fixed literal coordinates chosen against the natural size.
    Scaling the painter instead of hand-converting every literal means nothing gets
    missed, and stroke weights grow with the icon rather than staying hairlines on a
    shape eight times their intended size.

    Returns the scale applied, and expects the caller to be inside a save()/restore().

    IMPORTANT: the returned scale multiplies the pen as well as the geometry, so any
    quantity the caller wants to stay a constant number of pixels on screen -- every
    `X * scale` in these paint methods, where scale is 1/lod -- has to divide it back
    out. Use `screen = scale / paint_at_icon_scale(painter, item)` and draw stroke
    weights and pixel-sized details with `screen` instead of `scale`. Without that a
    2px outline becomes 7px at 350%, which reads as a bloated sausage around an
    otherwise correctly-sized icon.
    """
    scale = icon_scale_of(item)
    if scale != 1.0:
        painter.scale(scale, scale)
    return scale
