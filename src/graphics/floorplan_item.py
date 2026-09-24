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

        # Keep the scaled plan in a device-resolution cache instead of re-sampling the
        # original scan through SmoothPixmapTransform on every repaint. That cost is
        # the whole reason the canvas preferred partial repaints; on a display where
        # partial repaints leave trails (see core/repaint_mode.py) the canvas repaints
        # the full viewport every frame, and without this that would mean re-scaling a
        # multi-megapixel image continuously while dragging.
        self.setCacheMode(QGraphicsPixmapItem.DeviceCoordinateCache)

