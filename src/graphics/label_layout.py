"""Keeps item labels from running into each other.

Labels render at a constant on-screen size, so their footprint in scene units grows as
you zoom out: two wall drops eight feet apart read fine up close and collide into an
unreadable smear once the whole floor is in view. Rather than shrink or hide them, this
pushes colliding labels down a row at a time -- the standard cartographic answer, and
the one that keeps every label readable and attached to something.

The assignment is recomputed whenever it could have changed (zoom, moves, renames) and
is deliberately stable: an item keeps row 0 unless something is genuinely in the way, so
labels settle back down the moment you zoom in far enough for them to fit. Nothing is
sticky, so you never end up with a stack that has no reason to exist any more.
"""

from PySide6.QtCore import QRectF

from src.graphics.label_render import (anchor_y_for_row, label_local_size,
                                       label_row_step_px)

# How far a label may stray from its item, in rows, in EITHER direction. Kept small on
# purpose: a label that has marched half a screen away from the thing it names has
# stopped being a label. Beyond this the design is simply too dense to letter at this
# zoom, and the last row is reused rather than wandering further.
MAX_ROWS = 3

# Rows are tried nearest-first and alternate sides, so a crowd fills in around its items
# instead of growing a tail downward: 0, just above, just below, next above, ...
SEARCH_ORDER = [0]
for _n in range(1, MAX_ROWS + 1):
    SEARCH_ORDER += [-_n, _n]

# Labels this much closer than touching are treated as colliding. Small, so a stack
# packs tightly rather than reserving space it doesn't need.
GAP_PX = 1.0

LABELLED_TYPES = ("camera", "device", "rack", "custom")


def _anchor_offset(item):
    """Where an item's label sits relative to its origin, before any row shift."""
    object_type = getattr(item, "object_type", None)
    if object_type == "camera":
        return getattr(item, "icon_radius", 12.0) + 4.0
    if object_type == "rack":
        return item._icon_height() / 2.0 + 4.0 if hasattr(item, "_icon_height") else 26.0
    if object_type == "custom":
        if getattr(item, "icon_type", None) == "rack":
            return getattr(item, "rack_height", 18.0) / 2.0 + 4.0
        return getattr(item, "radius", 12.0) + 4.0
    return getattr(item, "height", 20.0) / 2.0 + 2.0


def label_rect(item, lod, row):
    """The label's footprint in SCENE coordinates at a given row.

    Goes through the same anchor maths the renderer uses, so what the layout tests for
    collisions is exactly what gets painted.
    """
    width, height = label_local_size(getattr(item, "label", ""), lod)
    gap = GAP_PX / (lod if lod and lod > 0 else 1.0)
    top = item.y() + anchor_y_for_row(_anchor_offset(item), row, lod, height)
    return QRectF(item.x() - width / 2.0, top - gap / 2.0, width, height + gap)


def labelled_items(scene):
    return [item for item in scene.items()
            if getattr(item, "object_type", None) in LABELLED_TYPES
            and getattr(item, "label", "")]


def assign_rows(scene, lod=None):
    """Works out each label's row. Returns {item: row} for the ones that must change.

    Straightforward greedy placement: take labels top-down and put each on the nearest
    free row, trying above and below alternately so a crowd packs around its items rather
    than growing a tail downward. Top-down ordering matters -- the upper label of a
    colliding pair keeps its natural position and the lower one moves, which reads as
    "that one stepped aside" rather than everything shuffling at once.
    """
    if lod is None:
        lod = getattr(scene, "view_lod", 1.0) or 1.0
    if not getattr(scene, "global_show_icons", True):
        # Labels aren't drawn at all, so nothing can collide; leave every row alone
        # rather than churning the whole scene's geometry for an invisible layout.
        return {}

    items = labelled_items(scene)
    items.sort(key=lambda i: (i.y(), i.x()))

    placed = []          # (rect, row) already committed this pass
    changes = {}
    for item in items:
        chosen, chosen_rect = 0, None
        for row in SEARCH_ORDER:
            candidate = label_rect(item, lod, row)
            if not any(candidate.intersects(other) for other, _r in placed):
                chosen, chosen_rect = row, candidate
                break
        if chosen_rect is None:
            # Nowhere clear within the cap -- take the furthest row rather than wandering
            # off the page, and accept the overlap. At this density the honest answer is
            # that there is no room, not that the label belongs somewhere else entirely.
            chosen = MAX_ROWS
            chosen_rect = label_rect(item, lod, chosen)
        placed.append((chosen_rect, chosen))
        if getattr(item, "label_row", 0) != chosen:
            changes[item] = chosen
    return changes
