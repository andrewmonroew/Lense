from PySide6.QtWidgets import QGraphicsObject
from PySide6.QtGui import QPen, QColor, QBrush, QFont
from PySide6.QtCore import QRectF, QPointF, Qt, Signal


class DiagramNodeItem(QGraphicsObject):
    """A single box in the Network Diagram tree: an icon (camera/switch/NVR/WAN.png)
    with a title and subtitle underneath. Clicking a node that represents a real
    placed object jumps back to it on the Floor Plan tab (see MainWindow wiring)."""

    clicked = Signal(object)

    BOX_W = 110.0
    ICON_SIZE = 42.0

    def __init__(self, pixmap, title, subtitle="", obj=None, parent=None):
        super().__init__(parent)
        self.pixmap = pixmap
        self.title = title
        self.subtitle = subtitle
        self.obj = obj  # underlying CameraItem/DeviceItem, or None for the imaginary WAN node
        self.is_hovered = False
        self.setAcceptHoverEvents(True)
        if obj is not None:
            self.setCursor(Qt.PointingHandCursor)

    def boundingRect(self):
        return QRectF(-self.BOX_W / 2, -6, self.BOX_W, 92)

    def paint(self, painter, option, widget=None):
        painter.save()

        if self.is_hovered and self.obj is not None:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(QColor(96, 165, 250, 40)))
            painter.drawRoundedRect(self.boundingRect().adjusted(2, 2, -2, -2), 6, 6)

        if self.pixmap and not self.pixmap.isNull():
            scaled = self.pixmap.scaled(int(self.ICON_SIZE), int(self.ICON_SIZE),
                                         Qt.KeepAspectRatio, Qt.SmoothTransformation)
            painter.drawPixmap(QPointF(-scaled.width() / 2.0, 0), scaled)

        title_font = QFont("Inter", 9)
        title_font.setWeight(QFont.DemiBold)
        painter.setFont(title_font)
        painter.setPen(QColor("#f5f5fa"))
        title_rect = QRectF(-self.BOX_W / 2, self.ICON_SIZE + 6, self.BOX_W, 16)
        painter.drawText(title_rect, Qt.AlignHCenter | Qt.AlignTop, self._elide(painter, self.title, self.BOX_W))

        if self.subtitle:
            sub_font = QFont("Inter", 8)
            painter.setFont(sub_font)
            painter.setPen(QColor("#9797ac"))
            sub_rect = QRectF(-self.BOX_W / 2, self.ICON_SIZE + 22, self.BOX_W, 28)
            painter.drawText(sub_rect, Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap, self.subtitle)

        painter.restore()

    def _elide(self, painter, text, width):
        metrics = painter.fontMetrics()
        return metrics.elidedText(text, Qt.ElideRight, int(width))

    def hoverEnterEvent(self, event):
        if self.obj is not None:
            self.is_hovered = True
            self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self.is_hovered = False
        self.update()
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event):
        if self.obj is not None:
            self.clicked.emit(self.obj)
        event.accept()
