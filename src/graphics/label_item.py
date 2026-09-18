import uuid
from PySide6.QtWidgets import QGraphicsObject
from PySide6.QtGui import QPen, QColor, QFont, QFontMetrics, QPainterPath
from PySide6.QtCore import QRectF, QPointF, Qt

from src.graphics.label_render import draw_halo_text


class LabelItem(QGraphicsObject):
    """A free-floating text annotation with a transparent background (a dark outline
    halo keeps it legible over any floorplan color instead of a solid backdrop pill).
    Selectable, movable, deletable, with its own properties (text, size, colors)."""

    def __init__(self, x, y, text="Label", parent=None):
        super().__init__(parent)
        self.object_type = "label"
        self.id = str(uuid.uuid4())

        self.text = text
        self.font_size = 14
        self.text_color = "#ffffff"
        self.outline_color = "#0a0a0f"
        self.notes = ""

        self.setPos(x, y)
        self.setFlags(QGraphicsObject.ItemIsMovable |
                      QGraphicsObject.ItemIsSelectable |
                      QGraphicsObject.ItemSendsGeometryChanges)

    def _metrics(self):
        font = QFont("Inter", self.font_size)
        font.setWeight(QFont.Bold)
        return QFontMetrics(font)

    def _text_size(self):
        fm = self._metrics()
        tw = fm.horizontalAdvance(self.text) if self.text else 10
        th = fm.height()
        return tw, th

    def boundingRect(self):
        tw, th = self._text_size()
        # Generous fixed padding: this text renders at a constant on-screen size
        # (see paint()'s scale-compensation), so its true local footprint shrinks as
        # the view zooms in and grows as it zooms out -- boundingRect can't track that
        # exactly, so it just needs to stay comfortably larger than the text across
        # the app's practical zoom range rather than fit it precisely.
        pad = max(tw, th) * 3 + 150
        return QRectF(-pad, -pad, pad * 2, pad * 2)

    def shape(self):
        tw, th = self._text_size()
        path = QPainterPath()
        path.addRect(QRectF(-tw / 2.0 - 6, -th / 2.0 - 4, tw + 12, th + 8))
        return path

    def paint(self, painter, option, widget=None):
        lod = option.levelOfDetailFromTransform(painter.worldTransform())
        scale = 1.0 / lod if lod > 0 else 1.0

        draw_halo_text(painter, QPointF(0, 0), self.text, scale, self.font_size,
                        self.text_color, self.outline_color)

        if self.isSelected():
            tw, th = self._text_size()
            painter.save()
            sel_pen = QPen(QColor("#60a5fa"))
            sel_pen.setStyle(Qt.DashLine)
            sel_pen.setWidthF(1.0)
            painter.setPen(sel_pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(QRectF(-tw / 2.0 - 8, -th / 2.0 - 6, tw + 16, th + 12))
            painter.restore()

    def itemChange(self, change, value):
        if change == QGraphicsObject.ItemPositionChange and self.scene():
            scene = self.scene()
            if hasattr(scene, "on_item_property_changed"):
                from PySide6.QtCore import QTimer
                QTimer.singleShot(0, lambda: scene.on_item_property_changed(self))
        return super().itemChange(change, value)
