from PySide6.QtGui import QPen, QColor, QBrush
from PySide6.QtCore import QRectF, QPointF, Qt
from src.graphics.device_item import DeviceItem
from src.graphics.icon_scale import paint_at_icon_scale
from src.graphics.label_render import (draw_item_label, draw_warning_badge,
                                       label_anchor)


class PatchPanelItem(DeviceItem):
    """A passive patch panel: purely a row of numbered ports, no PoE/routing smarts of
    its own. Field cables (from cameras, or anything else routed into the rack) land on
    a specific port here; a short patch cable drawn from that same port out to a switch
    (or another panel) is what actually completes the connection -- see
    CanvasView.resolve_camera_chain for how that gets walked back into "which switch,
    which port" for the properties panel and PoE/oversubscription warnings.

    Port occupancy is never cached here -- it's derived live from cables' own
    start_port/end_port + start_device_id/end_device_id fields (cables_at_port and
    friends, inherited from DeviceItem -- any mounted device with a clean port count
    gets the same port-grid behavior now, not just patch panels), so there's no
    separate bookkeeping that could drift out of sync with the cables themselves."""

    def __init__(self, spec, x, y, parent=None):
        super().__init__(spec, x, y, parent)
        self.category = "patch-panel"
        self.port_count = int(spec.get("ports", 24) or 24)  # always numeric for a panel

        # Wider/flatter than a switch/NVR box, evoking an actual 19" panel strip.
        self.width = 60.0
        self.height = 14.0

    def paint(self, painter, option, widget=None):
        scene = self.scene()
        if scene and not getattr(scene, "global_show_icons", True):
            return

        lod = option.levelOfDetailFromTransform(painter.worldTransform())
        scale = 1.0 / lod if lod > 0 else 1.0

        accent_color = QColor("#94a3b8")  # Neutral slate -- passive equipment, not active like a switch/NVR
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

        # Port row -- a static tick per port on the floor-plan icon (occupancy detail
        # only really matters in the Rack Editor's elevation, see EquipmentBlockItem).
        visible_ports = min(self.port_count, 24)
        pw = (self.design_width - 6) / visible_ports
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor("#3a3a4d")))
        for i in range(visible_ports):
            px = -self.design_width / 2 + 3 + i * pw
            painter.drawRect(QRectF(px, -self.design_height / 2 + 3, pw - 1, self.design_height - 6))
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
