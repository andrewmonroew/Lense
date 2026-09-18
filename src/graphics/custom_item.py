import uuid
from PySide6.QtWidgets import QGraphicsObject
from PySide6.QtGui import QPen, QColor, QBrush, QPainterPath
from PySide6.QtCore import QRectF, QPointF, Qt
from src.graphics.icon_scale import natural_size, paint_at_icon_scale, scaled_size
from src.graphics.label_render import (draw_item_label, expand_for_label, label_anchor)

class CustomItem(QGraphicsObject):
    radius = scaled_size("radius")
    rack_width = scaled_size("rack_width")
    rack_height = scaled_size("rack_height")
    design_radius = natural_size("radius")
    design_rack_width = natural_size("rack_width")
    design_rack_height = natural_size("rack_height")

    def __init__(self, x, y, label="Custom Object", icon_type="generic", parent=None):
        super().__init__(parent)
        self.object_type = "custom"
        self.id = str(uuid.uuid4())

        self.label = label
        self.icon_type = icon_type  # "generic" (plain marker) or "rack" (data rack footprint)
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

        self.setPos(x, y)
        self.setFlags(QGraphicsObject.ItemIsMovable |
                      QGraphicsObject.ItemIsSelectable |
                      QGraphicsObject.ItemSendsGeometryChanges)

        self.radius = 12.0
        self.rack_width = 28.0
        self.rack_height = 18.0

    def _footprint_rect(self):
        return QRectF(-self.rack_width / 2, -self.rack_height / 2, self.rack_width, self.rack_height)

    def _design_footprint_rect(self):
        return QRectF(-self.design_rack_width / 2, -self.design_rack_height / 2,
                      self.design_rack_width, self.design_rack_height)

    def boundingRect(self):
        if self.icon_type == "rack":
            w, h = self.rack_width, self.rack_height
            rect = QRectF(-w / 2 - 20, -h / 2 - 20, w + 40, h + 40)
            anchor_y = h / 2 + 4.0
        else:
            r = self.radius + 20
            rect = QRectF(-r, -r, r * 2, r * 2 + 20)
            anchor_y = self.radius + 4.0
        return expand_for_label(self, rect, anchor_y)

    def shape(self):
        path = QPainterPath()
        if self.icon_type == "rack":
            path.addRect(self._footprint_rect())
        else:
            path.addEllipse(QPointF(0, 0), self.radius, self.radius)
        return path

    def paint(self, painter, option, widget=None):
        scene = self.scene()
        if scene and not getattr(scene, "global_show_icons", True):
            return

        lod = option.levelOfDetailFromTransform(painter.worldTransform())
        scale = 1.0 / lod if lod > 0 else 1.0

        is_rack = self.icon_type == "rack"
        accent_color = QColor("#64748b") if is_rack else QColor("#a3a3ad")
        if self.isSelected():
            accent_color = QColor("#60a5fa")

        painter.save()
        screen = scale / paint_at_icon_scale(painter, self)
        # `screen` keeps a quantity a constant number of pixels on screen despite
        # the painter scale above -- stroke weights and pixel-sized details use it,
        # while the icon's own geometry uses natural coordinates and grows.
        pen = QPen(accent_color, 2.0 * screen)
        painter.setPen(pen)
        painter.setBrush(QBrush(QColor("#111118")))

        if is_rack:
            rect = self._design_footprint_rect()
            painter.drawRect(rect)

            # Rack-unit slat lines, evoking a stack of mounted units seen from above
            painter.setPen(QPen(accent_color, 1.0 * screen))
            slots = 4
            for i in range(1, slots):
                y = rect.top() + (rect.height() / slots) * i
                painter.drawLine(QPointF(rect.left() + 2, y), QPointF(rect.right() - 2, y))

            # Corner brackets, evoking mounting rails
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(accent_color))
            corner = 2.5  # natural units, so brackets stay inside the footprint
            for cx, cy in ((rect.left(), rect.top()), (rect.right() - corner, rect.top()),
                           (rect.left(), rect.bottom() - corner), (rect.right() - corner, rect.bottom() - corner)):
                painter.drawRect(QRectF(cx, cy, corner, corner))
        else:
            painter.drawEllipse(QPointF(0, 0), self.design_radius, self.design_radius)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(accent_color))
            painter.drawEllipse(QPointF(0, 0), self.design_radius * 0.35, self.design_radius * 0.35)

        painter.restore()

        if self.isSelected():
            painter.save()
            sel_pen = QPen(QColor("#60a5fa"))
            sel_pen.setStyle(Qt.DashLine)
            sel_pen.setWidthF(1.0 * scale)
            painter.setPen(sel_pen)
            painter.setBrush(Qt.NoBrush)
            if is_rack:
                w, h = self.rack_width, self.rack_height
                painter.drawRect(QRectF(-w / 2 - 4 * scale, -h / 2 - 4 * scale, w + 8 * scale, h + 8 * scale))
            else:
                painter.drawEllipse(QPointF(0, 0), self.radius + 4 * scale, self.radius + 4 * scale)
            painter.restore()

        label_y = (self.rack_height / 2 + 4.0) if is_rack else (self.radius + 4.0)
        draw_item_label(painter, label_anchor(self, label_y), self.label, scale)

    def itemChange(self, change, value):
        if change == QGraphicsObject.ItemPositionChange and self.scene():
            scene = self.scene()
            if hasattr(scene, "on_item_property_changed"):
                from PySide6.QtCore import QTimer
                QTimer.singleShot(0, lambda: scene.on_item_property_changed(self))
        return super().itemChange(change, value)
