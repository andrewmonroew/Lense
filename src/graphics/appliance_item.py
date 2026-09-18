from PySide6.QtGui import QPen, QColor, QBrush, QPolygonF, QLinearGradient
from PySide6.QtCore import QRectF, QPointF, Qt
from src.graphics.device_item import DeviceItem
from src.graphics.icon_scale import paint_at_icon_scale
from src.graphics.label_render import (draw_item_label, draw_warning_badge,
                                       label_anchor)


# The one appliance that gets bespoke artwork; see _paint_death_laser.
DEATH_LASER_ID = "generic-misc-death-laser"


class ApplianceItem(DeviceItem):
    """A generic network-enabled appliance -- a TV, smart speaker, streaming box, or
    anything else that isn't purpose-built network gear but still terminates a cable
    (usually plugged straight into a wall drop). One shared icon for every model in
    the catalog's "misc" category rather than one-off art per appliance: a small
    rounded box with a plug glyph, reading as "generic powered/networked device"
    regardless of what it actually is."""

    ACCENT_COLOR = "#fb923c"  # Amber-orange -- distinct from every other category's accent

    def __init__(self, spec, x, y, parent=None):
        super().__init__(spec, x, y, parent)
        self.category = "misc"
        self.port_count = int(spec.get("ports", 1) or 1)

        self.width = 30.0
        self.height = 20.0

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
        # the plug glyph below is literal coordinates
        screen = scale / paint_at_icon_scale(painter, self)
        # `screen` keeps a quantity a constant number of pixels on screen despite
        # the painter scale above -- stroke weights and pixel-sized details use it,
        # while the icon's own geometry uses natural coordinates and grows.
        pen = QPen(accent_color, 2.0 * screen)
        painter.setPen(pen)
        painter.setBrush(QBrush(QColor("#111118")))
        painter.drawRoundedRect(QRectF(-self.design_width / 2, -self.design_height / 2,
                                       self.design_width, self.design_height), 4.0, 4.0)

        if (self.spec or {}).get("id") == DEATH_LASER_ID:
            self._paint_death_laser(painter, screen)
        else:
            # Plug glyph: two prongs over a rounded body, reads as "generic powered device"
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(accent_color))
            painter.drawRect(QRectF(-4.0, -7.0, 2.0, 4.0))
            painter.drawRect(QRectF(2.0, -7.0, 2.0, 4.0))
            painter.drawRoundedRect(QRectF(-6.0, -3.0, 12.0, 7.0), 2.0, 2.0)
        painter.restore()

        if self.isSelected():
            painter.save()
            sel_pen = QPen(QColor("#60a5fa"))
            sel_pen.setStyle(Qt.DashLine)
            sel_pen.setWidthF(1.0 * scale)
            painter.setPen(sel_pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(QRectF(-self.width / 2 - 4 * scale, -self.height / 2 - 4 * scale,
                                           self.width + 8 * scale, self.height + 8 * scale), 5.0, 5.0)
            painter.restore()

        draw_item_label(painter, label_anchor(self, self.height / 2 + 2.0), self.label, scale)

        self._update_connectivity_warnings()
        if self._connectivity_warnings:
            badge_pt = QPointF(self.width / 2 - 2.0, -self.height / 2 - 2.0)
            draw_warning_badge(painter, badge_pt, scale)

    def _paint_death_laser(self, painter, screen):
        """An emitter dish charging a beam, because it is called a Super Mega Death Laser
        and a plug glyph would not do it justice.

        Drawn in the same natural coordinates as every other appliance glyph and using
        `screen` for stroke weights, so it obeys the icon-scale and zoom rules like
        anything else -- the joke is in the artwork, not in the plumbing.
        """
        # Charging core: concentric rings, hottest in the middle.
        painter.setPen(Qt.NoPen)
        for radius, colour in ((6.2, "#7f1d1d"), (4.6, "#dc2626"),
                               (3.0, "#f97316"), (1.6, "#fef08a")):
            painter.setBrush(QBrush(QColor(colour)))
            painter.drawEllipse(QPointF(-2.0, 0.0), radius, radius)

        # Emitter barrel, tapering to the muzzle.
        barrel = QPolygonF([QPointF(1.0, -3.4), QPointF(11.0, -1.7),
                            QPointF(11.0, 1.7), QPointF(1.0, 3.4)])
        painter.setBrush(QBrush(QColor("#cbd5e1")))
        painter.setPen(QPen(QColor("#0f172a"), 0.8 * screen))
        painter.drawPolygon(barrel)

        # The beam itself, leaving frame with intent.
        beam = QLinearGradient(11.0, 0.0, 15.0, 0.0)
        beam.setColorAt(0.0, QColor(255, 255, 255, 235))
        beam.setColorAt(1.0, QColor(248, 113, 113, 0))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(beam))
        painter.drawPolygon(QPolygonF([QPointF(11.0, -1.4), QPointF(15.0, -2.6),
                                       QPointF(15.0, 2.6), QPointF(11.0, 1.4)]))

        # Hazard chevrons on the housing, and a warning notch on top.
        painter.setBrush(QBrush(QColor("#fbbf24")))
        for x in (-11.5, -9.0, -6.5):
            painter.drawPolygon(QPolygonF([QPointF(x, 4.2), QPointF(x + 1.6, 4.2),
                                           QPointF(x + 0.6, 7.0), QPointF(x - 1.0, 7.0)]))
        painter.setPen(QPen(QColor("#fbbf24"), 1.0 * screen))
        painter.setBrush(Qt.NoBrush)
        painter.drawLine(QPointF(-2.0, -8.6), QPointF(-2.0, -6.4))
