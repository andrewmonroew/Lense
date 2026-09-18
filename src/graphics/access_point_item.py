from PySide6.QtGui import QPen, QColor, QBrush
from PySide6.QtCore import QRectF, QPointF, Qt
from src.graphics.device_item import DeviceItem
from src.graphics.icon_scale import paint_at_icon_scale
from src.graphics.label_render import (draw_item_label, draw_warning_badge,
                                       label_anchor)


class AccessPointItem(DeviceItem):
    """A PoE-powered wireless access point: a single uplink jack (port 1), drawn as a
    circular puck with a fanning Wi-Fi glyph instead of the switch/NVR rounded box, so
    it reads as a distinct device type on the canvas and in the Network Diagram rather
    than falling back to DeviceItem's generic switch/NVR rendering."""

    ACCENT_COLOR = "#2dd4bf"  # Teal -- distinct from switch-blue/NVR-purple/passive-slate

    def __init__(self, spec, x, y, parent=None):
        super().__init__(spec, x, y, parent)
        self.category = "access-point"
        self.port_count = int(spec.get("ports", 1) or 1)

        self.width = 20.0
        self.height = 20.0
        self.radius = self.width / 2.0

    def paint(self, painter, option, widget=None):
        scene = self.scene()
        if scene and not getattr(scene, "global_show_icons", True):
            return

        lod = option.levelOfDetailFromTransform(painter.worldTransform())
        scale = 1.0 / lod if lod > 0 else 1.0

        accent_color = QColor(self.ACCENT_COLOR)
        if self.isSelected():
            accent_color = QColor("#60a5fa")

        painter.save()
        # the Wi-Fi arcs below are literal radii
        screen = scale / paint_at_icon_scale(painter, self)
        # `screen` keeps a quantity a constant number of pixels on screen despite
        # the painter scale above -- stroke weights and pixel-sized details use it,
        # while the icon's own geometry uses natural coordinates and grows.
        pen = QPen(accent_color, 2.0 * screen)
        painter.setPen(pen)
        painter.setBrush(QBrush(QColor("#111118")))
        painter.drawEllipse(QPointF(0, 0), self.design_radius, self.design_radius)

        # Wi-Fi glyph: three fanning arcs above a center dot.
        painter.setPen(QPen(accent_color, 1.5 * screen))
        painter.setBrush(Qt.NoBrush)
        for i, r in enumerate((3.0, 6.0, 9.0)):
            painter.drawArc(QRectF(-r, -r + 3.0, r * 2, r * 2), 45 * 16, 90 * 16)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(accent_color))
        painter.drawEllipse(QPointF(0, 3.0), 1.6, 1.6)
        painter.restore()

        if self.isSelected():
            painter.save()
            sel_pen = QPen(QColor("#60a5fa"))
            sel_pen.setStyle(Qt.DashLine)
            sel_pen.setWidthF(1.0 * scale)
            painter.setPen(sel_pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(QPointF(0, 0), self.radius + 4 * scale, self.radius + 4 * scale)
            painter.restore()

        draw_item_label(painter, label_anchor(self, self.height / 2 + 2.0), self.label, scale)

        self._update_connectivity_warnings()
        if self._connectivity_warnings:
            badge_pt = QPointF(self.width / 2 - 2.0, -self.height / 2 - 2.0)
            draw_warning_badge(painter, badge_pt, scale)
