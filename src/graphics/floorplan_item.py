from PySide6.QtWidgets import QGraphicsPixmapItem
from PySide6.QtGui import QPixmap
from PySide6.QtCore import Qt

class FloorPlanItem(QGraphicsPixmapItem):
    def __init__(self, pixmap=None, parent=None):
        super().__init__(parent)
        if pixmap:
            self.setPixmap(pixmap)
        
        # Make it click-through or non-selectable depending on tool
        self.setZValue(-100)
        self.setAcceptedMouseButtons(Qt.NoButton) # Click-through

