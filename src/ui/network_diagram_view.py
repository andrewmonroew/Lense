import os
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGraphicsView, QGraphicsScene,
                             QPushButton, QLabel)
from PySide6.QtGui import QPixmap, QPainter, QPen, QColor, QPainterPath
from PySide6.QtCore import Qt, QPointF, Signal

from src.core.utils import get_data_dir, ZOOM_STEP
from src.graphics.diagram_node_item import DiagramNodeItem

NODE_H_GAP = 24.0    # minimum horizontal gap between sibling subtrees
LEVEL_HEIGHT = 120.0  # vertical distance between a parent's icon row and its children's


class _DiagramGraphicsView(QGraphicsView):
    """Wheel-zoom lives here, not on the QWidget wrapper -- QGraphicsView consumes
    wheel events itself (it's the actual widget under the cursor), so an override on
    an ancestor widget would never fire.

    Cursor-anchoring is handled entirely by transformationAnchor=AnchorUnderMouse (set
    in NetworkDiagramView.__init__) -- a plain scale() is all that's needed. Do NOT
    also add a manual mapToScene()-before/after + translate() correction here: Qt
    re-anchors translate() calls too whenever transformationAnchor isn't NoAnchor,
    which silently neutralizes most of a manual correction (see CanvasView.wheelEvent,
    which needs that manual math and pairs it with NoAnchor for exactly this reason)."""

    def wheelEvent(self, event):
        zoom_factor = ZOOM_STEP if event.angleDelta().y() > 0 else 1.0 / ZOOM_STEP
        self.scale(zoom_factor, zoom_factor)


class NetworkDiagramView(QWidget):
    """Auto-generated network topology diagram: WAN (imaginary -- there's no WAN object
    class yet) at the top, then whatever devices/cameras are actually wired together on
    the canvas, cascading down. Purely derived from cable topology every time it's
    generated, via CanvasView.build_network_topology() -- nothing here is hand-placed,
    so it can never drift out of sync with what the cables actually show."""

    jump_to_object = Signal(object)  # emitted with a CameraItem/DeviceItem when its node is clicked

    def __init__(self, canvas_view, parent=None):
        super().__init__(parent)
        self.canvas_view = canvas_view
        self._icons = self._load_icons()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        toolbar = QHBoxLayout()
        hint = QLabel("Click a node to jump to it on the Floor Plan. Regenerates automatically when you open this tab.")
        hint.setStyleSheet("color: #8888a0; font-size: 11px;")
        hint.setWordWrap(True)
        toolbar.addWidget(hint, 1)

        regen_btn = QPushButton("Regenerate")
        regen_btn.clicked.connect(self.refresh)
        toolbar.addWidget(regen_btn)

        fit_btn = QPushButton("Fit to Window")
        fit_btn.clicked.connect(self.fit_to_window)
        toolbar.addWidget(fit_btn)

        layout.addLayout(toolbar)

        self.scene = QGraphicsScene(self)
        self.scene.setBackgroundBrush(QColor("#141420"))
        self.view = _DiagramGraphicsView(self.scene)
        self.view.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform | QPainter.TextAntialiasing)
        self.view.setDragMode(QGraphicsView.ScrollHandDrag)
        self.view.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        layout.addWidget(self.view, 1)

        self._built = False

    def _load_icons(self):
        icons_dir = os.path.join(get_data_dir(), "icons")
        names = {"camera": "camera.png", "switch": "switch.png", "nvr": "nvr.png", "wan": "wan.png",
                  "access-point": "access-point.png", "misc": "misc.png"}
        icons = {}
        for key, filename in names.items():
            path = os.path.join(icons_dir, filename)
            pixmap = QPixmap(path) if os.path.exists(path) else QPixmap()
            icons[key] = pixmap
        return icons

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()

    # ── Diagram generation ──
    def refresh(self):
        self.scene.clear()
        if self.canvas_view is None:
            return

        roots = self.canvas_view.build_network_topology()
        render_root = self._to_render_tree(roots)

        self._compute_widths(render_root)
        self._position(render_root, 0.0, 0)

        self._place_items(render_root, None)

        rect = self.scene.itemsBoundingRect().adjusted(-60, -60, 60, 60)
        self.scene.setSceneRect(rect)
        self._built = True
        self.fit_to_window()

    def fit_to_window(self):
        rect = self.scene.itemsBoundingRect()
        if not rect.isEmpty():
            self.view.fitInView(rect.adjusted(-40, -40, 40, 40), Qt.KeepAspectRatio)

    # ── Render-tree construction ──
    def _to_render_tree(self, roots):
        children = [self._device_node(r) for r in roots]
        return {"icon": self._icons.get("wan"), "title": "WAN", "subtitle": "Internet Uplink",
                "obj": None, "children": children}

    def _device_node(self, node):
        dev = node["device"]
        # Falls back to the NVR icon for any category without a dedicated one (a
        # graceful degrade, not a category-specific rule -- e.g. covers a future
        # category added before its own icon asset exists).
        icon = self._icons.get(dev.spec.get("category")) or self._icons.get("nvr")
        subtitle = f"{dev.spec.get('manufacturer', '')} {dev.spec.get('model', '')}".strip()

        children = [self._device_node(child) for child in node["children"]]
        for cam in node["cameras"]:
            cam_subtitle = f"{cam.spec.get('manufacturer', '')} {cam.spec.get('model', '')}".strip()
            children.append({"icon": self._icons.get("camera"), "title": cam.label,
                              "subtitle": cam_subtitle, "obj": cam, "children": []})

        return {"icon": icon, "title": dev.label, "subtitle": subtitle, "obj": dev, "children": children}

    # ── Tree layout (subtree-width bottom-up, position top-down; siblings side by
    # side, each level one row further down -- a standard org-chart layout) ──
    def _compute_widths(self, node):
        slot = DiagramNodeItem.BOX_W + NODE_H_GAP
        if not node["children"]:
            node["_width"] = slot
            return slot
        total = sum(self._compute_widths(c) for c in node["children"])
        node["_width"] = max(total, slot)
        return node["_width"]

    def _position(self, node, left_x, depth):
        node["_y"] = depth * LEVEL_HEIGHT
        if not node["children"]:
            node["_x"] = left_x + node["_width"] / 2.0
            return
        cursor = left_x
        for child in node["children"]:
            self._position(child, cursor, depth + 1)
            cursor += child["_width"]
        node["_x"] = (node["children"][0]["_x"] + node["children"][-1]["_x"]) / 2.0

    def _place_items(self, node, parent_item):
        item = DiagramNodeItem(node["icon"], node["title"], node["subtitle"], obj=node["obj"])
        item.setPos(node["_x"], node["_y"])
        item.clicked.connect(self.jump_to_object.emit)
        self.scene.addItem(item)

        if parent_item is not None:
            self._add_connector(parent_item, item)

        for child in node["children"]:
            self._place_items(child, item)

    def _add_connector(self, parent_item, child_item):
        px, py = parent_item.pos().x(), parent_item.pos().y() + DiagramNodeItem.ICON_SIZE / 2.0
        cx, cy = child_item.pos().x(), child_item.pos().y()
        mid_y = (py + cy) / 2.0

        path = QPainterPath(QPointF(px, py))
        path.lineTo(px, mid_y)
        path.lineTo(cx, mid_y)
        path.lineTo(cx, cy)

        line = self.scene.addPath(path, QPen(QColor("#3f3f52"), 1.5))
        line.setZValue(-1)
