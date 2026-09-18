import math
import os
import time
import uuid
from PySide6.QtWidgets import QGraphicsObject, QStyleOptionGraphicsItem
from PySide6.QtGui import QPen, QColor, QBrush, QPainterPath, QPixmap
from PySide6.QtCore import QRectF, QPointF, Qt, QLineF
from src.core.utils import rad_to_deg, deg_to_rad, distance, compute_ground_range, get_ir_range_feet, get_data_dir
from src.graphics.icon_scale import scaled_size
from src.graphics.label_render import (draw_item_label, draw_warning_badge,
                                       expand_for_label, label_anchor)

def rgba(r, g, b, a):
    """Builds a QColor from 0-255 RGB + 0-1 alpha. QColor's string constructor does not understand CSS rgba() syntax."""
    color = QColor(r, g, b)
    color.setAlphaF(a)
    return color

class CameraItem(QGraphicsObject):
    # The icon glyph scales with the canvas icon scale; the FOV wedge and IR ring
    # below deliberately do not -- those are real distances in feet, not decoration.
    icon_radius = scaled_size("icon_radius")
    handle_distance = scaled_size("handle_distance")
    handle_radius = scaled_size("handle_radius")

    def __init__(self, spec, x, y, parent=None):
        super().__init__(parent)
        self.object_type = "camera"
        self.id = str(uuid.uuid4())
        self.spec = spec
        
        # State
        self.label = spec.get("model", "Camera")
        self.elevation = 10.0       # ft
        self.rotation_deg = 0.0     # Pan: degrees (0 = right, clockwise)
        self.downtilt_deg = 20.0    # Tilt: degrees below horizontal (0 = level, 90 = straight down)
        self.show_fov = True
        self.show_ir = True
        self.fov_opacity = 0.8
        self.notes = ""
        # Cable allowances for runs landing here, in feet. None means "follow the
        # default for my kind" (Settings > Cabling). See core/cable_length.py.
        self.vertical_rise = None
        self.service_loop = None
        # How runs land here: None follows the default for this kind of object
        # (Settings > Cabling), "rj45" or "jack" pins it. See core/termination.py.
        self.termination = None
        # Rows this item's label is pushed down to avoid colliding with a neighbour's.
        # Assigned by graphics/label_layout.py; 0 means sitting at its natural spot.
        self.label_row = 0
        self.connected_device_id = None
        self.connected_cable_id = None

        # Position
        self.setPos(x, y)
        self.setFlags(QGraphicsObject.ItemIsMovable |
                      QGraphicsObject.ItemIsSelectable |
                      QGraphicsObject.ItemSendsGeometryChanges)

        # Handle size / selection flags
        self.icon_radius = 12.0
        self.handle_distance = 32.0 # Distance of rotation handle from center
        self.handle_radius = 4.0
        self.is_rotating = False

        # Hovering or selecting a camera shows its FOV even with the Visibility bar's
        # FOV toggle off -- otherwise there'd be no way to see what you're adjusting
        # while dialing in one camera's orientation with every other camera decluttered.
        self.is_hovered = False
        self.setAcceptHoverEvents(True)
        self._connectivity_warning = None
        self._warnings_checked_at = None  # see _update_connectivity_warning

    def boundingRect(self):
        # We need a large bounding rect to cover the FOV and IR range
        # Find maximum range in pixels
        # No fabricated fallback: a camera with no established range (e.g. an explicit
        # null irRange for color-only night vision with no IR illuminator) shows no
        # coverage wedge at all rather than a misleading made-up distance.
        ir_feet = get_ir_range_feet(self.spec, 0.0)
        
        # Ask scene/view for scale, fallback to 2 pixels/foot
        scale_ratio = 2.0
        scene = self.scene()
        if scene and hasattr(scene, "scale_ratio") and scene.scale_ratio:
            scale_ratio = scene.scale_ratio

        max_r = ir_feet * scale_ratio
        # Add padding for rotation handle and selection outline. The icon floor is
        # measured from the scaled icon rather than a fixed 50, or a camera turned up
        # to a large icon scale would paint outside its own bounding rect and leave
        # trails behind it when moved.
        icon_extent = max(self.icon_radius, self.handle_distance + self.handle_radius)
        r = max(max_r, icon_extent + 30.0) + 20
        rect = QRectF(-r, -r, r * 2, r * 2)
        return expand_for_label(self, rect, self.icon_radius + 4.0)

    def shape(self):
        # Shape is used for precise collision / selection.
        # We only want the camera icon itself (not the FOV wedge) to trigger click selection
        path = QPainterPath()
        path.addEllipse(QPointF(0, 0), self.icon_radius, self.icon_radius)
        # Add rotation handle area to shape so it is clickable
        path.addEllipse(QPointF(self.handle_distance, 0), self.handle_radius + 4, self.handle_radius + 4)
        return path

    _topology_icon = None  # lazy-loaded, shared by every CameraItem instance

    @classmethod
    def _topology_pixmap(cls):
        if cls._topology_icon is None:
            path = os.path.join(get_data_dir(), "icons", "camera.png")
            cls._topology_icon = QPixmap(path) if os.path.exists(path) else QPixmap()
        return cls._topology_icon

    def _is_topology_mode(self):
        view = self._canvas_view()
        return getattr(view, "project_type", "cctv") == "network_topology"

    def paint(self, painter, option, widget=None):
        lod = option.levelOfDetailFromTransform(painter.worldTransform())
        scale = 1.0 / lod if lod > 0 else 1.0

        if self._is_topology_mode():
            self._paint_topology_icon(painter, scale)
            return

        scale_ratio = 2.0
        scene = self.scene()
        if scene and hasattr(scene, "scale_ratio") and scene.scale_ratio:
            scale_ratio = scene.scale_ratio

        ir_feet = get_ir_range_feet(self.spec, 0.0)  # see boundingRect() re: no fabricated fallback
        vfov_deg = self.spec.get("fov", {}).get("vertical", 50.0)

        # Elevation + downtilt + vertical FOV determine where the view cone actually
        # meets the ground: a near (blind-spot) radius and a far radius, both possibly
        # tighter than the camera's flat rated IR range.
        near_ft, far_ft = compute_ground_range(self.elevation, self.downtilt_deg, vfov_deg, ir_feet)
        near_px = near_ft * scale_ratio
        far_px = far_ft * scale_ratio

        # Master visibility toggles (the Visibility bar) layer on top of each camera's
        # own per-item settings above -- except a selected or hovered camera always
        # shows its FOV regardless, so you can still see (and adjust) one camera's
        # coverage while every other camera stays decluttered with FOV off globally.
        global_show_fov = getattr(scene, "global_show_fov", True) if scene else True
        global_show_icons = getattr(scene, "global_show_icons", True) if scene else True
        effective_show_fov = global_show_fov or self.isSelected() or self.is_hovered

        # 1. Draw FOV Wedges
        if self.show_fov and effective_show_fov and scale_ratio > 0.0:
            self._draw_fov_wedges(painter, near_px, far_px)

        # 2. Draw IR Range Ring
        if self.show_ir and effective_show_fov and scale_ratio > 0.0:
            self._draw_ir_ring(painter, near_px, far_px, scale)

        if not global_show_icons:
            return

        # 3. Draw Camera Base Icon (Centered at 0, 0)
        painter.save()
        # Rotate painter to match our orientation
        painter.rotate(self.rotation_deg)

        # Outer ring
        pen = QPen(QColor("#22c55e"))
        if self.isSelected():
            pen.setColor(QColor("#60a5fa"))
        pen.setWidthF(2.0 * scale)
        painter.setPen(pen)
        painter.setBrush(QBrush(QColor("#111118")))
        painter.drawEllipse(QPointF(0, 0), self.icon_radius, self.icon_radius)

        # Inner Lens / Core
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor("#22c55e") if not self.isSelected() else QColor("#60a5fa")))
        painter.drawEllipse(QPointF(0, 0), self.icon_radius * 0.4, self.icon_radius * 0.4)

        # Aim indicator lens line
        painter.setPen(pen)
        painter.drawLine(0, 0, self.icon_radius * 1.5, 0)

        # 4. Draw Rotation Handle (dotted line + dot)
        if self.isSelected():
            handle_pen = QPen(QColor("#60a5fa"))
            handle_pen.setStyle(Qt.DotLine)
            handle_pen.setWidthF(1.0 * scale)
            painter.setPen(handle_pen)
            painter.drawLine(self.icon_radius, 0, self.handle_distance, 0)

            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(QColor("#60a5fa")))
            painter.drawEllipse(QPointF(self.handle_distance, 0), self.handle_radius * scale, self.handle_radius * scale)

        painter.restore()

        # 5. Draw Label
        draw_item_label(painter, label_anchor(self, self.icon_radius + 4.0), self.label, scale)

        # 6. Selection Highlight
        if self.isSelected():
            painter.save()
            sel_pen = QPen(QColor("#60a5fa"))
            sel_pen.setStyle(Qt.DashLine)
            sel_pen.setWidthF(1.0 * scale)
            painter.setPen(sel_pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(QPointF(0, 0), self.icon_radius + 4 * scale, self.icon_radius + 4 * scale)
            painter.restore()

        # 7. Connectivity warning badge -- a camera not wired to any switch/NVR is
        # easy to lose track of on a real design with dozens of cameras, so flag it
        # right on the icon rather than only in the properties panel.
        self._update_connectivity_warning()
        if self._connectivity_warning:
            badge_pt = QPointF(-self.icon_radius * 0.75, -self.icon_radius * 0.75)
            draw_warning_badge(painter, badge_pt, scale)

    def _paint_topology_icon(self, painter, scale):
        """Network Topology mode: a camera is just another node in the diagram, not a
        physically-aimed device -- no FOV cone/IR ring/rotation handle to simulate,
        just the same flat camera.png used in the Network Diagram tab, so a design
        reads consistently between the two."""
        scene = self.scene()
        if not getattr(scene, "global_show_icons", True):
            return

        pixmap = self._topology_pixmap()
        painter.save()
        if not pixmap.isNull():
            size = self.icon_radius * 2.0
            target = QRectF(-size / 2.0, -size / 2.0, size, size)
            painter.drawPixmap(target, pixmap, QRectF(pixmap.rect()))
        else:
            # Fallback if the icon asset is ever missing -- a plain filled circle
            # beats a blank, invisible camera.
            painter.setPen(QPen(QColor("#22c55e"), 2.0 * scale))
            painter.setBrush(QBrush(QColor("#111118")))
            painter.drawEllipse(QPointF(0, 0), self.icon_radius, self.icon_radius)
        painter.restore()

        draw_item_label(painter, label_anchor(self, self.icon_radius + 4.0), self.label, scale)

        if self.isSelected():
            painter.save()
            sel_pen = QPen(QColor("#60a5fa"))
            sel_pen.setStyle(Qt.DashLine)
            sel_pen.setWidthF(1.0 * scale)
            painter.setPen(sel_pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(QPointF(0, 0), self.icon_radius + 4 * scale, self.icon_radius + 4 * scale)
            painter.restore()

        self._update_connectivity_warning()
        if self._connectivity_warning:
            badge_pt = QPointF(-self.icon_radius * 0.75, -self.icon_radius * 0.75)
            draw_warning_badge(painter, badge_pt, scale)

    def _canvas_view(self):
        scene = self.scene()
        if scene:
            views = scene.views()
            if views:
                return views[0]
        return None

    WARNING_REFRESH_SECONDS = 0.25

    def invalidate_connectivity_warnings(self):
        self._warnings_checked_at = None

    def _update_connectivity_warning(self):
        if getattr(self, "is_preview", False):
            return  # the ghost trailing the cursor is not part of the design yet
        # Throttled for the same reason as DeviceItem's -- see the note there.
        now = time.monotonic()
        if (self._warnings_checked_at is not None
                and now - self._warnings_checked_at < self.WARNING_REFRESH_SECONDS):
            return
        self._warnings_checked_at = now
        view = self._canvas_view()
        warning = view.get_camera_connectivity_warning(self) if view is not None and hasattr(view, "get_camera_connectivity_warning") else None
        self._connectivity_warning = warning
        self.setToolTip(warning or "")

    def _annulus_sector_path(self, inner_r, outer_r, start_angle_deg, sweep_deg):
        """Builds a wedge between inner_r and outer_r (a pie slice if inner_r is ~0,
        a ring-shaped band otherwise) spanning start_angle_deg..start_angle_deg+sweep_deg."""
        path = QPainterPath()
        if outer_r <= 0:
            return path
        inner_r = max(0.0, min(inner_r, outer_r))

        if inner_r <= 0.01:
            path.moveTo(0, 0)
            path.arcTo(QRectF(-outer_r, -outer_r, outer_r * 2, outer_r * 2), start_angle_deg, sweep_deg)
            path.closeSubpath()
            return path

        end_angle_deg = start_angle_deg + sweep_deg
        inner_rect = QRectF(-inner_r, -inner_r, inner_r * 2, inner_r * 2)
        outer_rect = QRectF(-outer_r, -outer_r, outer_r * 2, outer_r * 2)

        path.arcMoveTo(inner_rect, start_angle_deg)
        path.arcTo(inner_rect, start_angle_deg, sweep_deg)          # inner edge, start -> end
        path.arcTo(outer_rect, end_angle_deg, -sweep_deg)           # cross to outer edge, sweep back end -> start
        path.closeSubpath()                                        # cross back to inner edge start
        return path

    def _draw_fov_wedges(self, painter, near_px, far_px):
        fov_deg = self.spec.get("fov", {}).get("horizontal", 90.0)
        half_fov = fov_deg / 2.0

        # arcTo parameters: QRectF of bounding ellipse, startAngle, sweepLength.
        # QPainter degrees are counter-clockwise! Our self.rotation_deg is clockwise,
        # and QPainter's 0 degrees is right (3 o'clock), so negate to convert.
        start_angle = -(self.rotation_deg + half_fov)
        sweep_angle = fov_deg

        # The three ID/Recognize/Detect zones are fractions of the ground-visible span
        # (near_px..far_px), not of the raw IR range -- elevation/downtilt/vertical FOV
        # may have already clipped how much of that range actually falls on the ground.
        span = far_px - near_px
        zones = [
            {"max": 1.0, "fill": rgba(239, 68, 68, 0.15), "stroke": rgba(239, 68, 68, 0.4)},  # Outer (Detect) - Red
            {"max": 0.6, "fill": rgba(245, 158, 11, 0.22), "stroke": rgba(245, 158, 11, 0.5)}, # Mid (Recog) - Amber
            {"max": 0.3, "fill": rgba(34, 197, 94, 0.30), "stroke": rgba(34, 197, 94, 0.6)}    # Inner (ID) - Green
        ]

        for zone in zones:
            outer_r = near_px + span * zone["max"]
            path = self._annulus_sector_path(near_px, outer_r, start_angle, sweep_angle)

            # Apply overall FOV opacity on top of the zone's own alpha
            fill_color = QColor(zone["fill"])
            fill_color.setAlphaF(zone["fill"].alphaF() * self.fov_opacity)
            stroke_color = QColor(zone["stroke"])
            stroke_color.setAlphaF(zone["stroke"].alphaF() * self.fov_opacity)

            painter.setPen(QPen(stroke_color, 1.0, Qt.SolidLine))
            painter.setBrush(QBrush(fill_color))
            painter.drawPath(path)

    def _draw_ir_ring(self, painter, near_px, far_px, scale):
        pen = QPen(rgba(139, 92, 246, 0.35))  # Violet, distinct from the red/amber/green FOV zones
        pen.setStyle(Qt.DashLine)
        pen.setWidthF(1.5 * scale)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(QPointF(0, 0), far_px, far_px)

        # Blind-spot boundary: only worth showing if downtilt/elevation actually carve one out
        if near_px > 1.0:
            blind_pen = QPen(rgba(239, 68, 68, 0.45))
            blind_pen.setStyle(Qt.DashLine)
            blind_pen.setWidthF(1.0 * scale)
            painter.setPen(blind_pen)
            painter.drawEllipse(QPointF(0, 0), near_px, near_px)

    # ── Interaction & Rotation Handle ──
    def mousePressEvent(self, event):
        if self._is_topology_mode():
            # No FOV cone to aim in this mode, so no rotation handle is drawn --
            # don't let a click in that dead zone silently start a no-op rotate drag.
            super().mousePressEvent(event)
            return

        # Check if clicked rotation handle
        # Handle position in local space
        pos = event.pos()
        handle_pos = QPointF(self.handle_distance, 0)
        
        # Adjust for rotation
        rotated_handle = QPointF(
            self.handle_distance * math.cos(deg_to_rad(self.rotation_deg)),
            self.handle_distance * math.sin(deg_to_rad(self.rotation_deg))
        )
        
        if distance((pos.x(), pos.y()), (rotated_handle.x(), rotated_handle.y())) <= self.handle_radius + 6:
            self.is_rotating = True
            event.accept()
            # Set cursor to grabbing
            self.setCursor(Qt.ClosedHandCursor)
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.is_rotating:
            # Calculate rotation angle based on mouse position relative to camera center (0,0)
            # In scene space or parent space?
            pos = event.pos() # local coord
            # Angle relative to (0,0)
            angle = math.atan2(pos.y(), pos.x())
            self.rotation_deg = rad_to_deg(angle) % 360
            self.update()
            
            # Notify sidebar
            scene = self.scene()
            if scene and hasattr(scene, "on_item_property_changed"):
                scene.on_item_property_changed(self)
                
            event.accept()
        else:
            super().mouseMoveEvent(event)
            # Notify coordinates changed
            scene = self.scene()
            if scene and hasattr(scene, "on_item_property_changed"):
                scene.on_item_property_changed(self)

    def mouseReleaseEvent(self, event):
        if self.is_rotating:
            self.is_rotating = False
            self.setCursor(Qt.ArrowCursor)
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def hoverEnterEvent(self, event):
        self.is_hovered = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self.is_hovered = False
        self.update()
        super().hoverLeaveEvent(event)

    def itemChange(self, change, value):
        if change == QGraphicsObject.ItemPositionChange and self.scene():
            # Notify positions changed
            scene = self.scene()
            if hasattr(scene, "on_item_property_changed"):
                # Call later to make sure value is set
                from PySide6.QtCore import QTimer
                QTimer.singleShot(0, lambda: scene.on_item_property_changed(self))
        return super().itemChange(change, value)
