import uuid
from PySide6.QtWidgets import QGraphicsItem
from PySide6.QtGui import QPen, QColor, QBrush, QPolygonF, QPainterPath, QPainterPathStroker
from PySide6.QtCore import QRectF, QPointF, Qt

from src.core.utils import distance, closest_point_on_segment
from src.graphics.label_render import draw_halo_text

# (stored value, display name) -- vertical placement of a zone's label relative to its
# bounding box, keyed so the Properties sidebar and save/load share one source of
# truth. "center" (today's original behavior) stays the default.
LABEL_POSITIONS = [
    ("above", "Above"),
    ("below_top", "Just Below Top Line"),
    ("center", "Center"),
    ("above_bottom", "Just Above Bottom"),
    ("below", "Just Below"),
]


class ZoneItem(QGraphicsItem):
    """A semi-transparent polygon overlay (restricted area, coverage zone, etc.) with
    its own label, drawn with the Label/Zone tool the same way cables are drawn --
    click to place each vertex, double-click/Enter to finish. Vertices are draggable
    (double-click with Select/Move to enter edit mode, same convention as CableItem),
    and new vertices can be inserted the same way cables support it."""

    def __init__(self, points=None, label="Zone", parent=None):
        super().__init__(parent)
        self.object_type = "zone"
        self.id = str(uuid.uuid4())

        self.points = points or []  # QPointF list, absolute scene coords (item stays at pos (0,0))
        self.label = label
        self.notes = ""
        self.fill_color = "#3b82f6"
        self.fill_opacity = 0.22
        self.border_color = "#3b82f6"

        # Vertical placement of the label relative to the zone's bounding box -- an
        # automated alternative to free dragging, so a label can be kept clear of
        # whatever else is nearby without having to babysit its exact position by
        # hand. See LABEL_POSITIONS for the available keys.
        self.label_position = "center"

        # True for a quick-drawn bounding-box label (CanvasView's click-and-drag path
        # on the Label/Zone tool) -- constrains vertex-drag editing to a rectangle
        # resize (opposite corner stays fixed, the other two follow) instead of the
        # free-form independent-vertex dragging a hand-drawn polygon zone allows.
        self.is_box = False

        self.setZValue(-50)  # Above the floorplan, below cameras/devices/cables
        self.setFlags(QGraphicsItem.ItemIsSelectable)
        self.setAcceptHoverEvents(True)

        # Vertex-drag editing (toggled via double-click with the Select/Move tool)
        self.editing_vertices = False
        self.dragging_vertex_index = None
        self.vertex_handle_radius = 6.0
        self._box_resize_anchor = None  # fixed opposite corner, captured when a box resize-drag starts

    def set_points(self, points):
        self.prepareGeometryChange()
        self.points = points
        self.update()

    def boundingRect(self):
        if not self.points:
            return QRectF()
        xs = [p.x() for p in self.points]
        ys = [p.y() for p in self.points]
        # Generous enough to cover an "above"/"below" label's halo text too -- like
        # LabelItem, this can't track the label's true on-screen footprint exactly
        # (draw_halo_text keeps it a constant screen size regardless of zoom), it just
        # needs to comfortably contain it across the app's practical zoom range.
        pad = 50.0
        return QRectF(min(xs) - pad, min(ys) - pad, (max(xs) - min(xs)) + pad * 2, (max(ys) - min(ys)) + pad * 2)

    def shape(self):
        path = QPainterPath()
        if len(self.points) >= 3:
            path.addPolygon(QPolygonF(self.points + [self.points[0]]))
        elif len(self.points) == 2:
            line_path = QPainterPath()
            line_path.moveTo(self.points[0])
            line_path.lineTo(self.points[1])
            stroker = QPainterPathStroker()
            stroker.setWidth(12.0)
            path = stroker.createStroke(line_path)
        return path

    LABEL_EDGE_GAP_PX = 3.0  # constant on-screen pixel gap between the text and the line

    def label_anchor_point(self, scale=1.0):
        """Where the label renders, per self.label_position, and how draw_halo_text
        should align text against that point (see its valign param) -- horizontally
        centered on the average of the vertices either way, vertically flush against
        the bounding box's top/bottom edge with just a few pixels of daylight (exact
        edges for a box zone; an approximation for a hand-drawn polygon, same as
        "center" already was).

        `scale` is the same 1/lod convention used everywhere else in this codebase
        for constant-on-screen-size drawing (e.g. DeviceItem's pens) -- multiplying
        the desired pixel gap by it converts to the scene-unit offset that renders as
        that many pixels at the CURRENT zoom, so the gap neither balloons when zoomed
        out nor disappears when zoomed in.

        Returns (QPointF, valign)."""
        cx = sum(p.x() for p in self.points) / len(self.points)
        ys = [p.y() for p in self.points]
        top, bottom = min(ys), max(ys)
        gap = self.LABEL_EDGE_GAP_PX * scale

        if self.label_position == "above":
            return QPointF(cx, top - gap), "bottom"    # text grows upward, ends `gap` above the line
        elif self.label_position == "below_top":
            return QPointF(cx, top + gap), "top"       # text grows downward, starts `gap` below the line
        elif self.label_position == "above_bottom":
            return QPointF(cx, bottom - gap), "bottom"  # text grows upward, ends `gap` above the line
        elif self.label_position == "below":
            return QPointF(cx, bottom + gap), "top"     # text grows downward, starts `gap` below the line
        else:  # "center", or any unrecognized/legacy value
            return QPointF(cx, (top + bottom) / 2.0), "center"

    def get_area_sqft(self):
        """Polygon area via the shoelace formula, converted to real-world square feet."""
        n = len(self.points)
        if n < 3:
            return None
        area_px = 0.0
        for i in range(n):
            p1 = self.points[i]
            p2 = self.points[(i + 1) % n]
            area_px += p1.x() * p2.y() - p2.x() * p1.y()
        area_px = abs(area_px) / 2.0
        scene = self.scene()
        if scene and hasattr(scene, "scale_ratio") and scene.scale_ratio:
            return area_px / (scene.scale_ratio ** 2)
        return None

    def _vertex_at(self, local_pt):
        hit_radius = self.vertex_handle_radius + 5.0
        for i, pt in enumerate(self.points):
            if distance((local_pt.x(), local_pt.y()), (pt.x(), pt.y())) <= hit_radius:
                return i
        return None

    def insert_vertex_at(self, scene_pt):
        """Inserts a new vertex at the point on the polygon's boundary closest to scene_pt."""
        n = len(self.points)
        if n < 2:
            return
        best_idx, best_dist, best_proj = None, None, None
        for i in range(n):
            a = (self.points[i].x(), self.points[i].y())
            b_pt = self.points[(i + 1) % n]
            b = (b_pt.x(), b_pt.y())
            proj, _t = closest_point_on_segment((scene_pt.x(), scene_pt.y()), a, b)
            d = distance((scene_pt.x(), scene_pt.y()), proj)
            if best_dist is None or d < best_dist:
                best_idx, best_dist, best_proj = i, d, proj
        if best_idx is None:
            return
        new_points = list(self.points)
        new_points.insert(best_idx + 1, QPointF(best_proj[0], best_proj[1]))
        self.set_points(new_points)

    def _find_canvas_view(self):
        scene = self.scene()
        if scene and scene.views():
            return scene.views()[0]
        return None

    # ── Vertex-drag editing ──
    def mouseDoubleClickEvent(self, event):
        canvas = self._find_canvas_view()
        if canvas and getattr(canvas, "active_tool", None) == "select" and event.button() == Qt.LeftButton:
            canvas.set_editing_zone(self)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mousePressEvent(self, event):
        if self.editing_vertices and event.button() == Qt.LeftButton:
            vertex_index = self._vertex_at(event.pos())
            if vertex_index is not None:
                self.dragging_vertex_index = vertex_index
                if self.is_box and len(self.points) == 4:
                    # Opposite corner (index+2, mod 4) stays fixed for the duration of
                    # the drag -- the whole rectangle is re-derived from this anchor and
                    # the live cursor position every move, so it stays a true rectangle
                    # no matter which corner was grabbed or how far the cursor travels.
                    self._box_resize_anchor = QPointF(self.points[(vertex_index + 2) % 4])
                self.setCursor(Qt.ClosedHandCursor)
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.dragging_vertex_index is not None:
            self.prepareGeometryChange()
            if self.is_box and len(self.points) == 4 and self._box_resize_anchor is not None:
                rect = QRectF(self._box_resize_anchor, event.pos()).normalized()
                self.points = [rect.topLeft(), rect.topRight(), rect.bottomRight(), rect.bottomLeft()]
            else:
                self.points[self.dragging_vertex_index] = QPointF(event.pos())
            self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self.dragging_vertex_index is not None:
            self.dragging_vertex_index = None
            self._box_resize_anchor = None
            self.setCursor(Qt.OpenHandCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def hoverMoveEvent(self, event):
        if self.editing_vertices:
            near_vertex = self._vertex_at(event.pos()) is not None
            self.setCursor(Qt.OpenHandCursor if near_vertex else Qt.ArrowCursor)
        super().hoverMoveEvent(event)

    def hoverLeaveEvent(self, event):
        self.unsetCursor()
        super().hoverLeaveEvent(event)

    def paint(self, painter, option, widget=None):
        if len(self.points) < 2:
            return
        lod = option.levelOfDetailFromTransform(painter.worldTransform())
        scale = 1.0 / lod if lod > 0 else 1.0

        polygon = QPolygonF(self.points)
        fill = QColor(self.fill_color)
        fill.setAlphaF(self.fill_opacity)
        border = QColor(self.border_color)
        if self.isSelected() or self.editing_vertices:
            border = QColor("#60a5fa")

        pen = QPen(border, (3.0 if self.isSelected() or self.editing_vertices else 2.0) * scale)
        pen.setStyle(Qt.DashLine if self.editing_vertices else Qt.SolidLine)
        painter.setPen(pen)
        painter.setBrush(QBrush(fill))
        if len(self.points) >= 3:
            painter.drawPolygon(polygon)
        else:
            painter.drawPolyline(polygon)

        if self.editing_vertices:
            handle_radius = self.vertex_handle_radius * scale
            for pt in self.points:
                painter.setPen(QPen(QColor("#ffffff"), 1.5 * scale))
                painter.setBrush(QBrush(QColor("#f59e0b")))
                painter.drawEllipse(pt, handle_radius, handle_radius)

        if self.label and self.points:
            anchor, valign = self.label_anchor_point(scale)
            draw_halo_text(painter, anchor, self.label, scale, font_size=12, valign=valign)
