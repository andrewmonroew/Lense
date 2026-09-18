from PySide6.QtWidgets import QGraphicsView, QGraphicsScene, QGraphicsPathItem, QMenu
from PySide6.QtGui import QPainter, QPen, QColor, QPainterPath
from PySide6.QtCore import Qt, QPointF, QRectF

from src.core.utils import CATALOG_SPEC_MIME_TYPE, ZOOM_STEP
from src.graphics.rack_elevation_items import (RackFrameItem, EquipmentBlockItem, CableStubItem,
                                                PatchCableItem, RU_PIXEL_HEIGHT, FRAME_WIDTH, ru_span_to_rect)

STUB_COLUMN_X = FRAME_WIDTH + 70.0
STUB_MARGIN_TOP = 20.0
STUB_SPACING = 20.0


class RackElevationView(QGraphicsView):
    """The graphical rack: two rails with bolt holes, mounted equipment (draggable to
    reorder), and every field cable that terminates here entering from the right edge
    as a bundle that forks -- pitchfork-style -- into individual strands, each either
    still dangling (unterminated) or landing on a specific port/device.

    Two tool modes, toggled by the panel's toolbar:
    - "select" (default): drag equipment to reorder; drag one cable stub (or a rubber-
      band-selected group of them) onto a port/device to terminate them there.
    - "patch": click two ports/devices in turn to lay a new patch cable between them.
    """

    def __init__(self, panel, parent=None):
        super().__init__(parent)
        self.panel = panel
        self.scene_obj = QGraphicsScene(self)
        self.scene_obj.setBackgroundBrush(QColor("#141420"))
        self.setScene(self.scene_obj)
        self.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
        self.setDragMode(QGraphicsView.RubberBandDrag)
        self.setAcceptDrops(True)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        # Matches CanvasView -- MinimalViewportUpdate (the default) can miscalculate
        # the dirty region for a fast item drag over this view's custom-painted
        # background (bolt holes, port grids, the trunk/patch-cable paths), leaving a
        # visible trail behind the dragged block instead of a clean repaint.
        self.setViewportUpdateMode(QGraphicsView.FullViewportUpdate)
        # Keyboard focus needs to actually live here while this tab is open -- see
        # keyPressEvent below for why.
        self.setFocusPolicy(Qt.StrongFocus)

        self.frame_item = None
        self._stub_items = []      # list of CableStubItem
        self._block_items = []     # list of EquipmentBlockItem
        self._trunk_path_item = None
        self._patch_path_items = []

        self._drag_candidates = []  # CableStubItems part of the in-progress drag
        self.pan_active = False
        self.last_pan_pos = QPointF()

    def showEvent(self, event):
        super().showEvent(event)
        self.setFocus()

    def keyPressEvent(self, event):
        # Delete/Backspace is intentionally never allowed to fall through to whatever's
        # underneath this view -- if it somehow ends up not focused (a stray click
        # landing back on the Canvas tab's widget, which is hidden but not destroyed
        # while this tab is open), an unhandled Delete could otherwise bubble up and
        # hit CanvasView's own keyPressEvent, silently deleting whatever's still
        # selected there. Everything selectable in this view (patch cables, mounted
        # devices, landed cable stubs) has its own delete/return gesture handled
        # locally here, and that's the end of it -- never propagates further.
        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            selected = self.scene_obj.selectedItems()
            selected_patch_cables = [i for i in selected if isinstance(i, PatchCableItem)]
            if selected_patch_cables:
                self.panel.delete_patch_cables(selected_patch_cables)
            selected_blocks = [i for i in selected if isinstance(i, EquipmentBlockItem)]
            if selected_blocks:
                self.panel.delete_devices([b.slot for b in selected_blocks])
            selected_stubs = [i for i in selected if isinstance(i, CableStubItem)]
            if selected_stubs:
                self.panel.return_stubs_to_pitchfork(selected_stubs)
            event.accept()
            return
        if event.key() == Qt.Key_H and not event.isAutoRepeat():
            # Hold H to "peek under the cables" -- every patch cable goes semi-
            # transparent and click-through (same treatment as actively drawing a new
            # one) for as long as it's held, so a port hidden under one can be seen
            # and clicked without having to start a whole new patch draw to reveal it.
            self.panel.ghost_mode = True
            self.scene_obj.update()
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.key() == Qt.Key_H and not event.isAutoRepeat():
            self.panel.ghost_mode = False
            self.scene_obj.update()
            event.accept()
            return
        super().keyReleaseEvent(event)

    def focusOutEvent(self, event):
        # Don't let ghost mode get stuck on if focus leaves this view (alt-tab, a
        # dialog popping up, ...) while H is still physically held -- no keyRelease
        # will ever arrive in that case.
        if self.panel.ghost_mode:
            self.panel.ghost_mode = False
            self.scene_obj.update()
        super().focusOutEvent(event)

    # ── Catalog spec drop-to-mount ──
    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat(CATALOG_SPEC_MIME_TYPE):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasFormat(CATALOG_SPEC_MIME_TYPE):
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        mime = event.mimeData()
        if not mime.hasFormat(CATALOG_SPEC_MIME_TYPE):
            super().dropEvent(event)
            return
        spec_id = bytes(mime.data(CATALOG_SPEC_MIME_TYPE)).decode("utf-8")
        scene_pt = self.mapToScene(event.position().toPoint())
        preferred_ru = None
        if self.frame_item is not None:
            local = self.frame_item.mapFromScene(scene_pt)
            if 0 <= local.y() <= self.frame_item.ru_height * RU_PIXEL_HEIGHT:
                from src.graphics.rack_elevation_items import y_to_ru
                preferred_ru = y_to_ru(local.y(), self.frame_item.ru_height)
        self.panel.mount_new_device(spec_id, preferred_start_ru=preferred_ru)
        event.acceptProposedAction()

    # ── Stub drag tracking (view-level so a rubber-band multi-selection moves and
    # terminates as one group, not just whatever single item happened to be clicked) ──
    def mousePressEvent(self, event):
        if event.button() == Qt.MiddleButton:
            self.pan_active = True
            self.last_pan_pos = event.pos()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return

        # Always start from a clean slate: a press that never saw its matching release
        # (interrupted drag, a dialog stealing the event) would otherwise leave stale
        # candidates behind, and the panel's refresh guard keys off exactly that.
        self._drag_candidates = []
        item = self.itemAt(event.pos())
        if isinstance(item, CableStubItem) and self.panel.tool_mode == "select":
            selected_stubs = [s for s in self.scene_obj.selectedItems() if isinstance(s, CableStubItem)]
            self._drag_candidates = selected_stubs if item in selected_stubs and len(selected_stubs) > 1 else [item]
        else:
            self._drag_candidates = []
        super().mousePressEvent(event)

        # Clicking a mounted device should populate the Properties panel with that
        # device -- previously the panel kept showing the rack, which is the one thing
        # you already have open, and left all that space saying nothing useful.
        block = item if isinstance(item, EquipmentBlockItem) else None
        if block is None and isinstance(item, CableStubItem):
            block = None
        if block is not None:
            self.panel.device_selected.emit(block.slot.device)

    def mouseMoveEvent(self, event):
        if self.pan_active:
            delta = event.pos() - self.last_pan_pos
            self.last_pan_pos = event.pos()
            h_bar = self.horizontalScrollBar()
            v_bar = self.verticalScrollBar()
            h_bar.setValue(h_bar.value() - delta.x())
            v_bar.setValue(v_bar.value() - delta.y())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        # Taken (and cleared) before any early return, so the drag state can never be
        # left set -- see the note in mousePressEvent.
        candidates = self._drag_candidates
        self._drag_candidates = []

        if self.pan_active and event.button() == Qt.MiddleButton:
            self.pan_active = False
            self.setCursor(Qt.ArrowCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)
        if not candidates:
            return
        drop_pt = self.mapToScene(event.pos())
        target_block = None
        for item in self.items(event.pos()):
            if isinstance(item, EquipmentBlockItem):
                target_block = item
                break
        self.panel.handle_stub_drop(candidates, target_block, drop_pt)

    def wheelEvent(self, event):
        # Needed to work a dense port grid (e.g. 48 ports on a 1U switch) at all --
        # zoomed out, each port is a couple of pixels wide.
        zoom_factor = ZOOM_STEP if event.angleDelta().y() > 0 else 1.0 / ZOOM_STEP
        self.scale(zoom_factor, zoom_factor)

    def contextMenuEvent(self, event):
        item = self.itemAt(event.pos())
        if isinstance(item, PatchCableItem):
            menu = QMenu(self)
            delete_action = menu.addAction("Delete Patch Cable")
            delete_action.triggered.connect(lambda: self.panel.delete_patch_cables([item]))
            menu.exec(event.globalPos())
            return
        if isinstance(item, EquipmentBlockItem):
            menu = QMenu(self)
            delete_action = menu.addAction("Delete Device")
            delete_action.triggered.connect(lambda: self.panel.delete_devices([item.slot]))
            menu.exec(event.globalPos())
            return
        if isinstance(item, CableStubItem) and item.terminated:
            menu = QMenu(self)
            return_action = menu.addAction("Return Cable to Pitchfork")
            return_action.triggered.connect(lambda: self.panel.return_stubs_to_pitchfork([item]))
            menu.exec(event.globalPos())
            return
        super().contextMenuEvent(event)

    # ── Rendering ──
    def refresh(self):
        self.scene_obj.clear()
        self._block_items = []
        self._stub_items = []

        rack = self.panel.rack
        self.frame_item = RackFrameItem(rack.ru_height)
        self.scene_obj.addItem(self.frame_item)

        for slot in rack.slots:
            block = EquipmentBlockItem(slot, rack.ru_height, self.panel)
            block.moved_to_ru.connect(self.panel.move_slot_to)
            self.scene_obj.addItem(block)
            self._block_items.append(block)

        self._build_stubs()
        self._build_trunk()
        self._build_patch_cables()

        frame_h = rack.ru_height * RU_PIXEL_HEIGHT
        rect = QRectF(-20, -20, STUB_COLUMN_X + 140, max(frame_h, STUB_MARGIN_TOP + len(self._unrouted_stubs()) * STUB_SPACING) + 40)
        self.scene_obj.setSceneRect(rect)

    def _block_for_device(self, device_id):
        for b in self._block_items:
            if b.slot.device.id == device_id:
                return b
        return None

    def _unrouted_stubs(self):
        return [s for s in self._stub_items if not s.terminated]

    def _build_stubs(self):
        cable_ends = self.panel.cables_terminating_here()
        unrouted_index = 0
        # Multiple cables landing on the same whole-device target (no port grid, or a
        # click that missed one) would otherwise all sit at the exact same point --
        # stack them vertically along that device's edge instead, same idea as the
        # unrouted pitchfork fan.
        device_stub_counts = {}
        for cable, end_index in cable_ends:
            label = self.panel.stub_label(cable, end_index)
            stub = CableStubItem(cable, end_index, label, self.panel)
            anchor_id = cable.vertex_anchors[end_index]
            port = cable.start_port if end_index == 0 else cable.end_port

            if anchor_id == self.panel.rack.id:
                stub.terminated = False
                stub.setPos(STUB_COLUMN_X, STUB_MARGIN_TOP + unrouted_index * STUB_SPACING)
                unrouted_index += 1
            else:
                stub.terminated = True
                block = self._block_for_device(anchor_id)
                if block is None:
                    stub.setPos(STUB_COLUMN_X, STUB_MARGIN_TOP + unrouted_index * STUB_SPACING)
                    unrouted_index += 1
                elif block.has_ports() and port is not None:
                    stub.setPos(block.port_scene_pos(port) + QPointF(6, 0))
                else:
                    n = device_stub_counts.get(anchor_id, 0)
                    device_stub_counts[anchor_id] = n + 1
                    block_rect = QRectF(block.pos(), QPointF(block.pos().x() + block._w, block.pos().y() + block._h))
                    stub.setPos(block_rect.right() + 4, block_rect.top() + 10 + n * STUB_SPACING)

            self.scene_obj.addItem(stub)
            self._stub_items.append(stub)

    def _build_trunk(self):
        # Tines fan out to EVERY incoming stub, not just the still-unrouted ones -- a
        # landed/terminated stub still represents a real physical cable that came in
        # from outside the rack and was run to that specific port, and the pitchfork
        # is what shows that provenance. Drawn behind everything else (z=-5) so a tine
        # running deep into the rack just disappears behind the equipment it passes,
        # like a cable dressed along the rack's side channel rather than floating
        # visibly on top of the port grid it terminates at.
        stubs = self._stub_items
        if not stubs:
            return
        unrouted = self._unrouted_stubs()
        anchor_stubs = unrouted if unrouted else stubs
        ys = [s.y() for s in anchor_stubs]
        conv = QPointF(STUB_COLUMN_X + 40, sum(ys) / len(ys))

        path = QPainterPath(conv)
        path.lineTo(QPointF(conv.x() + 40, conv.y()))  # trunk entering from further right
        for s in stubs:
            tine = QPainterPath(conv)
            tine.lineTo(QPointF(conv.x() - 20, s.y()))
            tine.lineTo(QPointF(s.x(), s.y()))
            path.addPath(tine)

        item = self.scene_obj.addPath(path, QPen(QColor("#3f3f52"), 2.0))
        item.setZValue(-5)
        self._trunk_path_item = item

    def _build_patch_cables(self):
        self._patch_path_items = []
        for cable, a_id, a_port, b_id, b_port in self.panel.patch_cables_here():
            a_pos = self._anchor_scene_pos(a_id, a_port)
            b_pos = self._anchor_scene_pos(b_id, b_port)
            if a_pos is None or b_pos is None:
                continue
            path = QPainterPath(a_pos)
            mid_y = (a_pos.y() + b_pos.y()) / 2.0
            path.cubicTo(QPointF(a_pos.x(), mid_y), QPointF(b_pos.x(), mid_y), b_pos)
            item = PatchCableItem(cable, path, self.panel)
            self.scene_obj.addItem(item)
            self._patch_path_items.append(item)

    def _anchor_scene_pos(self, item_id, port):
        if item_id == self.panel.rack.id:
            return QPointF(FRAME_WIDTH, 0)
        block = self._block_for_device(item_id)
        if block is None:
            return None
        if block.has_ports() and port is not None:
            return block.port_scene_pos(port)
        return block.mapToScene(QPointF(block._w, block._h / 2))
