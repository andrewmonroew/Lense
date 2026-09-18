from PySide6.QtGui import QFont, QColor, QBrush, QPen
from PySide6.QtCore import QRectF, QPointF, Qt


# Base point size for item labels. A canvas-wide setting rather than a per-item one:
# labels are drawn at a constant on-screen size (see the zoom note below), so the only
# thing that makes them too small or too large is the reader's display and preference.
DEFAULT_LABEL_FONT_SIZE = 9
_label_font_size = DEFAULT_LABEL_FONT_SIZE


def set_label_font_size(size):
    """Sets the base point size for every item label drawn from here on."""
    global _label_font_size
    try:
        _label_font_size = max(6, min(20, int(size)))
    except (TypeError, ValueError):
        _label_font_size = DEFAULT_LABEL_FONT_SIZE
    return _label_font_size


def label_font_size():
    return _label_font_size


def label_local_size(text, lod, font_size=None):
    """Approximate (width, height) of a drawn label, in the item's LOCAL units.

    Labels render at a constant on-screen size (draw_item_label cancels the view
    transform), so their footprint in local units grows as you zoom out -- by 1/lod.
    Estimated from the text length rather than measured with QFontMetrics because this
    feeds boundingRect(), which Qt calls hundreds of times per frame. Deliberately
    generous: over-reporting costs a slightly larger repaint region, under-reporting
    leaves smeared label trails behind a dragged item.
    """
    size = _label_font_size if font_size is None else font_size
    width_px = size * 0.75 * max(1, len(text or "")) + 20.0
    height_px = size * 2.2 + 8.0
    lod = lod if lod and lod > 0 else 1.0
    return width_px / lod, height_px / lod


# A warning badge is drawn at constant on-screen size too, hanging off the icon's top
# corner, so it needs the same 1/lod allowance as a label.
BADGE_RADIUS_PX = 7.0


# Gap left between stacked labels, in screen pixels. Tight on purpose: a stack has to
# stay visibly close to the things it labels, and every pixel of padding multiplies by
# the number of rows into labels drifting off into open space.
LABEL_ROW_GAP_PX = 1.0


_row_step_cache = (None, 0.0)


def label_row_step_px():
    """How far one label drops to clear the one above it, in screen pixels.

    Derived from the label's own height rather than fixed: a step shorter than a label
    means row 1 still collides and the layout has to skip to row 2, wasting the rows it
    has and pushing stacks further down the page than they need to go.

    Memoized against the font size, because bounding rects are recomputed hundreds of
    times a frame and this sits squarely on that path.
    """
    global _row_step_cache
    if _row_step_cache[0] != _label_font_size:
        _row_step_cache = (_label_font_size,
                           label_local_size("", 1.0)[1] + LABEL_ROW_GAP_PX)
    return _row_step_cache[1]


def anchor_y_for_row(base_y, row, lod, label_height):
    """Where a label sits for a given row, in the item's local units.

    Row 0 is the natural spot just under the icon. Positive rows step further down;
    NEGATIVE rows go above the icon instead, mirroring base_y to clear it and then
    stepping upward. Going both ways roughly halves how far a label in a crowd ends up
    from the thing it names -- pushing everything downward marched the lower labels off
    into empty space.
    """
    if row == 0:
        return base_y
    step = label_row_step_px() / (lod if lod and lod > 0 else 1.0)
    if row > 0:
        return base_y + row * step
    # base_y clears the icon downward, so -base_y clears it upward; back off by the
    # label's own height so it sits above the icon rather than straddling it.
    return -base_y - label_height + (row + 1) * step


def _item_lod(item):
    scene = item.scene()
    lod = getattr(scene, "view_lod", 1.0) if scene else 1.0
    return lod if lod and lod > 0 else 1.0


def label_anchor_y(item, base_y):
    """The final y for this item's label, given whichever row it was assigned."""
    row = getattr(item, "label_row", 0)
    if not row:
        return base_y
    lod = _item_lod(item)
    height = label_local_size(getattr(item, "label", ""), lod)[1]
    return anchor_y_for_row(base_y, row, lod, height)


def label_anchor(item, base_y):
    """Where to draw this item's label, accounting for any anti-collision offset.

    Paint sites use this and expand_for_label applies the same offset, so the drawn
    label and the rect Qt repaints can never drift apart.
    """
    return QPointF(0.0, label_anchor_y(item, base_y))


def expand_for_label(item, rect, anchor_y, lines=1, badge=True):
    """Grows `rect` to cover the label drawn at anchor_y below the item's origin.

    Without this an item's bounding rect stops at its icon, Qt has no idea the label
    was painted outside it, and any viewport update mode short of repainting
    everything leaves the old label on screen when the item moves.
    """
    if getattr(item, "is_preview", False):
        # The ghost on the cursor draws neither a label nor a warning badge, so it needs
        # no room for them. Reserving it anyway gave the thing a bounding rect thousands
        # of units across at low zoom -- a huge region marked dirty on every mouse move,
        # for artwork that is never painted.
        return rect
    scene = item.scene()
    lod = getattr(scene, "view_lod", 1.0) if scene else 1.0
    width, height = label_local_size(getattr(item, "label", ""), lod)
    anchor_y = anchor_y_for_row(anchor_y, getattr(item, "label_row", 0), lod, height)
    rect = rect.united(QRectF(-width / 2.0, anchor_y, width, height * lines))
    if badge:
        pad = (BADGE_RADIUS_PX * 2.0 + 4.0) / lod
        rect = rect.adjusted(-pad, -pad, pad, 0.0)
    return rect


def draw_item_label(painter, anchor_pt, text, scale, font_size=None):
    """Draws an item label with a solid backdrop pill, anchored at anchor_pt (in the
    item's normal, un-scaled local coordinate space -- same frame as the rest of the
    item's geometry, so the anchor still tracks the icon as it grows/shrinks with zoom).

    Fixes two things a plain painter.drawText() gets wrong for canvas items:
    - Contrast: white text drawn directly over the floorplan is unreadable wherever
      the image underneath happens to be light. A solid backdrop guarantees contrast
      against any background, without needing to sample pixel colors underneath.
    - Zoom clipping: text drawn as fixed-point-size local geometry grows with the
      view's zoom transform, while a bounding rect sized by a fixed formula (rather
      than the actual text metrics) does not grow in step -- at high zoom the glyphs
      outgrow their box and get clipped. Applying painter.scale(scale, scale) here
      (scale = 1/lod, the same convention used for constant-width pens elsewhere in
      this codebase) cancels the view's zoom for everything drawn after it, so the
      font renders at a true constant on-screen size and the backdrop -- sized from
      that same font's metrics -- always fits it exactly, at any zoom level.
    """
    if not text:
        # No text, no pill. An empty label used to still paint its backdrop, which on
        # the ghost that follows the cursor meant a grey smear trailing the mouse: it
        # painted below a bounding rect that (correctly) reserved no room for a label.
        return

    painter.save()
    painter.translate(anchor_pt)
    painter.scale(scale, scale)

    font = QFont("Inter", _label_font_size if font_size is None else font_size)
    font.setWeight(QFont.DemiBold)
    painter.setFont(font)

    metrics = painter.fontMetrics()
    text_width = metrics.horizontalAdvance(text)
    text_height = metrics.height()
    pad_x, pad_y = 5.0, 2.0
    rect = QRectF(-text_width / 2.0 - pad_x, 0.0, text_width + pad_x * 2.0, text_height + pad_y * 2.0)

    painter.setPen(Qt.NoPen)
    painter.setBrush(QBrush(QColor(10, 10, 16, 184)))
    painter.drawRoundedRect(rect, 3.0, 3.0)

    painter.setPen(QColor("#f5f5fa"))
    painter.drawText(rect, Qt.AlignCenter, text)
    painter.restore()


def draw_halo_text(painter, anchor_pt, text, scale, font_size=13, text_color="#ffffff",
                    outline_color="#0a0a0f", bold=True, valign="center"):
    """Draws text with a dark outline halo instead of a solid backdrop -- for annotations
    (labels, zone names) that are meant to sit directly over the floorplan with a
    genuinely transparent background, rather than device/cable labels which use
    draw_item_label's solid pill. The halo still guarantees legibility against any
    underlying color without blocking the floorplan behind a filled rectangle.

    Same zoom-constant-size technique as draw_item_label (painter.scale(scale, scale)
    before measuring/drawing), so it doesn't clip at high zoom either.

    valign controls where anchor_pt falls relative to the text block: "center" (the
    default) straddles it evenly; "top" grows the text downward from anchor_pt (its
    top edge lands exactly there); "bottom" grows it upward (its bottom edge lands
    there). "top"/"bottom" are what let a caller place text flush against a line with
    an exact, zoom-constant pixel gap (offset anchor_pt by that many pixels' worth of
    scene units, same `* scale` convention as everywhere else in this codebase) rather
    than centering across it.
    """
    painter.save()
    painter.translate(anchor_pt)
    painter.scale(scale, scale)

    font = QFont("Inter", font_size)
    if bold:
        font.setWeight(QFont.Bold)
    painter.setFont(font)

    metrics = painter.fontMetrics()
    text_width = metrics.horizontalAdvance(text) if text else 0
    text_height = metrics.height()
    if valign == "top":
        rect = QRectF(-text_width / 2.0, 0.0, text_width, text_height)
    elif valign == "bottom":
        rect = QRectF(-text_width / 2.0, -text_height, text_width, text_height)
    else:
        rect = QRectF(-text_width / 2.0, -text_height / 2.0, text_width, text_height)

    painter.setPen(QColor(outline_color))
    for dx, dy in ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)):
        painter.drawText(rect.translated(dx, dy), Qt.AlignCenter, text)

    painter.setPen(QColor(text_color))
    painter.drawText(rect, Qt.AlignCenter, text)
    painter.restore()


def draw_warning_badge(painter, anchor_pt, scale, radius=7.0):
    """Draws a small filled amber circle with a black '!' at anchor_pt, constant
    on-screen size at any zoom (same painter.scale(scale, scale) technique as
    draw_item_label/draw_halo_text above). Used for network-design sanity checks
    (unconnected camera, oversubscribed switch) that need to catch the eye without
    a tooltip already being open."""
    painter.save()
    painter.translate(anchor_pt)
    painter.scale(scale, scale)

    painter.setPen(QPen(QColor("#78350f"), 1.0))
    painter.setBrush(QBrush(QColor("#f59e0b")))
    painter.drawEllipse(QPointF(0, 0), radius, radius)

    font = QFont("Inter", int(radius * 1.3))
    font.setWeight(QFont.Bold)
    painter.setFont(font)
    painter.setPen(QColor("#1c1006"))
    painter.drawText(QRectF(-radius, -radius, radius * 2, radius * 2), Qt.AlignCenter, "!")
    painter.restore()
