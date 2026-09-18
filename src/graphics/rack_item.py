import uuid
from PySide6.QtWidgets import QGraphicsObject
from PySide6.QtGui import QPen, QColor, QBrush, QPainterPath
from PySide6.QtCore import QRectF, QPointF, Qt
from src.graphics.icon_scale import (icon_scale_of, natural_size,
                                     paint_at_icon_scale, scaled_size)
from src.graphics.label_render import (draw_item_label, expand_for_label, label_anchor)


class RackSlot:
    """One mounted device inside a RackItem: the DeviceItem itself (never added to the
    scene directly while mounted -- see CanvasView.get_network_devices), plus where it
    sits in the rack's RU numbering."""

    def __init__(self, device, start_ru, ru_size=1):
        self.device = device
        self.start_ru = start_ru
        self.ru_size = ru_size


class RackItem(QGraphicsObject):
    width = scaled_size("width")
    design_width = natural_size("width")

    """A data rack container: switches/NVRs mounted inside are hidden from the floor
    plan (the point is decluttering -- 'instead of putting a bunch of switches, NVRs
    and all the other shit directly on the canvas'), replaced by this one compact icon.
    Cables anchored to a mounted device visually terminate at the rack's position,
    since that's where the device now spatially "lives" as far as the floor plan goes
    -- see cable_item.py's _find_anchor_target. Double-click opens the Rack Editor to
    see/manage what's actually inside."""

    BOLT_COL_INSET = 5.0

    def __init__(self, x, y, label="Rack", ru_height=12, parent=None):
        super().__init__(parent)
        self.object_type = "rack"
        self.id = str(uuid.uuid4())

        self.label = label
        self.ru_height = ru_height
        self.notes = ""
        self.slots = []  # list of RackSlot

        # Cable allowances for runs landing here, in feet. None means "follow the
        # default for my kind" (Settings > Cabling); an explicit number overrides it.
        # See core/cable_length.py.
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

        self.width = 34.0

    # ── Slot management ──
    def used_ru(self):
        return sum(s.ru_size for s in self.slots)

    def free_ru_ranges(self):
        """Yields (start_ru, length) for every contiguous run of empty RU slots,
        1-indexed from the bottom like a real rack elevation."""
        occupied = set()
        for s in self.slots:
            occupied.update(range(s.start_ru, s.start_ru + s.ru_size))
        run_start = None
        for ru in range(1, self.ru_height + 1):
            if ru not in occupied:
                if run_start is None:
                    run_start = ru
            else:
                if run_start is not None:
                    yield (run_start, ru - run_start)
                    run_start = None
        if run_start is not None:
            yield (run_start, self.ru_height - run_start + 1)

    def can_fit(self, ru_size, start_ru=None):
        if start_ru is not None:
            occupied = set()
            for s in self.slots:
                occupied.update(range(s.start_ru, s.start_ru + s.ru_size))
            span = range(start_ru, start_ru + ru_size)
            if span.stop - 1 > self.ru_height or span.start < 1:
                return False
            return not (occupied & set(span))
        return any(length >= ru_size for _start, length in self.free_ru_ranges())

    def next_fit(self, ru_size):
        for start, length in self.free_ru_ranges():
            if length >= ru_size:
                return start
        return None

    def mount(self, device, ru_size=1, start_ru=None):
        if start_ru is None:
            start_ru = self.next_fit(ru_size)
            if start_ru is None:
                return False
        if not self.can_fit(ru_size, start_ru):
            return False
        self.slots.append(RackSlot(device, start_ru, ru_size))
        device.setPos(self.pos())
        return True

    def unmount(self, device_id):
        for s in self.slots:
            if s.device.id == device_id:
                self.slots.remove(s)
                return s.device
        return None

    def find_slot(self, device_id):
        for s in self.slots:
            if s.device.id == device_id:
                return s
        return None

    # ── Rendering (compact floor-plan icon; full elevation lives in the Rack Editor) ──
    def boundingRect(self):
        h = self._icon_height()
        rect = QRectF(-self.width / 2 - 20, -h / 2 - 10, self.width + 40, h + 42)
        # Two stacked labels: the rack's name and its "n/m RU" fill readout.
        return expand_for_label(self, rect, h / 2 + 4.0, lines=2)

    def _icon_height(self):
        # Fixed regardless of ru_height -- the whole point of collapsing a rack into
        # one icon is that it stays a compact, constant size on the floor plan no
        # matter how much (or little) is inside; RU height only matters once you open
        # the Rack Editor to see the actual elevation.
        return self._design_icon_height() * icon_scale_of(self)

    def _design_icon_height(self):
        return 44.0

    def shape(self):
        path = QPainterPath()
        h = self._icon_height()
        path.addRect(QRectF(-self.width / 2, -h / 2, self.width, h))
        return path

    def paint(self, painter, option, widget=None):
        scene = self.scene()
        if scene and not getattr(scene, "global_show_icons", True):
            return

        lod = option.levelOfDetailFromTransform(painter.worldTransform())
        scale = 1.0 / lod if lod > 0 else 1.0

        h = self._icon_height()
        # The shell is drawn at natural size inside a scaled painter, so the bolt holes
        # and RU fill bar below keep their proportions -- sizing the bolt run off the
        # scaled height instead put fourteen of them down a rack at 4x.
        design_h = self._design_icon_height()
        rect = QRectF(-self.design_width / 2, -design_h / 2, self.design_width, design_h)
        accent = QColor("#60a5fa") if self.isSelected() else QColor("#64748b")

        painter.save()
        screen = scale / paint_at_icon_scale(painter, self)
        # `screen` keeps a quantity a constant number of pixels on screen despite
        # the painter scale above -- stroke weights and pixel-sized details use it,
        # while the icon's own geometry uses natural coordinates and grows.
        painter.setPen(QPen(accent, 2.0 * screen))
        painter.setBrush(QBrush(QColor("#0d0d13")))
        painter.drawRoundedRect(rect, 3.0, 3.0)

        # Bolt holes down both rails
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor("#3a3a4d")))
        # Natural units, not `* scale`: a constant-on-screen radius keeps growing in
        # scene units as you zoom out while the frame shrinks, so below roughly 32%
        # zoom the bolts overhang the rails they are supposed to sit inside.
        bolt_r = 1.6
        n_bolts = max(3, int(design_h // 12))
        for i in range(n_bolts):
            t = (i + 0.5) / n_bolts
            y = rect.top() + t * rect.height()
            painter.drawEllipse(QPointF(rect.left() + self.BOLT_COL_INSET, y), bolt_r, bolt_r)
            painter.drawEllipse(QPointF(rect.right() - self.BOLT_COL_INSET, y), bolt_r, bolt_r)

        # Fill indicator: a filled bar showing how much RU capacity is used
        used = self.used_ru()
        if self.ru_height > 0 and used > 0:
            frac = min(1.0, used / self.ru_height)
            fill_h = rect.height() * frac
            fill_rect = QRectF(rect.left() + 8, rect.bottom() - fill_h, rect.width() - 16, fill_h)
            fill_color = QColor("#22c55e") if used <= self.ru_height else QColor("#ef4444")
            fill_color.setAlphaF(0.35)
            painter.setBrush(QBrush(fill_color))
            painter.drawRect(fill_rect)

        painter.restore()

        if self.isSelected():
            painter.save()
            sel_pen = QPen(QColor("#60a5fa"))
            sel_pen.setStyle(Qt.DashLine)
            sel_pen.setWidthF(1.0 * scale)
            painter.setPen(sel_pen)
            painter.setBrush(Qt.NoBrush)
            outline = QRectF(-self.width / 2, -h / 2, self.width, h)
            painter.drawRoundedRect(outline.adjusted(-4 * scale, -4 * scale, 4 * scale, 4 * scale), 4, 4)
            painter.restore()

        draw_item_label(painter, label_anchor(self, h / 2 + 4.0), self.label, scale)
        used_text = f"{used}/{self.ru_height} RU"
        draw_item_label(painter, label_anchor(self, h / 2 + 22.0 * scale), used_text, scale, font_size=8)

    def mouseDoubleClickEvent(self, event):
        scene = self.scene()
        view = scene.views()[0] if scene and scene.views() else None
        if view is not None and hasattr(view, "open_rack_editor"):
            view.open_rack_editor(self)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def itemChange(self, change, value):
        if change == QGraphicsObject.ItemPositionChange and self.scene():
            # Cables anchored to a mounted device track the rack's position, not the
            # device's own (irrelevant, since it's not independently placed anymore).
            new_pos = value
            for s in self.slots:
                s.device.setPos(new_pos)
            scene = self.scene()
            if hasattr(scene, "on_item_property_changed"):
                from PySide6.QtCore import QTimer
                QTimer.singleShot(0, lambda: scene.on_item_property_changed(self))
        return super().itemChange(change, value)
