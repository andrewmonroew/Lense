from PySide6.QtWidgets import QGraphicsItem, QGraphicsObject, QGraphicsPathItem
from PySide6.QtGui import QPen, QColor, QBrush, QFont, QPainterPathStroker, QPainterPath, QFontMetrics
from PySide6.QtCore import QRectF, QPointF, Qt, Signal
from src.core import poe_chain

RU_PIXEL_HEIGHT = 26.0
RAIL_WIDTH = 20.0
INTERIOR_WIDTH = 260.0
FRAME_WIDTH = RAIL_WIDTH * 2 + INTERIOR_WIDTH
BOLT_HOLES_PER_RU = 3


def ru_span_to_rect(start_ru, ru_size, ru_height):
    """Local-space rect (relative to the frame's top-left at (0,0)) for a slot spanning
    [start_ru, start_ru+ru_size). RU 1 is the bottom row, matching a real elevation."""
    top_ru = start_ru + ru_size - 1
    y = (ru_height - top_ru) * RU_PIXEL_HEIGHT
    return QRectF(RAIL_WIDTH, y, INTERIOR_WIDTH, ru_size * RU_PIXEL_HEIGHT)


def y_to_ru(y, ru_height):
    """Inverse of ru_span_to_rect's y -- which RU row a given local y falls in."""
    row = int(y // RU_PIXEL_HEIGHT)
    ru = ru_height - row
    return max(1, min(ru_height, ru))


class RackFrameItem(QGraphicsItem):
    """Static backdrop: two rails with bolt holes, an RU gridline + number per row.
    Non-interactive -- just what a rack elevation is supposed to look like."""

    def __init__(self, ru_height, parent=None):
        super().__init__(parent)
        self.ru_height = ru_height
        self.setZValue(-10)
        self.setFlag(QGraphicsItem.ItemIsSelectable, False)
        self.setFlag(QGraphicsItem.ItemIsMovable, False)

    def set_ru_height(self, ru_height):
        self.prepareGeometryChange()
        self.ru_height = ru_height
        self.update()

    def boundingRect(self):
        return QRectF(0, 0, FRAME_WIDTH, self.ru_height * RU_PIXEL_HEIGHT)

    def paint(self, painter, option, widget=None):
        total_h = self.ru_height * RU_PIXEL_HEIGHT

        painter.setPen(QPen(QColor("#2e2e3f"), 1.0))
        painter.setBrush(QBrush(QColor("#0d0d13")))
        painter.drawRect(QRectF(RAIL_WIDTH, 0, INTERIOR_WIDTH, total_h))

        painter.setBrush(QBrush(QColor("#1b1b26")))
        painter.drawRect(QRectF(0, 0, RAIL_WIDTH, total_h))
        painter.drawRect(QRectF(RAIL_WIDTH + INTERIOR_WIDTH, 0, RAIL_WIDTH, total_h))

        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor("#3a3a4d")))
        bolt_r = 2.0
        font = QFont("Inter", 8)
        painter.setFont(font)

        for row in range(self.ru_height):
            ru_num = self.ru_height - row
            y_top = row * RU_PIXEL_HEIGHT
            for i in range(BOLT_HOLES_PER_RU):
                t = (i + 1) / (BOLT_HOLES_PER_RU + 1)
                by = y_top + t * RU_PIXEL_HEIGHT
                painter.drawEllipse(QPointF(RAIL_WIDTH / 2, by), bolt_r, bolt_r)
                painter.drawEllipse(QPointF(RAIL_WIDTH + INTERIOR_WIDTH + RAIL_WIDTH / 2, by), bolt_r, bolt_r)

            painter.setPen(QPen(QColor("#22222e"), 1.0))
            painter.drawLine(QPointF(RAIL_WIDTH, y_top), QPointF(RAIL_WIDTH + INTERIOR_WIDTH, y_top))
            painter.setPen(QColor("#4a4a5e"))
            painter.drawText(QRectF(0, y_top, RAIL_WIDTH - 2, RU_PIXEL_HEIGHT), Qt.AlignRight | Qt.AlignVCenter, str(ru_num))
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(QColor("#3a3a4d")))


class EquipmentBlockItem(QGraphicsObject):
    """One mounted RackSlot (switch/NVR/patch panel), positioned by RU. In Select/Move
    mode it's draggable vertically (RU-snapped) to reorder; in Patch Cable mode a click
    reports itself (and, if it has a port grid, which port) to the panel instead of
    moving. ANY mounted device with a clean numeric port count (not just patch panels
    -- most switches too) gets a real, individually-clickable port grid, laid out in
    two rows once there are more than 24 so ports stay a reasonable size to hit -- the
    Rack Editor's wheel-zoom is what makes a dense 48-port grid usable up close."""

    moved_to_ru = Signal(object, int)  # (slot, new_start_ru) after a reorder drag

    def __init__(self, slot, ru_height, panel, parent=None):
        super().__init__(parent)
        self.slot = slot
        self.ru_height = ru_height
        self.panel = panel
        self.setFlags(QGraphicsObject.ItemIsSelectable | QGraphicsObject.ItemIsMovable)
        self.setAcceptHoverEvents(True)
        self._drag_start_ru = slot.start_ru
        self.is_hovered = False
        self._hover_port = None
        self._sync_pos()

    def _sync_pos(self):
        rect = ru_span_to_rect(self.slot.start_ru, self.slot.ru_size, self.ru_height)
        self.setPos(rect.left(), rect.top())
        self._w = rect.width()
        self._h = rect.height()

    # How far left of the rack's outer edge (RackFrameItem's x=0) the device name's
    # LAST character sits, and how wide a margin is reserved for the text itself.
    LABEL_GAP_FROM_RACK = 20.0
    LABEL_MARGIN_WIDTH = 220.0

    def boundingRect(self):
        # Extended left to cover the device-name label, which now lives in the margin
        # outside the rack frame rather than overlaid on the port grid (see paint()).
        left_margin = RAIL_WIDTH + self.LABEL_GAP_FROM_RACK + self.LABEL_MARGIN_WIDTH
        return QRectF(-left_margin, 0, self._w + left_margin, self._h)

    def _has_rj45(self):
        return bool(getattr(self.slot.device, "port_count", None))

    def _has_sfp(self):
        return bool(getattr(self.slot.device, "sfp_port_count", None))

    def has_ports(self):
        return self._has_rj45() or self._has_sfp()

    def is_patch_panel(self):
        return getattr(self.slot.device, "category", None) == "patch-panel"

    def _rj45_grid_dims(self):
        total = self.slot.device.port_count
        rows = 2 if total > 24 else 1
        cols = (total + rows - 1) // rows
        return rows, cols, total

    # Fixed, "true to size" port dimensions -- NOT stretched to fill whatever space
    # happens to be available. A 24/48-port switch's grid divides down close to these
    # on its own; a 1-2 port NVR previously stretched its single port/SFP cell to fill
    # the ENTIRE band, which read as one giant undifferentiated block instead of a
    # real, individually-sized jack. min() against the divided-up band size still
    # shrinks ports down to fit when there genuinely isn't room for many of them
    # (e.g. a 48-port grid), it just never grows a port bigger than this.
    RJ45_PORT_WIDTH = 10.0
    SFP_PORT_HEIGHT = 10.0
    SFP_COLUMN_WIDTH = 14.0

    def _sfp_column_width(self):
        # A separate vertical strip on the RIGHT edge, spanning the full port-grid
        # height -- how a real switch/NVR faceplate keeps its SFP/SFP+ uplinks off to
        # the side of the copper bank, rather than a shorter row underneath it (which
        # ate into every RJ45 row's height and visibly squashed the whole grid). Fixed
        # width, independent of RJ45 port count -- it used to be derived from "one RJ45
        # column's width", which for a 1-port NVR (cols=1) meant "the entire block".
        return self.SFP_COLUMN_WIDTH if self._has_sfp() else 0.0

    def _rj45_band_rect(self):
        return QRectF(0, 2, self._w - self._sfp_column_width(), max(4.0, self._h - 4))

    def _sfp_band_rect(self):
        if not self._has_sfp():
            return None
        w = self._sfp_column_width()
        return QRectF(self._w - w, 2, w, max(4.0, self._h - 4))

    def _rj45_port_width(self, band=None):
        if band is None:
            band = self._rj45_band_rect()
        _rows, cols, _total = self._rj45_grid_dims()
        return min(self.RJ45_PORT_WIDTH, band.width() / cols)

    def _sfp_port_height(self, band=None):
        if band is None:
            band = self._sfp_band_rect()
        count = self.slot.device.sfp_port_count
        return min(self.SFP_PORT_HEIGHT, band.height() / count) if count else 0.0

    # Unoccupied ports are tinted by what they can actually deliver, so a designer can
    # see at a glance that (say) a Pro 24's ports 17-24 are the PoE++ bank -- the thing
    # that decides whether a downstream Flex gets 46W or 20W. Occupied still wins with
    # green: "is something plugged in" stays the primary read.
    POE_CLASS_FILL = {
        "802.3bt": QColor("#7c4a03"),   # PoE++ - amber
        "802.3at": QColor("#1e3a5f"),   # PoE+  - blue
        "802.3af": QColor("#134e4a"),   # PoE   - teal
    }
    NO_POE_FILL = QColor("#2e2e3f")

    def _poe_group_for_port(self, port_num):
        dev = self.slot.device
        if dev.is_sfp_port(port_num):
            return None
        for group in poe_chain.port_groups(dev):
            if group["first"] <= port_num <= group["last"]:
                return group
        return None

    def port_fill(self, port_num, occupied):
        if occupied:
            return QColor("#22c55e")
        group = self._poe_group_for_port(port_num)
        if group is None:
            return self.NO_POE_FILL
        return self.POE_CLASS_FILL.get(group["standard"], self.NO_POE_FILL)

    def port_tooltip(self, port_num):
        dev = self.slot.device
        if dev.is_sfp_port(port_num):
            return f"{dev.port_display_name(port_num)} - data only, no PoE"
        group = self._poe_group_for_port(port_num)
        if group is None:
            return f"Port {port_num} - no PoE"
        return (f"Port {port_num} - {poe_chain.class_label(group['standard'])} "
                f"up to {group['maxWatts']:g}W")

    def port_at(self, local_pt):
        if not self.has_ports():
            return None
        if not (0 <= local_pt.x() <= self._w) or not (0 <= local_pt.y() <= self._h):
            return None
        dev = self.slot.device

        sfp_band = self._sfp_band_rect()
        if sfp_band is not None and sfp_band.left() <= local_pt.x() <= sfp_band.right():
            count = dev.sfp_port_count
            ph = self._sfp_port_height(sfp_band)
            if ph <= 0:
                return None
            idx = int((local_pt.y() - sfp_band.top()) // ph)
            if 0 <= idx < count:
                return dev.SFP_PORT_BASE + idx + 1
            return None

        if self._has_rj45():
            band = self._rj45_band_rect()
            if band.top() <= local_pt.y() <= band.bottom() and band.left() <= local_pt.x() <= band.right():
                rows, cols, total = self._rj45_grid_dims()
                pw = self._rj45_port_width(band)
                ph = band.height() / rows
                col = int((local_pt.x() - band.left()) // pw)
                row = int((local_pt.y() - band.top()) // ph) if ph > 0 else 0
                idx = row * cols + col
                if 0 <= col < cols and 0 <= row < rows and 0 <= idx < total:
                    return idx + 1
        return None

    def port_rect(self, port_num):
        dev = self.slot.device
        if dev.is_sfp_port(port_num):
            band = self._sfp_band_rect()
            n = port_num - dev.SFP_PORT_BASE
            ph = self._sfp_port_height(band)
            return QRectF(band.left(), band.top() + (n - 1) * ph, band.width() - 1, ph - 1)
        band = self._rj45_band_rect()
        rows, cols, _total = self._rj45_grid_dims()
        idx = port_num - 1
        row, col = divmod(idx, cols)
        pw = self._rj45_port_width(band)
        ph = band.height() / rows
        return QRectF(band.left() + col * pw, band.top() + row * ph, pw - 1, ph - 1)

    def port_scene_pos(self, port_num):
        return self.mapToScene(self.port_rect(port_num).center())

    def _draw_port_number(self, painter, rect, text, occupied, lod):
        # Zoom-aware: a 48-port grid's cells are only a few scene-units wide, so a
        # number drawn at that native size would just be noise -- skip it until the
        # ACTUAL on-screen size (rect size * zoom level) is big enough to read, same
        # idea as the wheel-zoom itself ("even if we gotta zoom in to see it").
        if rect.width() * lod < 9 or rect.height() * lod < 7:
            return
        painter.save()
        painter.setPen(QColor("#0d0d13") if occupied else QColor("#c7c7d6"))
        font = QFont("Inter", 5)
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignCenter, text)
        painter.restore()

    def paint(self, painter, option, widget=None):
        dev = self.slot.device
        lod = option.levelOfDetailFromTransform(painter.worldTransform())
        accent = QColor("#60a5fa") if self.isSelected() else (
            QColor("#94a3b8") if self.is_patch_panel() else
            (QColor("#3b82f6") if dev.category == "switch" else QColor("#8b5cf6")))

        painter.setPen(QPen(accent, 1.5))
        painter.setBrush(QBrush(QColor("#1b1b26")))
        rect = QRectF(0, 0, self._w, self._h)
        painter.drawRect(rect)

        if self._has_rj45():
            painter.setPen(QPen(QColor("#0d0d13"), 0.5))
            _rows, _cols, total = self._rj45_grid_dims()
            for p in range(1, total + 1):
                occupied = bool(dev.cables_at_port(p, self.panel.canvas_view))
                painter.setBrush(QBrush(self.port_fill(p, occupied)))
                port_rect = self.port_rect(p)
                painter.drawRect(port_rect)
                self._draw_port_number(painter, port_rect, str(p), occupied, lod)

        if self._has_sfp():
            painter.setPen(QPen(QColor("#0d0d13"), 0.5))
            for p in range(1, dev.sfp_port_count + 1):
                port_num = dev.SFP_PORT_BASE + p
                occupied = bool(dev.cables_at_port(port_num, self.panel.canvas_view))
                painter.setBrush(QBrush(QColor("#22c55e") if occupied else QColor("#4a4a5e")))
                port_rect = self.port_rect(port_num)
                painter.drawRect(port_rect)
                self._draw_port_number(painter, port_rect, f"S{p}", occupied, lod)

        # Device name lives in the margin to the LEFT of the rack, right-aligned so its
        # last character sits LABEL_GAP_FROM_RACK px before the rack's outer edge --
        # not overlaid on the block itself, which ran straight into the port grid/
        # numbers once every port started showing its own label.
        painter.setPen(QColor("#f5f5fa"))
        font = QFont("Inter", 8)
        font.setWeight(QFont.DemiBold)
        painter.setFont(font)
        label = f"{dev.label} — {dev.spec.get('model', '')}"
        label_right_x = -RAIL_WIDTH - self.LABEL_GAP_FROM_RACK
        label_rect = QRectF(label_right_x - self.LABEL_MARGIN_WIDTH, 0, self.LABEL_MARGIN_WIDTH, self._h)
        painter.drawText(label_rect, Qt.AlignRight | Qt.AlignVCenter, label)

        if self.panel.tool_mode == "patch" and self.is_hovered:
            painter.setPen(QPen(QColor("#eab308"), 2.0, Qt.DashLine))
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(rect)

        # Per-port hover readout ("Port 5" / "SFP 2") -- drawn on the canvas itself
        # rather than via QToolTip. A native OS tooltip is a separate always-on-top
        # window with its own show/hide timing -- it can sit right over a nearby
        # CableStubItem's own custom-drawn label and, being a real popup, interferes
        # with moving the mouse on to hover the next port/stub over. A plain paint()
        # overlay can never do either: it's pure pixels, gone the instant this repaints.
        if self._hover_port is not None:
            text = dev.port_display_name(self._hover_port)
            port_rect = self.port_rect(self._hover_port)
            painter.save()
            hover_font = QFont("Inter", 7)
            painter.setFont(hover_font)
            metrics = painter.fontMetrics()
            text_w = metrics.horizontalAdvance(text) + 8
            readout_rect = QRectF(port_rect.center().x() - text_w / 2, port_rect.top() - 15, text_w, 13)
            painter.setBrush(QBrush(QColor(10, 10, 16, 230)))
            painter.setPen(Qt.NoPen)
            painter.drawRect(readout_rect)
            painter.setPen(QColor("#e8e8f0"))
            painter.drawText(readout_rect, Qt.AlignCenter, text)
            painter.restore()

    def hoverEnterEvent(self, event):
        self.is_hovered = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverMoveEvent(self, event):
        # See the hover-readout comment in paint() for why this tracks a hovered port
        # directly instead of calling setToolTip().
        port = None
        if self.panel.tool_mode != "patch" and self.has_ports():
            port = self.port_at(event.pos())
        if port != self._hover_port:
            self._hover_port = port
            self.update()
        super().hoverMoveEvent(event)

    def hoverLeaveEvent(self, event):
        self.is_hovered = False
        if self._hover_port is not None:
            self._hover_port = None
        self.update()
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event):
        if self.panel.tool_mode == "patch":
            port = self.port_at(event.pos())
            self.panel.handle_patch_click(self.slot, port)
            event.accept()
            return
        self._drag_start_ru = self.slot.start_ru
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if self.panel.tool_mode == "patch":
            event.accept()
            return
        super().mouseReleaseEvent(event)
        bottom_ru = y_to_ru(self.y() + self._h - 1, self.ru_height)
        if bottom_ru != self._drag_start_ru:
            self.moved_to_ru.emit(self.slot, bottom_ru)
        else:
            self._sync_pos()  # snap back exactly even if unchanged


class CableStubItem(QGraphicsObject):
    """The rack-side end of one field cable that terminates at this rack. Unterminated
    (still anchored to the rack as a whole, not a specific port/device) stubs sit in a
    fanned-out column near the right edge, pitchfork-style. Dragging is handled at the
    view level (RackElevationView) so a rubber-band multi-selection of stubs can be
    dropped as a group in one motion, not just one at a time.

    Its label only draws on hover/selection -- terminated stubs sit right at their
    port, often just a few pixels from their neighbors on a dense grid, and every
    label drawn permanently there runs together into an unreadable smear."""

    def __init__(self, cable, end_index, label, panel, parent=None):
        super().__init__(parent)
        self.cable = cable
        self.end_index = end_index  # 0 or -1, which end of cable.vertex_anchors this is
        self.label = label
        self.panel = panel
        self.terminated = False
        self.setFlags(QGraphicsObject.ItemIsSelectable | QGraphicsObject.ItemIsMovable)
        self.setAcceptHoverEvents(True)
        self.radius = 5.0
        self.is_hovered = False

    def boundingRect(self):
        return QRectF(-self.radius - 4, -self.radius - 4, self.radius * 2 + 90, self.radius * 2 + 8)

    def _label_is_visible(self):
        return not self.terminated or self.is_hovered or self.isSelected()

    def shape(self):
        """Tight hit area -- the dot, plus the label strip only while it's actually drawn.

        Without this, hit-testing fell back to boundingRect, which reserves ~100px to
        the right for the label. On a dense port grid (cells are ~10px wide) that
        invisible box blanketed a dozen neighbouring ports, so clicking near one stub
        routinely grabbed a different one sitting several ports away -- which is what
        made moving a cable between ports feel like it was picking up the wrong cable
        and flinging some other one somewhere else.
        """
        path = QPainterPath()
        path.addEllipse(QPointF(0, 0), self.radius + 3, self.radius + 3)
        if self._label_is_visible():
            metrics = QFontMetrics(QFont("Inter", 7))
            width = metrics.horizontalAdvance(self.label) + 6
            path.addRect(QRectF(self.radius + 2, -8, width, 16))
        return path

    def paint(self, painter, option, widget=None):
        color = QColor("#22c55e") if self.terminated else QColor("#f59e0b")
        if self.isSelected():
            color = QColor("#60a5fa")

        # A terminated stub sits right on top of its port -- the port cell itself
        # already turns green when occupied (see EquipmentBlockItem.paint) and the
        # yellow patch cord makes the connection obvious, so the dot never draws for
        # a terminated stub at all, not even on hover/selection -- it was still
        # disorienting to have it flash back in while working the port grid. The
        # item stays fully clickable/selectable regardless (boundingRect doesn't
        # depend on what's actually painted), so it's still selectable for Backspace
        # -> return-to-pitchfork. An unrouted stub has nothing else marking it at
        # all, so its dot stays permanently visible -- that's still the only thing
        # that shows it exists out there in the pitchfork column.
        if not self.terminated:
            painter.setPen(QPen(color.darker(140), 1.5))
            painter.setBrush(QBrush(color))
            painter.drawEllipse(QPointF(0, 0), self.radius, self.radius)
        elif self.isSelected():
            # A selected terminated stub used to render nothing at all, so a lingering
            # multi-selection was invisible -- then dragging one stub silently dragged
            # the whole hidden group. A thin ring shows what's selected without putting
            # the solid dot back over the port grid.
            painter.setPen(QPen(color, 1.5))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(QPointF(0, 0), self.radius + 2, self.radius + 2)

        show_label = not self.terminated or self.is_hovered or self.isSelected()
        if show_label:
            painter.setPen(QColor("#c7c7d6"))
            font = QFont("Inter", 7)
            painter.setFont(font)
            metrics = painter.fontMetrics()
            text_w = metrics.horizontalAdvance(self.label) + 6
            bg_rect = QRectF(self.radius + 2, -8, text_w, 16)
            painter.setBrush(QBrush(QColor(10, 10, 16, 220)))
            painter.setPen(Qt.NoPen)
            painter.drawRect(bg_rect)
            painter.setPen(QColor("#e8e8f0"))
            painter.drawText(bg_rect, Qt.AlignLeft | Qt.AlignVCenter, self.label)

    def hoverEnterEvent(self, event):
        self.is_hovered = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self.is_hovered = False
        self.update()
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event):
        if self.panel.tool_mode == "patch":
            # Let the click fall through to whatever's underneath (a port on the
            # equipment block this stub is sitting on/near) instead of grabbing this
            # stub -- otherwise clicking an already-occupied port (which has a stub
            # marker right on it) would drag the stub instead of registering the
            # patch-cable click, and the drag could silently re-terminate the field
            # cable onto a different port than intended.
            event.ignore()
            return
        super().mousePressEvent(event)


class PatchCableItem(QGraphicsPathItem):
    """The visual line for one patch cable (a short jumper between two ports/devices,
    drawn inside the rack only -- see RackEditorPanel.handle_patch_click). Selectable
    and clickable so a mis-patched connection can be selected and deleted directly,
    the same as any other cable, rather than needing to go find it on the floor plan
    (it isn't even there -- patch cables are hidden from the Canvas on purpose)."""

    def __init__(self, cable, path, panel, parent=None):
        super().__init__(path, parent)
        self.cable = cable
        self.panel = panel
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setAcceptHoverEvents(True)
        self.setZValue(3)
        self.is_hovered = False
        self._apply_pen()

    def _apply_pen(self):
        if self.isSelected():
            color = QColor("#ef4444")
        elif self.is_hovered:
            color = QColor("#fbbf24")
        else:
            color = QColor("#eab308")
        width = 3.0 if (self.isSelected() or self.is_hovered) else 2.0
        self.setPen(QPen(color, width))

    def _should_ghost(self):
        # Two independent triggers for the same fade + click-through treatment:
        # - mid-draw (first endpoint already clicked, waiting on the second) -- NOT
        #   just "the Draw Patch Cable tool happens to be selected", which made every
        #   cable look permanently faded/invisible to anyone who mostly works in that
        #   mode; the fade is only needed for the few seconds an existing cable might
        #   visually bury the port a NEW connection is aimed at.
        # - the H key held down -- manual "peek under the cables" on demand, for
        #   whenever you need to see/click a port a cable happens to be sitting on
        #   without having to actually start drawing a new one to trigger it.
        actively_drawing = self.panel.tool_mode == "patch" and self.panel._patch_pending is not None
        return actively_drawing or getattr(self.panel, "ghost_mode", False)

    def paint(self, painter, option, widget=None):
        self._apply_pen()
        painter.save()
        if self._should_ghost():
            painter.setOpacity(painter.opacity() * 0.25)
        super().paint(painter, option, widget)
        painter.restore()

    def hoverEnterEvent(self, event):
        self.is_hovered = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self.is_hovered = False
        self.update()
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event):
        if self._should_ghost():
            # Let the click fall through to whatever's underneath (a port this cable
            # happens to visually cross over) instead of grabbing/selecting THIS
            # cable -- same reasoning as CableStubItem's click-through in this mode.
            event.ignore()
            return
        super().mousePressEvent(event)

    def shape(self):
        # Widen the clickable area well beyond the visible 2-3px line -- a thin
        # bezier is otherwise nearly impossible to click precisely. Collapsed to
        # nothing while drawing a new patch cable, so a faded-out existing cable
        # can never intercept a click meant for the port grid underneath it even
        # via its normally-generous hit area.
        if self._should_ghost():
            return QPainterPath()
        stroker = QPainterPathStroker()
        stroker.setWidth(10.0)
        return stroker.createStroke(self.path())
