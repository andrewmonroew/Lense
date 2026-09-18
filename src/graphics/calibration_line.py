from PySide6.QtWidgets import QGraphicsItem
from PySide6.QtGui import QPen, QColor, QBrush, QFont
from PySide6.QtCore import QRectF, QPointF, Qt
from src.core.utils import distance

class CalibrationLine(QGraphicsItem):
    def __init__(self, p1=None, p2=None, parent=None):
        super().__init__(parent)
        self.p1 = p1 or QPointF(0, 0)
        self.p2 = p2 or QPointF(0, 0)
        self.setZValue(50)  # Render near top

    def set_points(self, p1, p2):
        self.prepareGeometryChange()
        self.p1 = p1
        self.p2 = p2
        self.update()

    def boundingRect(self):
        # Return bounding box containing p1 and p2 with some padding
        x1, x2 = min(self.p1.x(), self.p2.x()), max(self.p1.x(), self.p2.x())
        y1, y2 = min(self.p1.y(), self.p2.y()), max(self.p1.y(), self.p2.y())
        padding = 30
        return QRectF(x1 - padding, y1 - padding, (x2 - x1) + padding * 2, (y2 - y1) + padding * 2)

    def paint(self, painter, option, widget=None):
        # Calculate size based on lod (level of detail) to look crisp at any zoom
        lod = option.levelOfDetailFromTransform(painter.worldTransform())
        scale = 1.0 / lod if lod > 0 else 1.0

        # Draw dotted line
        pen = QPen(QColor("#f59e0b"))
        pen.setWidthF(2.0 * scale)
        pen.setStyle(Qt.DashLine)
        painter.setPen(pen)
        painter.drawLine(self.p1, self.p2)

        # Draw circle nodes at ends
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor("#f59e0b")))
        circle_radius = 6.0 * scale
        painter.drawEllipse(self.p1, circle_radius, circle_radius)
        painter.drawEllipse(self.p2, circle_radius, circle_radius)

        # Draw ticks/crosses
        # Draw text label showing pixel distance or just generic guide
        px_dist = distance(self.p1, self.p2)
        label = f"Calibrating: {px_dist:.0f} px"
        
        font = QFont("Inter", 11)
        font.setBold(True)
        painter.setFont(font)
        
        # Center of line
        mx = (self.p1.x() + self.p2.x()) / 2.0
        my = (self.p1.y() + self.p2.y()) / 2.0
        
        # Shift text slightly up
        painter.setPen(QColor("#f59e0b"))
        # Render backdrop shadow for readability
        painter.drawText(QPointF(mx * lod - 60, my * lod - 10), label)
