from PySide6.QtGui import QPen, QColor, QBrush
from PySide6.QtCore import QRectF, QPointF, Qt
from src.graphics.device_item import DeviceItem
from src.graphics.icon_scale import paint_at_icon_scale
from src.graphics.label_render import (draw_item_label, draw_warning_badge,
                                       label_anchor)


class DropItem(DeviceItem):
    """A passive wall drop: the keystone/RJ45 jack where a structured-wiring home run
    terminates (e.g. a wall plate behind a TV). A field cable lands on the back of a
    port here; a short patch cord out the front of that same port is what actually
    reaches the local switch/AP -- identical dual-port-occupancy behavior to a patch
    panel (see DeviceItem.is_pass_through), just sized and drawn like a small wall
    plate instead of a 19" rack strip."""

    def __init__(self, spec, x, y, parent=None):
        super().__init__(spec, x, y, parent)
        self.category = "drop"
        self.port_count = int(spec.get("ports", 1) or 1)

        self.width = max(16.0, 8.0 * self.port_count + 8.0)
        self.height = 14.0

    def paint(self, painter, option, widget=None):
        scene = self.scene()
        if scene and not getattr(scene, "global_show_icons", True):
            return

        lod = option.levelOfDetailFromTransform(painter.worldTransform())
        scale = 1.0 / lod if lod > 0 else 1.0

        accent_color = QColor("#94a3b8")  # Same passive-equipment slate as a patch panel
        if self.isSelected():
            accent_color = QColor("#60a5fa")

        painter.save()
        screen = scale / paint_at_icon_scale(painter, self)
        # `screen` keeps a quantity a constant number of pixels on screen despite
        # the painter scale above -- stroke weights and pixel-sized details use it,
        # while the icon's own geometry uses natural coordinates and grows.
        pen = QPen(accent_color, 1.5 * screen)
        painter.setPen(pen)
        painter.setBrush(QBrush(QColor("#111118")))
        painter.drawRoundedRect(QRectF(-self.design_width / 2, -self.design_height / 2,
                                       self.design_width, self.design_height), 2.0, 2.0)

        # One square keystone glyph per port -- distinct from the patch panel's dense
        # tick-row strip, reads as "wall plate" rather than "rack panel" at a glance.
        gap = 3.0
        keystone_size = min(8.0, (self.design_width - gap * (self.port_count + 1)) / self.port_count)
        total_w = self.port_count * keystone_size + (self.port_count - 1) * gap
        start_x = -total_w / 2.0
        painter.setPen(QPen(accent_color, 1.0 * screen))
        painter.setBrush(QBrush(QColor("#1a1a25")))
        for i in range(self.port_count):
            kx = start_x + i * (keystone_size + gap)
            painter.drawRect(QRectF(kx, -keystone_size / 2, keystone_size, keystone_size))
        painter.restore()

        if self.isSelected():
            painter.save()
            sel_pen = QPen(QColor("#60a5fa"))
            sel_pen.setStyle(Qt.DashLine)
            sel_pen.setWidthF(1.0 * scale)
            painter.setPen(sel_pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(QRectF(-self.width / 2 - 4 * scale, -self.height / 2 - 4 * scale,
                                           self.width + 8 * scale, self.height + 8 * scale), 3.0, 3.0)
            painter.restore()

        draw_item_label(painter, label_anchor(self, self.height / 2 + 4.0), self.label, scale)

        self._update_connectivity_warnings()
        if self._connectivity_warnings:
            badge_pt = QPointF(self.width / 2 - 2.0, -self.height / 2 - 2.0)
            draw_warning_badge(painter, badge_pt, scale)
