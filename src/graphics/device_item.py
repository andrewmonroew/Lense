import time
import uuid
from PySide6.QtWidgets import QGraphicsObject, QStyleOptionGraphicsItem
from PySide6.QtGui import QPen, QColor, QBrush, QPainterPath
from PySide6.QtCore import QRectF, QPointF, Qt
from src.core.utils import distance
from src.graphics.icon_scale import natural_size, paint_at_icon_scale, scaled_size
from src.graphics.label_render import (draw_item_label, draw_warning_badge,
                                       expand_for_label, label_anchor)

class DeviceItem(QGraphicsObject):
    # Icon geometry follows the canvas icon scale (see graphics/icon_scale.py) so
    # equipment stays legible over a floor plan of any resolution. Subclasses assign
    # these in __init__ exactly as before -- the assignment stores the natural size.
    width = scaled_size("width")
    height = scaled_size("height")
    radius = scaled_size("radius")
    # Natural sizes, for the artwork drawn inside a scaled painter (see paint).
    design_width = natural_size("width")
    design_height = natural_size("height")
    design_radius = natural_size("radius")

    def __init__(self, spec, x, y, parent=None):
        super().__init__(parent)
        self.object_type = "device"
        self.id = str(uuid.uuid4())
        self.spec = spec
        
        self.label = spec.get("model", "Network Device")
        self.elevation = 0.0
        self.notes = ""
        self.connected_cables = [] # List of cable IDs

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
        
        # Position
        self.setPos(x, y)
        self.setFlags(QGraphicsObject.ItemIsMovable | 
                      QGraphicsObject.ItemIsSelectable |
                      QGraphicsObject.ItemSendsGeometryChanges)

        # Style constants
        self.category = spec.get("category", "switch") # "switch" or "nvr"
        self.width = 32.0
        self.height = 20.0
        self.radius = 4.0
        self._connectivity_warnings = []
        self._warnings_checked_at = None  # see _update_connectivity_warnings

        # A clean numeric port count (most switches; NVRs are often a dict like
        # {"rj45": 1, "sfp": 1} instead and get no port grid -- None means "whole-
        # device" termination only). This is what lets the Rack Editor draw a real,
        # individually-clickable port grid on ANY mounted device that has one, not
        # just patch panels -- a cable landing on "Switch 1" instead of "Switch 1,
        # Port 14" was the whole reason ports mattered in the first place.
        # "ports" is a plain int for most categories (switches), but an NVR's catalog
        # entry stores it as {"rj45": n, "sfp": n} instead (its RJ45 count is an uplink
        # count, not a big switch-style bank) -- read either shape so any category with
        # ports renders a real grid, not just switches/panels.
        raw_ports = spec.get("ports")
        if isinstance(raw_ports, int) and raw_ports > 0:
            self.port_count = raw_ports
        elif isinstance(raw_ports, dict) and isinstance(raw_ports.get("rj45"), int) and raw_ports.get("rj45") > 0:
            self.port_count = raw_ports["rj45"]
        else:
            self.port_count = None

        # SFP (fiber uplink) ports are a physically separate jack type from RJ45, so
        # they get their own count and their own numbering range rather than being
        # folded into port_count -- see SFP_PORT_BASE. Read from a top-level
        # "sfpPorts" key (how the Catalog Manager saves it for every category) with a
        # fallback to ports["sfp"] for any older/scraped entry that only has the
        # nested NVR-style shape.
        raw_sfp = spec.get("sfpPorts")
        if not (isinstance(raw_sfp, int) and raw_sfp > 0) and isinstance(raw_ports, dict):
            raw_sfp = raw_ports.get("sfp")
        self.sfp_port_count = raw_sfp if isinstance(raw_sfp, int) and raw_sfp > 0 else None

    # Port numbers 1..SFP_PORT_BASE-1 are RJ45; SFP_PORT_BASE+1.. are SFP, so a plain
    # int can keep meaning "which port" everywhere (CableItem.start_port/end_port,
    # project-file serialization) without a schema change to distinguish jack types.
    SFP_PORT_BASE = 1000

    def is_sfp_port(self, port_num):
        return port_num is not None and port_num > self.SFP_PORT_BASE

    def port_display_name(self, port_num):
        if port_num is None:
            return None
        if self.is_sfp_port(port_num):
            return f"SFP {port_num - self.SFP_PORT_BASE}"
        return f"Port {port_num}"

    def cables_at_port(self, port_num, canvas_view, ignore_cable=None):
        """Every cable (field or patch) currently plugged into this port.

        `ignore_cable` excludes one cable from the count -- needed when re-seating a
        cable that is itself already landed somewhere on this device, so it doesn't
        collide with its own existing termination.
        """
        result = []
        for cable in canvas_view.get_cables():
            if ignore_cable is not None and cable is ignore_cable:
                continue
            if cable.start_device_id == self.id and cable.start_port == port_num:
                result.append(cable)
            elif cable.end_device_id == self.id and cable.end_port == port_num:
                result.append(cable)
        return result

    def field_cable_at_port(self, port_num, canvas_view, ignore_cable=None):
        for cable in self.cables_at_port(port_num, canvas_view, ignore_cable):
            if cable.cable_type != "Patch":
                return cable
        return None

    def patch_cable_at_port(self, port_num, canvas_view, ignore_cable=None):
        for cable in self.cables_at_port(port_num, canvas_view, ignore_cable):
            if cable.cable_type == "Patch":
                return cable
        return None

    def is_patch_panel(self):
        return self.category == "patch-panel"

    # Passive jacks (patch panels, wall drops): a field cable lands on the back of a
    # port and a patch cable jumpers out the front to the real switch/NVR on the same
    # port, rather than one physical jack per port like every other device. Never a
    # routing/PoE endpoint themselves -- CanvasView.resolve_camera_chain walks through
    # them, get_device_connectivity_warnings and build_network_topology exclude them.
    PASSTHROUGH_CATEGORIES = ("patch-panel", "drop")

    def is_pass_through(self):
        return self.category in self.PASSTHROUGH_CATEGORIES

    def port_free_for_new_cable(self, port_num, canvas_view, new_cable_type, ignore_cable=None):
        """Whether `port_num` can take one more cable of `new_cable_type`. A passive
        jack's (patch panel, wall drop) port is really two independent jacks (a field
        cable punched down on the back, a patch cable jumpered on the front), so field
        vs. patch occupancy is tracked separately there. Every other device
        (switch/NVR/access-point/etc) has one physical jack per port -- a patch cable
        and a field cable can never coexist on the same port, so ANY existing cable
        there blocks a new one, regardless of which type is being added."""
        if self.is_pass_through():
            if new_cable_type == "Patch":
                return self.patch_cable_at_port(port_num, canvas_view, ignore_cable) is None
            return self.field_cable_at_port(port_num, canvas_view, ignore_cable) is None
        return not self.cables_at_port(port_num, canvas_view, ignore_cable)

    def free_port_for_field_cable(self, canvas_view, sfp=False, ignore_cable=None):
        if sfp:
            if not self.sfp_port_count:
                return None
            for p in range(1, self.sfp_port_count + 1):
                port_num = self.SFP_PORT_BASE + p
                if self.port_free_for_new_cable(port_num, canvas_view, "CAT6", ignore_cable):
                    return port_num
            return None
        if not self.port_count:
            return None
        for p in range(1, self.port_count + 1):
            if self.port_free_for_new_cable(p, canvas_view, "CAT6", ignore_cable):
                return p
        return None

    def boundingRect(self):
        rect = QRectF(-self.width/2 - 20, -self.height/2 - 10, self.width + 40, self.height + 30)
        return expand_for_label(self, rect, self.height / 2 + 2.0)

    def shape(self):
        path = QPainterPath()
        path.addRoundedRect(QRectF(-self.width/2, -self.height/2, self.width, self.height), self.radius, self.radius)
        return path

    def paint(self, painter, option, widget=None):
        scene = self.scene()
        if scene and not getattr(scene, "global_show_icons", True):
            return

        lod = option.levelOfDetailFromTransform(painter.worldTransform())
        scale = 1.0 / lod if lod > 0 else 1.0

        # Colors based on type
        accent_color = QColor("#3b82f6") if self.category == "switch" else QColor("#8b5cf6") # Switch (Blue) vs NVR (Purple)
        if self.isSelected():
            accent_color = QColor("#60a5fa") # Selected light blue

        painter.save()
        # Everything from here to restore() is drawn at natural size -- the port squares
        # and drive bays below are literal coordinates picked against a 32x20 icon, so
        # they only follow the canvas icon scale if the painter carries it.
        screen = scale / paint_at_icon_scale(painter, self)
        # `screen` keeps a quantity a constant number of pixels on screen despite
        # the painter scale above -- stroke weights and pixel-sized details use it,
        # while the icon's own geometry uses natural coordinates and grows.

        # Draw main box
        pen = QPen(accent_color, 2.0 * screen)
        painter.setPen(pen)
        painter.setBrush(QBrush(QColor("#111118")))
        painter.drawRoundedRect(QRectF(-self.design_width/2, -self.design_height/2,
                                       self.design_width, self.design_height),
                                self.design_radius, self.design_radius)

        # Draw icon details based on category
        if self.category == "switch":
            # Draw tiny ports
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(accent_color))
            # 4 tiny squares for ports
            # Part of the drawing, in natural units: they keep their proportions, so
            # the icon looks like itself at any size AND at any zoom. Sizing them to a
            # constant number of screen pixels instead makes them grow in scene units
            # as you zoom out until they spill outside the icon that contains them.
            pw = 3.0
            ph = 2.0
            painter.drawRect(-10, -3, pw, ph)
            painter.drawRect(-4, -3, pw, ph)
            painter.drawRect(2, -3, pw, ph)
            painter.drawRect(8, -3, pw, ph)
            
            painter.drawRect(-10, 2, pw, ph)
            painter.drawRect(-4, 2, pw, ph)
            painter.drawRect(2, 2, pw, ph)
            painter.drawRect(8, 2, pw, ph)
        else: # NVR
            # Draw horizontal drive bays
            painter.setPen(QPen(accent_color, 1.0 * screen))
            painter.drawLine(-12, -4, 12, -4)
            painter.drawLine(-12, 1, 12, 1)
            painter.drawLine(-12, 6, 12, 6)
            
            # Tiny power LED
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(QColor("#22c55e")))
            painter.drawEllipse(-12, -8, 2.0, 2.0)

        painter.restore()

        # Draw selection outline
        if self.isSelected():
            painter.save()
            sel_pen = QPen(QColor("#60a5fa"))
            sel_pen.setStyle(Qt.DashLine)
            sel_pen.setWidthF(1.0 * scale)
            painter.setPen(sel_pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(QRectF(-self.width/2 - 4 * scale, -self.height/2 - 4 * scale, 
                                           self.width + 8 * scale, self.height + 8 * scale), 
                                    self.radius + 2 * scale, self.radius + 2 * scale)
            painter.restore()

        # Draw Label
        draw_item_label(painter, label_anchor(self, self.height / 2 + 2.0), self.label, scale)

        # Oversubscription warning badge -- too many cameras for the PoE ports available,
        # or the connected cameras' combined draw exceeds the rated PoE budget.
        self._update_connectivity_warnings()
        if self._connectivity_warnings:
            badge_pt = QPointF(self.width / 2 - 2.0, -self.height / 2 - 2.0)
            draw_warning_badge(painter, badge_pt, scale)

    def _canvas_view(self):
        scene = self.scene()
        if scene:
            views = scene.views()
            if views:
                return views[0]
        return None

    # Validation walks the whole power chain, which is far too expensive to redo on
    # every repaint -- at real project sizes that alone capped the frame rate and made
    # dragging feel broken. Paint reuses the last result and only re-asks a few times a
    # second; anything that actually changes the design calls
    # CanvasView.refresh_connectivity_badges(), which forces an immediate recheck.
    WARNING_REFRESH_SECONDS = 0.25

    def invalidate_connectivity_warnings(self):
        self._warnings_checked_at = None

    def _update_connectivity_warnings(self):
        if getattr(self, "is_preview", False):
            return  # the ghost trailing the cursor is not part of the design yet
        now = time.monotonic()
        if (self._warnings_checked_at is not None
                and now - self._warnings_checked_at < self.WARNING_REFRESH_SECONDS):
            return
        self._warnings_checked_at = now
        view = self._canvas_view()
        warnings = view.get_device_connectivity_warnings(self) if view is not None and hasattr(view, "get_device_connectivity_warnings") else []
        self._connectivity_warnings = warnings
        self.setToolTip("\n".join(warnings))

    def itemChange(self, change, value):
        if change == QGraphicsObject.ItemPositionChange and self.scene():
            scene = self.scene()
            if hasattr(scene, "on_item_property_changed"):
                from PySide6.QtCore import QTimer
                QTimer.singleShot(0, lambda: scene.on_item_property_changed(self))
        return super().itemChange(change, value)
