import math
import time
import uuid
from PySide6.QtWidgets import QGraphicsPathItem, QGraphicsItem
from PySide6.QtGui import QPen, QColor, QBrush, QFont, QPainterPath, QPainterPathStroker
from src.graphics.icon_scale import icon_scale_of
from PySide6.QtCore import QRectF, QPointF, Qt
from src.core.utils import distance, distance_to_segment, closest_point_on_segment, format_distance, CAT6_MAX_RUN_FEET, FIBER_MAX_RUN_FEET, PATCH_MAX_RUN_FEET

# The dark stroke drawn under every cable. Near-black rather than pure black so it
# reads as a shadow on a dark canvas rather than a hard hole, and wide enough to show a
# sliver either side of the core at the thinnest line weight.
CASING_COLOR = QColor("#0b0b12")
CASING_WIDTH_PX = 2.6


class CableItem(QGraphicsPathItem):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.object_type = "cable"
        self.id = str(uuid.uuid4())
        
        # State
        self.points = []            # Raw list of QPointF waypoints
        self.draw_points = []       # Offset-adjusted QPointF waypoints for rendering
        self.segment_widths = []    # Per-segment line-width multiplier (see set_points)
        self.vertex_anchors = []    # Parallel list: device/camera id anchoring that vertex, or None if free
        self.label = "Cable"
        self.cable_type = "CAT6"
        self.notes = ""
        self.start_device_id = None # ID of device/camera (convenience alias of vertex_anchors[0])
        self.end_device_id = None   # convenience alias of vertex_anchors[-1]

        # Which numbered port on a patch panel (see PatchPanelItem) each end is
        # plugged into, if that end's anchor is a patch panel -- None otherwise
        # (including for whole-device anchors like a switch/NVR/camera, which don't
        # have per-port tracking in this pass). This is what lets the connectivity
        # chain-walker (CanvasView.resolve_camera_chain) follow a field cable into a
        # specific port and out again via whatever patch cable is plugged into that
        # same port on the other side.
        self.start_port = None
        self.end_port = None
        
        self.setZValue(5) # Draw above floorplan, below camera/devices
        self.setFlags(QGraphicsItem.ItemIsSelectable)
        self.setAcceptHoverEvents(True)
        self.pixel_length = 0.0

        # Vertex-drag editing (toggled via double-click with the Select/Move tool)
        self.editing_vertices = False
        self.dragging_vertex_index = None
        self.vertex_handle_radius = 6.0

        # Length pill only shows on hover, anchored to the cursor rather than the cable's
        # midpoint -- with many cables on screen, permanent mid-cable labels scattered
        # everywhere in different orientations made the canvas unreadable.
        self.is_hovered = False
        self.hover_local_pos = QPointF()

        # Set by CanvasView when a camera/device this cable is anchored to gets
        # selected -- renders the same white highlight as an actually-selected cable,
        # without this cable itself being selected (no selection handles, not part of
        # a Delete-key selection, etc). Lets an orphaned camera with no cable at all
        # be spotted at a glance: select it, and nothing lights up.
        self.connected_highlight = False

    def set_points(self, points, anchors=None):
        self.prepareGeometryChange()
        self.points = points
        old_start, old_end = self.start_device_id, self.end_device_id
        if anchors is not None:
            self.vertex_anchors = anchors
        elif len(self.vertex_anchors) != len(points):
            # Point count changed without explicit anchors (e.g. inserting a vertex) —
            # preserve what we can and default the rest to free/unanchored.
            self.vertex_anchors = (self.vertex_anchors + [None] * len(points))[:len(points)]
        self.start_device_id = self.vertex_anchors[0] if self.vertex_anchors else None
        self.end_device_id = self.vertex_anchors[-1] if self.vertex_anchors else None
        # A port assignment only means anything relative to the anchor it was recorded
        # against -- if that end got re-anchored to something else, the old port number
        # is meaningless (and possibly still occupied by a different cable now).
        if self.start_device_id != old_start:
            self.start_port = None
        if self.end_device_id != old_end:
            self.end_port = None
        self.draw_points = list(points) # Default to raw points
        # Per-segment line-width multiplier (1.0 = normal). recalculate_all_cable_offsets
        # sets this above 1.0 for segments shared with other cables, so a bundle renders
        # as one merged thick line rather than each cable drawing its own thin copy.
        self.segment_widths = [1.0] * max(0, len(points) - 1)
        self.calculate_length()
        self.invalidate_length()
        self.update_path()

    def insert_vertex_at(self, scene_pt):
        """Inserts a new free vertex into this cable at the point on its path closest to scene_pt."""
        if len(self.points) < 2:
            return
        best_idx, best_dist, best_proj = None, None, None
        for i in range(1, len(self.points)):
            a = (self.points[i - 1].x(), self.points[i - 1].y())
            b = (self.points[i].x(), self.points[i].y())
            proj, _t = closest_point_on_segment((scene_pt.x(), scene_pt.y()), a, b)
            d = distance((scene_pt.x(), scene_pt.y()), proj)
            if best_dist is None or d < best_dist:
                best_idx, best_dist, best_proj = i, d, proj
        if best_idx is None:
            return

        new_points = list(self.points)
        new_points.insert(best_idx, QPointF(best_proj[0], best_proj[1]))
        new_anchors = list(self.vertex_anchors) if self.vertex_anchors else [None] * len(self.points)
        new_anchors.insert(best_idx, None)
        self.set_points(new_points, anchors=new_anchors)

        canvas = self._find_canvas_view()
        if canvas:
            recalculate_all_cable_offsets(self.scene())

    def calculate_length(self):
        length = 0.0
        for i in range(1, len(self.points)):
            length += distance(self.points[i-1], self.points[i])
        self.pixel_length = length

    def horizontal_feet(self):
        """What the floor plan measures, ignoring vertical rise and service loops."""
        scene = self.scene()
        if scene and hasattr(scene, "scale_ratio") and scene.scale_ratio:
            return self.pixel_length / scene.scale_ratio
        return None

    # Finding what sits at each end means a lookup per end, and paint() needs the length
    # for every cable on every frame (is_over_limit picks the colour). Only that lookup
    # is cached: the allowances themselves are plain attribute reads, so editing one in
    # the properties panel takes effect immediately rather than a quarter second later.
    ENDPOINT_REFRESH_SECONDS = 0.25

    def invalidate_length(self):
        self._endpoints_checked_at = None

    def _resolved_endpoints(self):
        now = time.monotonic()
        checked = getattr(self, "_endpoints_checked_at", None)
        if checked is not None and now - checked < self.ENDPOINT_REFRESH_SECONDS:
            return self._endpoints_cache
        from src.core import cable_length
        view = self._find_canvas_view()
        self._endpoints_cache = cable_length.endpoint_items(self, view) if view else (None, None)
        self._endpoints_checked_at = now
        return self._endpoints_cache

    def length_breakdown(self):
        """(horizontal, vertical, loop, total) in feet, or None when uncalibrated."""
        from src.core import cable_length
        return cable_length.breakdown(self, endpoints=self._resolved_endpoints())

    def get_length_feet(self):
        """Cable actually pulled: the measured run plus each end's rise and slack.

        The plan only knows the horizontal distance, but a drop costs cable going up to
        the pathway, across, and back down, plus a coil at each end -- so this is what
        the takeoff, the cost, and the maximum-run check all need. horizontal_feet() is
        there for anything that genuinely wants the flat measurement.
        """
        parts = self.length_breakdown()
        if parts is not None:
            return parts[3]
        return self.horizontal_feet()

    def max_run_feet(self):
        if self.cable_type == "Fiber":
            return FIBER_MAX_RUN_FEET
        if self.cable_type == "Patch":
            return PATCH_MAX_RUN_FEET
        return CAT6_MAX_RUN_FEET

    def is_over_limit(self):
        ft = self.get_length_feet()
        return ft is not None and ft > self.max_run_feet()

    def _find_canvas_view(self):
        scene = self.scene()
        if scene and scene.views():
            return scene.views()[0]
        return None

    def _vertex_at(self, local_pt):
        """Returns the index of the FREE (unanchored) vertex under local_pt, or None.
        Anchored vertices are locked to their device and can't be dragged directly —
        they only move when the device they're anchored to moves."""
        hit_radius = (self.vertex_handle_radius + 5.0) * icon_scale_of(self)
        for i, pt in enumerate(self.points):
            if self._anchor_at(i) is not None:
                continue
            if distance((local_pt.x(), local_pt.y()), (pt.x(), pt.y())) <= hit_radius:
                return i
        return None

    def _anchor_at(self, index):
        if index < len(self.vertex_anchors):
            return self.vertex_anchors[index]
        return None

    def _try_anchor_vertex(self, index, radius=20.0):
        """Anchors a free vertex to whichever camera/device it was just dropped on top
        of, if any -- the other direction of reattaching a dangling cable end: instead
        of dragging a replacement device onto the cable, drag the loose cable end onto
        an existing device."""
        canvas = self._find_canvas_view()
        if canvas is None or index >= len(self.points):
            return False
        pt = self.points[index]
        # Devices mounted inside a rack sit at the exact same position as their rack,
        # so they're excluded here in favor of the rack itself -- nothing to visually
        # target them individually with a dragged vertex anyway.
        loose_devices = [d for d in canvas.get_network_devices() if canvas.find_rack_containing_device(d.id) is None]
        candidates = canvas.get_cameras() + loose_devices + canvas.get_racks() + canvas.get_custom_objects()

        # An end dropped inside a big icon's shape (a rack is 34x44, well past a 20px
        # centre radius) wins outright; otherwise fall back to nearest-centre. Without
        # the shape pass, dropping a run squarely on a rack silently failed to anchor,
        # so it never showed up as a pitchfork stub in the Rack Editor.
        best, best_dist = None, radius
        for item in candidates:
            if item.shape().contains(item.mapFromScene(pt)):
                best = item
                break
            d = distance((pt.x(), pt.y()), (item.x(), item.y()))
            if d < best_dist:
                best, best_dist = item, d
        if best is None:
            return False
        while len(self.vertex_anchors) <= index:
            self.vertex_anchors.append(None)
        self.vertex_anchors[index] = best.id
        self.points[index] = QPointF(best.pos())
        self.start_device_id = self.vertex_anchors[0] if self.vertex_anchors else None
        self.end_device_id = self.vertex_anchors[-1] if self.vertex_anchors else None
        self.calculate_length()
        return True

    # ── Vertex-drag editing ──
    def mouseDoubleClickEvent(self, event):
        canvas = self._find_canvas_view()
        if canvas and getattr(canvas, "active_tool", None) == "select" and event.button() == Qt.LeftButton:
            canvas.set_editing_cable(self)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mousePressEvent(self, event):
        if self.editing_vertices and event.button() == Qt.LeftButton:
            vertex_index = self._vertex_at(event.pos())
            if vertex_index is not None:
                self.dragging_vertex_index = vertex_index
                self.setCursor(Qt.ClosedHandCursor)
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.dragging_vertex_index is not None:
            self.points[self.dragging_vertex_index] = QPointF(event.pos())
            self.set_points(self.points)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self.dragging_vertex_index is not None:
            index = self.dragging_vertex_index
            self.dragging_vertex_index = None
            self.setCursor(Qt.OpenHandCursor)
            reattached = self._try_anchor_vertex(index)
            recalculate_all_cable_offsets(self.scene())
            if reattached:
                canvas = self._find_canvas_view()
                if canvas is not None:
                    canvas.refresh_connectivity_badges()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def hoverEnterEvent(self, event):
        self.is_hovered = True
        self.hover_local_pos = event.pos()
        self.update()
        super().hoverEnterEvent(event)

    def hoverMoveEvent(self, event):
        self.hover_local_pos = event.pos()
        if self.editing_vertices:
            near_vertex = self._vertex_at(event.pos()) is not None
            self.setCursor(Qt.OpenHandCursor if near_vertex else Qt.ArrowCursor)
        self.update()
        super().hoverMoveEvent(event)

    def hoverLeaveEvent(self, event):
        self.is_hovered = False
        self.unsetCursor()
        self.update()
        super().hoverLeaveEvent(event)

    def boundingRect(self):
        # Padded beyond the raw path bounds so the hover length pill -- which can render
        # to the right of the cursor anywhere along the cable, and is drawn at a constant
        # on-screen size independent of zoom -- never gets clipped or leaves stale repaint
        # artifacts at low zoom levels, where its local-coordinate footprint is largest.
        base = super().boundingRect()
        scene = self.scene()
        lod = getattr(scene, "view_lod", 1.0) if scene else 1.0
        pad = 220.0 / (lod if lod and lod > 0 else 1.0)
        return base.adjusted(-pad, -pad, pad, pad)

    def update_path(self):
        path = QPainterPath()
        pts = self.draw_points if self.draw_points else self.points
        if not pts:
            self.setPath(path)
            return

        path.moveTo(pts[0])
        for pt in pts[1:]:
            path.lineTo(pt)
        self.setPath(path)
        self.update()

    def shape(self):
        # Dilate path to make it easy to click.
        #
        # Memoized: Qt asks for shape() during hit-testing AND repeatedly while
        # repainting, so on a drag this ran well over a hundred times a frame, building
        # a fresh stroker each time. The key is everything the result depends on.
        pts = self.draw_points if self.draw_points else self.points
        width = 12.0 * icon_scale_of(self)
        key = (width, tuple((p.x(), p.y()) for p in pts))
        cached = getattr(self, "_shape_cache", None)
        if cached is not None and cached[0] == key:
            return cached[1]

        path = QPainterPath()
        if not pts:
            self._shape_cache = (key, path)
            return path

        stroker_path = QPainterPath()
        stroker_path.moveTo(pts[0])
        for pt in pts[1:]:
            stroker_path.lineTo(pt)

        stroker = QPainterPathStroker()
        # Raw scene units, unlike the drawn line weight -- so over a large floor plan
        # this really is a hairline you have to zoom in to hit, and it does need the
        # icon scale.
        stroker.setWidth(width)
        stroker.setCapStyle(Qt.RoundCap)
        stroker.setJoinStyle(Qt.RoundJoin)
        path = stroker.createStroke(stroker_path)
        self._shape_cache = (key, path)
        return path

    def paint(self, painter, option, widget=None):
        lod = option.levelOfDetailFromTransform(painter.worldTransform())
        scale = 1.0 / lod if lod > 0 else 1.0

        # Colors: fiber links render yellow instead of copper's blue, so a bundle's
        # mixed media stays visually distinguishable even before opening properties.
        over_limit = self.is_over_limit()
        base_color = QColor("#eab308") if self.cable_type == "Fiber" else QColor("#60a5fa")
        line_color = QColor("#ef4444") if over_limit else base_color
        if self.isSelected() or self.connected_highlight:
            line_color = QColor("#ffffff") # Selected (or a connected endpoint is selected) -- highlighted white
        if self.editing_vertices:
            line_color = QColor("#f59e0b") # Amber while actively editing vertices

        # 1. Draw line, segment by segment: a segment shared with other cables (part of
        # a bundle) renders at its own width multiplier -- thick and merged at rest,
        # normal width once fanned out on hover -- while a solo/diverging segment
        # always renders at the plain base width.
        base_width = 3.0 if self.isSelected() or self.editing_vertices or self.connected_highlight else 2.0
        # NOT scaled by the canvas icon scale, deliberately. Every width below is
        # multiplied by `scale` (= 1/lod), which already cancels the view transform and
        # renders a constant number of pixels on screen at any zoom -- that is why
        # cables stay perfectly readable on a huge floor plan while the icons, which are
        # plain scene geometry, shrink to specks. Multiplying an already zoom-invariant
        # width by the icon scale on top just draws sausages.
        pts = self.draw_points if self.draw_points else self.points
        painter.setBrush(Qt.NoBrush)

        # Casing: a dark stroke laid under the coloured core, so a highlighted cable
        # carries its own contrast instead of borrowing the background's. A white
        # "selected" line over a white floor plan was simply invisible, and tinting it to
        # the inverse of what is underneath does not fix that -- inverting a mid grey
        # gives back the same grey, and inverting a saturated mid-tone changes the hue
        # while leaving the luminance (which is what a thin line is actually read by)
        # almost unchanged.
        #
        # SELECTED cables only. Casing every run thickened the entire cable layer and
        # made a busy plan look bloated, for contrast that only the highlight needs --
        # an unselected run already reads fine in its own colour.
        for casing in ((True, False) if self.isSelected() else (False,)):
            if casing:
                painter.setPen(Qt.NoPen)
            for i in range(1, len(pts)):
                width_mult = self.segment_widths[i - 1] if (i - 1) < len(self.segment_widths) else 1.0
                width = base_width * width_mult * scale
                if casing:
                    pen = QPen(CASING_COLOR, width + CASING_WIDTH_PX * scale,
                               Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
                else:
                    pen = QPen(line_color, width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
                painter.setPen(pen)
                painter.drawLine(pts[i - 1], pts[i])

        # 2. Draw waypoint nodes — larger, grabbable handles while vertex-editing.
        # Anchored (connected) vertices render green/locked; free vertices render amber/grabbable.
        if self.editing_vertices:
            handle_radius = self.vertex_handle_radius * scale
            for i, pt in enumerate(pts):
                anchored = self._anchor_at(i) is not None
                painter.setPen(QPen(QColor("#ffffff"), 1.5 * scale))
                painter.setBrush(QBrush(QColor("#22c55e") if anchored else QColor("#f59e0b")))
                painter.drawEllipse(pt, handle_radius, handle_radius)
        else:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(line_color))
            node_radius = 4.0 * scale
            for pt in pts:
                painter.drawEllipse(pt, node_radius, node_radius)

        # 3. Length pill -- only while hovered, anchored to the cursor (not the cable's
        # midpoint) and offset to its right, out of the way rather than scattered
        # across the canvas at whatever angle the segment happens to run.
        length_ft = self.get_length_feet()
        if self.is_hovered and length_ft and len(pts) >= 2:
            self._draw_hover_length_pill(painter, length_ft, over_limit, scale)

    def _draw_hover_length_pill(self, painter, length_ft, over_limit, scale):
        painter.save()
        painter.translate(self.hover_local_pos)
        painter.scale(scale, scale)  # constant on-screen size regardless of zoom

        label = f"{length_ft:.0f} ft"
        font = QFont("JetBrains Mono", 9)
        font.setBold(True)
        painter.setFont(font)

        metrics = painter.fontMetrics()
        tw = metrics.horizontalAdvance(label)
        th = metrics.height()

        pad_x, pad_y = 6.0, 3.0
        gap = 14.0  # clearance between the cursor and the pill's left edge

        rect = QRectF(gap, -th / 2.0 - pad_y, tw + pad_x * 2.0, th + pad_y * 2.0)

        bg_color = QColor("#ef4444") if over_limit else QColor("#111118")
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(bg_color))
        painter.drawRoundedRect(rect, 4.0, 4.0)

        text_color = QColor("#ffffff") if over_limit else QColor("#e8e8f0")
        painter.setPen(text_color)
        painter.drawText(rect, Qt.AlignCenter, label)

        painter.restore()


ANCHORABLE_TYPES = ("camera", "device", "rack", "custom")


def anchor_target_index(scene):
    """id -> item for everything a cable vertex can anchor to, in one pass.

    Built once per sync_anchored_vertices rather than scanned per anchor. The old
    per-anchor search walked the whole scene every time, so a drag cost roughly
    (cables x anchors x scene items) of work on every single mouse-move -- the thing
    that made dragging lag behind the cursor on a real design.
    """
    index = {}
    racks = []
    for item in scene.items():
        obj_type = getattr(item, "object_type", None)
        if obj_type == "rack":
            racks.append(item)
        if obj_type in ANCHORABLE_TYPES:
            item_id = getattr(item, "id", None)
            if item_id is not None:
                index.setdefault(item_id, item)
    # Devices mounted inside a rack are deliberately not scene items (that's the point
    # of a rack: hidden from the floor plan), so add them from their slots.
    for rack in racks:
        for slot in rack.slots:
            if slot.device is not None:
                index.setdefault(slot.device.id, slot.device)
    return index


def _find_anchor_target(scene, item_id):
    """Single-lookup form. Prefer anchor_target_index when resolving many anchors."""
    return anchor_target_index(scene).get(item_id)


def sync_anchored_vertices(scene):
    """Pulls every anchored cable vertex to match its live device/camera position.
    A vertex whose anchor device was deleted is freed (becomes a static point
    at its last known position) rather than staying attached to nothing."""
    if not scene:
        return
    cables = [item for item in scene.items() if getattr(item, "object_type", None) == "cable"]
    index = anchor_target_index(scene)
    for cable in cables:
        if not cable.vertex_anchors or not any(cable.vertex_anchors):
            continue
        changed = False
        for i, anchor_id in enumerate(cable.vertex_anchors):
            if not anchor_id:
                continue
            target = index.get(anchor_id)
            if target is None:
                cable.vertex_anchors[i] = None  # device gone -> detach into a free vertex
                if i == 0:
                    cable.start_port = None
                if i == len(cable.vertex_anchors) - 1:
                    cable.end_port = None
                changed = True
                continue
            live_pos = target.pos()
            if cable.points[i] != live_pos:
                cable.points[i] = QPointF(live_pos)
                changed = True
        if changed:
            cable.start_device_id = cable.vertex_anchors[0] if cable.vertex_anchors else None
            cable.end_device_id = cable.vertex_anchors[-1] if cable.vertex_anchors else None
            cable.calculate_length()


def recalculate_all_cable_offsets(scene, hover_pt=None):
    """Groups cables that run the same physical path into bundles.

    At rest, a bundle renders as a single merged line at bundle_thick_mult times the
    normal cable width -- every cable in it draws at the exact same (unshifted)
    position, so they visually coalesce into one thick line rather than several thin
    ones stacked a few pixels apart. Only where a cable actually diverges to its own
    destination does its line return to normal width and position.

    If hover_pt is given (world/scene coordinates), whichever bundle it's near fans
    out smoothly with distance into individual thin lines a few pixels apart -- with
    the *total* width of the fan capped regardless of how many cables share the run,
    so a bundle of 20 cables never sprawls into an unreadable spread.
    """
    if not scene:
        return

    sync_anchored_vertices(scene)

    cables = [item for item in scene.items() if getattr(item, "object_type", None) == "cable"]
    if not cables:
        return

    # Every distance here is in scene units, so it follows the canvas icon scale --
    # otherwise a fan-out that reads clearly at 1x collapses into a single thick line
    # over a high-resolution floor plan, and you have to zoom in to see what you are
    # separating. The thickness multiplier is a ratio, not a distance, so it stays put.
    # These stay on the full icon scale rather than the damped stroke scale: they are
    # distances BETWEEN runs, the same kind of quantity as an icon's width, and the
    # whole point of a fan-out is that the separation is big enough to see.
    icon_scale = getattr(scene, "global_icon_scale", 1.0)
    # How close the cursor has to get is a matter of aim, so it is measured in SCREEN
    # pixels: a scene-unit radius shrinks to nothing on screen as you zoom out, which is
    # what made a bundle seem not to fan at all -- you simply could not land on it.
    view_lod = getattr(scene, "view_lod", 1.0) or 1.0
    bundle_thick_mult = 3.0                # merged-bundle line width, as a multiple of normal
    open_radius = 26.0 / view_lod          # ~26 px of aim tolerance at any zoom
    hover_span = 50.0 * icon_scale      # total width a fanned bundle aims to occupy
    hover_max_gap = 25.0 * icon_scale   # but adjacent cables never separate by more than this

    # Build segment catalog
    # Key: Tuple representing snapped segment start/end.
    # We sort coordinates to ensure segment A->B and B->A are grouped together.
    # To handle slight offsets, we snap coordinates to a 10px grid.
    def snap_pt(pt):
        grid = 15.0 * icon_scale
        return (round(pt.x() / grid) * grid, round(pt.y() / grid) * grid)

    def make_segment_key(p1, p2):
        s1 = snap_pt(p1)
        s2 = snap_pt(p2)
        # Sort by x, then y
        if s1 < s2:
            return (s1, s2)
        return (s2, s1)

    # Dictionary of segment_key -> list of (cable_item, segment_index)
    segment_groups = {}

    # What each cable was rendering before this pass, so untouched ones can be left
    # alone at the end instead of being handed a fresh path.
    previous = [([(p.x(), p.y()) for p in (cable.draw_points or ())],
                 list(cable.segment_widths)) for cable in cables]

    for cable in cables:
        # Reset to true, unshifted position and normal width -- the default "merged"
        # state. Bundled segments get shifted/thickened below as their group requires.
        cable.draw_points = [QPointF(p) for p in cable.points]
        if len(cable.segment_widths) != max(0, len(cable.points) - 1):
            cable.segment_widths = [1.0] * max(0, len(cable.points) - 1)
        else:
            for i in range(len(cable.segment_widths)):
                cable.segment_widths[i] = 1.0

        # A cable actively being vertex-edited always shows its true, un-bundled
        # position and is left out of segment matching so it doesn't skew other bundles.
        if getattr(cable, "editing_vertices", False):
            continue

        # Catalog all segments
        for i in range(1, len(cable.points)):
            key = make_segment_key(cable.points[i-1], cable.points[i])
            if key not in segment_groups:
                segment_groups[key] = []
            segment_groups[key].append((cable, i-1))

    # ── Which bundle is open ──
    # Fanning proportionally to cursor distance meant the cables squirmed away as you
    # approached and closed as you leaned in to read them -- you ended up chasing the
    # thing you were trying to inspect. Instead exactly one bundle is open at a time,
    # fully, and it stays open across a much wider area than it took to open. Move
    # clearly away and it shuts; move into a different bundle and that one takes over.
    #
    # "A bundle" is every segment carrying the SAME set of cables, so a corridor run
    # opens along its whole length rather than one segment at a time.
    members_of = {key: frozenset(c.id for c, _i in insts)
                  for key, insts in segment_groups.items() if len(insts) > 1}

    def distance_to(key):
        p1, p2 = key
        return distance_to_segment((hover_pt.x(), hover_pt.y()), p1, p2)

    def nearest_within(radius, restrict=None):
        best, best_d = None, radius
        for key, ids in members_of.items():
            if restrict is not None and ids != restrict:
                continue
            d = distance_to(key)
            if d < best_d:
                best, best_d = ids, d
        return best

    # Two ways to fan, because they suit different habits: "latched" opens one bundle
    # fully and keeps it open (steady to read, nothing squirms away), "dynamic" is the
    # original feel where a bundle spreads the closer you get.
    dynamic = getattr(scene, "fanout_mode", "latched") == "dynamic"

    open_members = getattr(scene, "open_bundle", None)
    if dynamic:
        open_members = None
        scene.open_bundle = None
    elif hover_pt is not None:
        # Once open, a bundle's own cables have moved aside by up to half the fan span,
        # so the cursor can sit well off the original centreline while still plainly
        # inside the thing. Closing at the same radius that opened it would snap it shut
        # the moment it opened.
        close_radius = open_radius + hover_span * 0.75
        entering = nearest_within(open_radius)
        if entering is not None:
            open_members = entering
        elif open_members is None or nearest_within(close_radius, restrict=open_members) is None:
            open_members = None
        scene.open_bundle = open_members
    # hover_pt is None when something other than the cursor triggered this pass (a drag,
    # an icon-scale change). That says nothing about where the cursor is, so the open
    # bundle is left exactly as it was rather than being slammed shut.

    # For each group of overlapping segments, decide merged-vs-fanned rendering
    for key, seg_instances in segment_groups.items():
        k = len(seg_instances)
        if k <= 1:
            continue  # solo segment: stays at default true position + normal width

        # Calculate normal vector
        p1_snap, p2_snap = key
        dx = p2_snap[0] - p1_snap[0]
        dy = p2_snap[1] - p1_snap[1]
        length = math.sqrt(dx * dx + dy * dy)
        if length == 0:
            continue

        # Normal vector (perpendicular)
        nx = -dy / length
        ny = dx / length

        if dynamic:
            # Proportional: spreads smoothly as the cursor closes on the centreline.
            hover_factor = 0.0
            if hover_pt is not None:
                d = distance_to_segment((hover_pt.x(), hover_pt.y()), p1_snap, p2_snap)
                if d <= open_radius:
                    hover_factor = 1.0 - (d / open_radius)
        else:
            # All the way open or all the way merged -- never a moving target between.
            hover_factor = 1.0 if (open_members is not None
                                   and members_of.get(key) == open_members) else 0.0

        # Every bundle fans out across the same span, however many cables are in it,
        # with the gap between neighbours capped so a mere pair doesn't fly apart like
        # two unrelated routes. Deriving the gap from a small fixed preference instead
        # meant a 2-cable bundle separated by a sixth of what a 10-cable one did -- so
        # the big trunks fanned obviously while a branch off to two wall drops looked
        # like it was ignoring the hover entirely.
        per_cable_gap = min(hover_max_gap, hover_span / max(1, k - 1))
        seg_spacing = per_cable_gap * hover_factor
        width_mult = bundle_thick_mult - (bundle_thick_mult - 1.0) * hover_factor

        # Apply shifts + width to each segment instance
        for index, (cable, seg_idx) in enumerate(seg_instances):
            # Symmetrical offset, e.g. for k=3, offsets are -1, 0, 1
            multiplier = index - (k - 1) / 2.0
            offset_val = multiplier * seg_spacing

            # Shift start and end points of this segment. Waypoints might be shared
            # between multiple segments; the normal shift is applied directly to the
            # segment's own points, so shared corners don't get torn apart.
            shift = QPointF(nx * offset_val, ny * offset_val)
            cable.draw_points[seg_idx] += shift
            cable.draw_points[seg_idx + 1] += shift
            if seg_idx < len(cable.segment_widths):
                cable.segment_widths[seg_idx] = width_mult

    # Redraw only what actually moved. update_path() calls setPath(), which makes Qt
    # re-index the item in the scene's BSP tree and repaint it -- doing that to every
    # cable on every mouse-move was most of the cost of dragging anything on a design
    # with a real amount of cable in it, even though a drag only ever changes the
    # handful of runs attached to the thing being dragged.
    for cable, (old_points, old_widths) in zip(cables, previous):
        new_points = [(p.x(), p.y()) for p in cable.draw_points]
        if new_points != old_points or cable.segment_widths != old_widths:
            cable.update_path()
