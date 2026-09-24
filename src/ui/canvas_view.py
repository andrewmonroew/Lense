import math
import re
from PySide6.QtWidgets import (QApplication, QGraphicsView, QGraphicsScene, QInputDialog, QMessageBox,
                             QGraphicsPixmapItem, QGraphicsItem, QGraphicsLineItem, QGraphicsPathItem, QDialog,
                             QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QComboBox, QPushButton,
                             QWidget, QMenu, QGraphicsEllipseItem)
from PySide6.QtCore import Qt, QPointF, QRectF, QLineF, Signal
from PySide6.QtGui import (QPainter, QPixmap, QPen, QColor, QBrush, QCursor, QKeySequence,
                           QPainterPath, QInputDevice)

from src.graphics.floorplan_item import FloorPlanItem
from src.graphics.camera_item import CameraItem
from src.graphics.device_factory import make_device_item
from src.graphics.cable_item import CableItem, recalculate_all_cable_offsets
from src.graphics.calibration_line import CalibrationLine
from src.graphics.custom_item import CustomItem
from src.graphics.label_item import LabelItem
from src.graphics.zone_item import ZoneItem
from src.graphics.rack_item import RackItem, RackSlot
from src.core.utils import (distance, format_distance_both, closest_point_on_segment,
                            point_hits_item, segment_intersection,
                            CATALOG_SPEC_MIME_TYPE, ZOOM_STEP)
from src.core import perf, poe_chain, zoom_input
from src.graphics.icon_scale import (
    DEFAULT_ICON_SCALE, MAX_ICON_SCALE, MIN_ICON_SCALE, clamp_icon_scale)


def _confirm_rack_delete_enabled():
    """Whether deleting a populated rack asks first (Settings > Behavior).

    Imported lazily: settings_dialog reaches back into the canvas to apply a scale, so
    importing it up here would close the loop.
    """
    from src.ui.settings_dialog import KEY_CONFIRM_RACK_DELETE, get_bool
    return get_bool(KEY_CONFIRM_RACK_DELETE)

# Friendly auto-label prefixes for newly-placed devices, keyed by catalog category --
# anything not listed falls back to the raw category string uppercased (unchanged
# behavior for switch/nvr/patch-panel, which read fine as-is: "SWITCH 1", "NVR 1").
DEVICE_LABEL_PREFIXES = {"access-point": "Access Point", "drop": "Wall Drop"}


ZONE_CLOSE_SNAP_RADIUS = 20.0  # how close a click needs to land to the polygon's start point to close it


def _device_label_prefix(spec):
    category = spec.get("category")
    if category == "misc":
        # A generic "MISC 1"/"MISC 2" label defeats the point once there's more than
        # one appliance type on the canvas -- the model name (TV, Toaster, ...) is
        # what actually identifies one of these.
        return spec.get("model") or "Device"
    return DEVICE_LABEL_PREFIXES.get(category, (category or "device").upper())

# Calibration Dialog
class CalibrationDialog(QDialog):
    def __init__(self, pixel_dist, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Set Real-world Distance")
        self.layout = QVBoxLayout(self)

        self.layout.addWidget(QLabel(f"Selected length: {pixel_dist:.0f} pixels"))
        self.layout.addWidget(QLabel("Enter the real-world distance between these points:"))

        # Input row
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        
        self.dist_input = QLineEdit()
        self.dist_input.setPlaceholderText("Distance")
        self.dist_input.setText("10.0")
        
        self.unit_combo = QComboBox()
        self.unit_combo.addItems(["feet", "meters"])
        
        row_layout.addWidget(self.dist_input)
        row_layout.addWidget(self.unit_combo)
        self.layout.addWidget(row)

        # Buttons
        btns = QWidget()
        btn_layout = QHBoxLayout(btns)
        btn_layout.setContentsMargins(0, 10, 0, 0)
        
        self.apply_btn = QPushButton("Apply")
        self.apply_btn.clicked.connect(self.accept)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.reject)
        
        btn_layout.addStretch()
        btn_layout.addWidget(self.cancel_btn)
        btn_layout.addWidget(self.apply_btn)
        self.layout.addWidget(btns)

    def get_values(self):
        try:
            val = float(self.dist_input.text())
        except ValueError:
            val = 10.0
        return val, self.unit_combo.currentText()


class CanvasScene(QGraphicsScene):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.scale_ratio = None # Pixels per foot
        self.calibration_unit = "feet"
        self.calibration_dist = 0.0
        self.calibration_p1 = None
        self.calibration_p2 = None

        # Master visibility toggles (the Visibility bar) -- decluttering switches that
        # apply on top of each item's own per-item visualization settings.
        self.global_show_icons = True
        self.global_show_fov = True
        self.global_show_cables = True

        # How large equipment icons are drawn relative to their natural size. Icons are
        # fixed-size scene geometry while a floor plan can be any resolution, so on a
        # large scan they come out as unreadable specks -- see graphics/icon_scale.py.
        self.global_icon_scale = DEFAULT_ICON_SCALE

        # Current view zoom, published here by CanvasView so items can size their
        # bounding rects to cover labels and warning badges, which render at a constant
        # on-screen size and so grow in scene units as you zoom out.
        self.view_lod = 1.0

        # Which cable bundle is currently fanned open, as the frozenset of cable ids
        # sharing it. Exactly one at a time; see recalculate_all_cable_offsets.
        self.open_bundle = None

        # "latched" (open one bundle fully until you leave) or "dynamic" (the original
        # proportional spread). Settings > Canvas.
        self.fanout_mode = "latched"

        # Set while a drag holds back a Properties-panel rebuild; see
        # on_item_property_changed / flush_deferred_property_change.
        self._deferred_property_item = None

        # Memoized build_network_topology result, keyed by a cheap content signature.
        self._topology_cache = None
        self._switched_edges_cache = None

        # Optional grid: a visual overlay (show_grid) and a placement/move aid
        # (snap_to_grid) -- independent toggles, since you might want the visual
        # reference without forcing every placement onto it, or vice versa.
        self.show_grid = False
        self.snap_to_grid = False
        self.grid_size = 50.0  # scene units, used when uncalibrated or once user-adjusted
        # True until the density spinbox is touched -- lets a calibrated project
        # default to a 1-real-world-foot grid (far more useful than an arbitrary pixel
        # count) while still letting the user dial in any spacing they want afterward.
        self.grid_size_is_default = True

    def effective_grid_size(self):
        if self.grid_size_is_default and self.scale_ratio:
            return self.scale_ratio
        return self.grid_size

    def drawBackground(self, painter, rect):
        super().drawBackground(painter, rect)
        if not self.show_grid:
            return
        size = self.effective_grid_size()
        if not size or size <= 0:
            return

        pen = QPen(QColor(255, 255, 255, 28))
        pen.setCosmetic(True)  # constant 1px on screen at any zoom level
        painter.setPen(pen)

        left = rect.left() - (rect.left() % size)
        top = rect.top() - (rect.top() % size)

        x = left
        while x < rect.right():
            painter.drawLine(QLineF(x, rect.top(), x, rect.bottom()))
            x += size
        y = top
        while y < rect.bottom():
            painter.drawLine(QLineF(rect.left(), y, rect.right(), y))
            y += size

    def on_item_property_changed(self, item):
        # Broadcast changes to main window / properties panel.
        #
        # Every pixel of a drag is an ItemPositionChange, and the listener on the other
        # end rebuilds the whole Properties panel -- which for a switch means walking
        # the cable graph for its clients and re-running PoE validation. Doing that
        # forty times over a forty-pixel drag is what made icons trail behind the
        # cursor. While a mouse button is down the rebuild is deferred to the release,
        # which is the only moment its answer can be final anyway.
        if QApplication.mouseButtons():
            self._deferred_property_item = item
            return
        self._emit_property_changed(item)

    def _emit_property_changed(self, item):
        parent_view = self.views()[0] if self.views() else None
        if parent_view and hasattr(parent_view, "item_property_changed"):
            parent_view.item_property_changed.emit(item)

    def flush_deferred_property_change(self):
        """Delivers the rebuild held back during a drag. Called on mouse release."""
        views = self.views()
        if views and hasattr(views[0], "relayout_labels"):
            views[0].relayout_labels()   # whatever just moved may have freed or blocked a row
        item = self._deferred_property_item
        self._deferred_property_item = None
        if item is not None and item.scene() is self:
            self._emit_property_changed(item)


class CanvasView(QGraphicsView):
    # Signals
    item_selected = Signal(object)      # Selected QGraphicsItem or None
    zoom_changed = Signal(float)       # Zoom percentage
    coords_changed = Signal(QPointF)    # Cursor coordinates in world space
    scale_changed = Signal(str)         # Calibration string
    item_property_changed = Signal(object) # Property modified
    rack_open_requested = Signal(object)   # A rack wants its editor tab opened/focused

    def __init__(self, parent=None):
        super().__init__(parent)
        self.scene_obj = CanvasScene(self)
        self.setScene(self.scene_obj)
        self.scene_obj.setBackgroundBrush(QBrush(QColor("#141420")))

        # View settings
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform | QPainter.TextAntialiasing)
        # Smart, not Full: repainting the whole viewport on every change re-scales the
        # entire floor plan bitmap through SmoothPixmapTransform, which on a large scan
        # is the single biggest cost of dragging anything. Full was previously masking
        # items painting outside their bounding rects -- see _sync_view_lod, which is
        # what makes partial updates safe.
        self.setViewportUpdateMode(QGraphicsView.SmartViewportUpdate)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        # Matches the "select" branch of set_tool() -- active_tool starts as "select"
        # (below) so the drag mode should match from the very first frame, not just
        # after the user's first explicit tool switch.
        self.setDragMode(QGraphicsView.RubberBandDrag)
        # wheelEvent below does its own cursor-anchored zoom math (scale, then measure
        # the drift and translate it back out) -- QGraphicsView's default anchor mode
        # (AnchorViewCenter) fights that by re-centering scale()/translate() calls on
        # the *viewport center* instead, which silently cancels most of the correction
        # and makes zoom drift away from the cursor the longer you scroll. NoAnchor
        # disables that built-in re-centering so the manual math is the only thing
        # steering the transform.
        self.setTransformationAnchor(QGraphicsView.NoAnchor)
        self.setResizeAnchor(QGraphicsView.NoAnchor)
        # Without this, Qt only delivers mouseMoveEvent while a button is held down —
        # cable drawing needs move events on a free-floating cursor between clicks.
        self.setMouseTracking(True)

        # State
        self.active_tool = "select" # select, pan, calibrate, camera, cable, infra, measure, place
        # Kept in sync by MainWindow._apply_project_mode -- lets graphics items (e.g.
        # CameraItem) read the current project type without needing a MainWindow
        # reference, just the CanvasView they're already able to reach via scene().
        self.project_type = "cctv"
        self.selected_catalog_id = None
        self.selected_catalog_spec = None
        # Ghost of the item the armed tool will place, trailing the cursor.
        self.hand_preview = None

        # Zoom speed per kind of device (Settings > Navigation). Separate so a touchpad
        # can be slowed down without touching a mouse wheel that already feels right.
        # Drawing one run to a multi-gang plate spawns the rest; see
        # spawn_multidrop_runs. Overridden from Settings at startup.
        self.multidrop_auto_cables = True
        self.zoom_sensitivity_mouse = zoom_input.DEFAULT_SENSITIVITY
        self.zoom_sensitivity_touchpad = zoom_input.DEFAULT_SENSITIVITY
        self.floorplan_item = None
        self.scale_ratio = None     # Pixels per foot
        self.catalog_data = {}      # spec_id -> spec dict (for the unified Place tool / drag-drop)

        # Enable dropping catalog items directly onto the canvas
        self.setAcceptDrops(True)

        # Interaction helpers
        self.pan_active = False
        self.last_pan_pos = QPointF()
        self.space_pressed = False
        self.last_mouse_scene_pos = QPointF()

        # Calibration tool state
        self.calib_line = None
        self.calib_start_pt = None

        # Cable drawing tool state
        self.cable_draw_points = []
        self.temp_draw_line = None
        # Highlight of the route a Ctrl+click would follow; see follow_bundle_path.
        self.follow_preview_line = None

        # Place tool: click-vs-drag detection, same pattern as the Label/Zone tool --
        # a plain click shows the usual equipment menu for a single placement, a
        # click-and-drag instead previews a line and, on release, asks for a catalog
        # item and a quantity to lay out evenly along it.
        self.place_press_pt = None
        self.place_press_screen_pt = None
        self.place_dragging_line = False
        self.temp_place_line = None

        # Cable vertex-editing state (double-click a cable with the Select/Move tool)
        self.editing_cable = None

        # Zone polygon drawing tool state (Label/Zone tool)
        self.zone_draw_points = []
        self.temp_zone_line = None
        self.temp_zone_start_marker = None  # highlights the polygon's start point once closing it is possible

        # Label/Zone tool: click-vs-drag detection on the very first press (before any
        # zone_draw_points exist) -- a plain click keeps the existing "Add Text Label /
        # Draw Zone Polygon" menu, a click-and-drag quick-draws a rectangular bounding
        # box instead. See mousePressEvent/mouseMoveEvent/mouseReleaseEvent.
        self.labelzone_press_pt = None
        self.labelzone_press_screen_pt = None
        self.labelzone_dragging_box = False

        # Zone vertex-editing state (double-click a zone with the Select/Move tool)
        self.editing_zone = None

        # Measurement tool state
        self.measure_line = None
        self.measure_start_pt = None

        # Lasso tool state -- drag a line across a fan of cables to gather them into
        # one bundled crossing point (see apply_lasso).
        self.lasso_line = None
        self.lasso_start_pt = None

        # Whether the Canvas sheet tab is the one actually visible right now (kept in
        # sync by MainWindow via sheet_tabs.currentChanged). Qt's keyboard focus can
        # drift back here even while a different tab (a Rack Editor, say) is what's
        # actually on screen -- rather than chase every way that can happen, anything
        # destructive gated on this flag simply refuses to act unless the user can
        # actually see the Canvas to have meant it. See delete_selected().
        self.is_active_tab = True

        # Undo/redo hooks -- no-ops until MainWindow replaces them with real callables
        # once UndoManager exists (avoids CanvasView importing MainWindow). Also used
        # directly by RackEditorPanel via its own canvas_view reference.
        self.push_undo_snapshot = lambda: None       # simple case: capture + commit now
        self.capture_undo_state = lambda: None        # just capture, for the drag begin/commit split
        self.commit_undo_state = lambda state: None   # push a previously-captured state
        self._pending_move_snapshot = None
        self._move_item = None
        self._move_start_geometry = None
        self._move_start_pos = None

        # No-op until MainWindow replaces it -- lets the grid-density spinbox refresh
        # its displayed value when calibration completes and the grid is still
        # following it automatically (see CanvasScene.grid_size_is_default).
        self.on_calibration_changed = lambda: None

        # Handle keyboard/focus
        self.setFocusPolicy(Qt.StrongFocus)

        # Selecting a camera/device highlights every cable connected to it (white,
        # same as a directly-selected cable) -- an orphaned camera with no cable at
        # all becomes obvious at a glance, since nothing lights up when you select it.
        self._highlighted_cables = set()
        self.scene_obj.selectionChanged.connect(self._update_cable_highlights)

    def _update_cable_highlights(self):
        selected_ids = {item.id for item in self.scene_obj.selectedItems()
                         if getattr(item, "object_type", None) in ("camera", "device")}
        new_highlighted = set()
        if selected_ids:
            for cable in self.get_cables():
                # Only the cable's TRUE endpoints (start_device_id/end_device_id) --
                # NOT every entry in vertex_anchors. A multi-vertex cable can have
                # plenty of intermediate waypoints that snapped near some OTHER
                # device's position while it was being routed across the floor plan;
                # those aren't real connections, just cosmetic path points, and
                # matching against them was lighting up nearly every cable in a busy
                # project regardless of what was actually selected.
                if cable.start_device_id in selected_ids or cable.end_device_id in selected_ids:
                    new_highlighted.add(cable)

        for cable in new_highlighted - self._highlighted_cables:
            cable.connected_highlight = True
            cable.update()
        for cable in self._highlighted_cables - new_highlighted:
            cable.connected_highlight = False
            cable.update()
        self._highlighted_cables = new_highlighted

    def set_catalog_data(self, catalog_data):
        self.catalog_data = catalog_data

    # ── Tool selection ──
    def set_tool(self, tool_name, catalog_spec=None):
        # An open bundle belongs to hovering with the select tool; leaving that tool
        # should let it settle back rather than freezing it open behind whatever you do
        # next. (The recalc below is what actually redraws it.)
        if tool_name != "select" and getattr(self.scene_obj, "open_bundle", None) is not None:
            self.scene_obj.open_bundle = None
            recalculate_all_cable_offsets(self.scene_obj)

        # Clean up previous tool states
        self.cleanup_temp_shapes()
        if tool_name != "select":
            self.set_editing_cable(None)
            self.set_editing_zone(None)

        self.active_tool = tool_name
        self.selected_catalog_spec = catalog_spec
        self.update_hand_preview()

        # Set appropriate cursor
        if tool_name == "pan":
            self.setCursor(Qt.OpenHandCursor)
            self.setDragMode(QGraphicsView.NoDrag)
        elif tool_name in ["calibrate", "camera", "cable", "fiber", "infra", "measure", "place", "labelzone", "lasso"]:
            self.setCursor(Qt.CrossCursor)
            self.setDragMode(QGraphicsView.NoDrag)
        else: # select
            self.setCursor(Qt.ArrowCursor)
            # RubberBandDrag only kicks in when the press starts on empty canvas --
            # pressing on an item still drags/moves it (and the whole selection along
            # with it, if that item is part of a multi-selection) exactly as before.
            self.setDragMode(QGraphicsView.RubberBandDrag)

    def cleanup_temp_shapes(self):
        # Remove calibration line
        if self.calib_line:
            self.scene_obj.removeItem(self.calib_line)
            self.calib_line = None
        self.calib_start_pt = None

        # Remove temp cable items
        if self.temp_draw_line:
            self.scene_obj.removeItem(self.temp_draw_line)
            self.temp_draw_line = None
        self.clear_follow_preview()
        self.cable_draw_points.clear()

        # Remove temp zone polygon items
        if self.temp_zone_line:
            self.scene_obj.removeItem(self.temp_zone_line)
            self.temp_zone_line = None
        if self.temp_zone_start_marker:
            self.scene_obj.removeItem(self.temp_zone_start_marker)
            self.temp_zone_start_marker = None
        self.zone_draw_points.clear()

        # Reset Label/Zone click-vs-drag tracking
        self.labelzone_press_pt = None
        self.labelzone_press_screen_pt = None
        self.labelzone_dragging_box = False

        # Remove temp Place-tool line preview
        if self.temp_place_line:
            self.scene_obj.removeItem(self.temp_place_line)
            self.temp_place_line = None
        self.place_press_pt = None
        self.place_press_screen_pt = None
        self.place_dragging_line = False

        # Remove lasso overlay
        if self.lasso_line:
            self.scene_obj.removeItem(self.lasso_line)
            self.lasso_line = None
        self.lasso_start_pt = None

        # Remove measure overlay
        if self.measure_line:
            self.scene_obj.removeItem(self.measure_line)
            self.measure_line = None
        self.measure_start_pt = None

    # ── Database Loaders ──
    def used_labels(self):
        """Every label currently in use, including equipment mounted inside racks."""
        labels = {getattr(item, "label", None) for item in self.scene_obj.items()}
        labels.update(device.label for device in self.get_network_devices())
        labels.discard(None)
        return labels

    def next_free_label(self, prefix, used=None):
        """The lowest unused "<prefix> N".

        Numbering used to come from the TOTAL count of devices on the canvas, so the
        first wall drop dropped onto a design that already had six other devices was
        christened "Wall Drop 7". Counting only same-prefix labels is still not enough
        on its own -- delete Camera 2 of three and a count-based name collides with the
        existing Camera 3 -- so this looks for the lowest number nobody is using.
        """
        used = self.used_labels() if used is None else used
        number = 1
        while f"{prefix} {number}" in used:
            number += 1
        return f"{prefix} {number}"

    def next_device_label(self, spec, used=None):
        """The next free auto-label for a device of this catalog spec's kind."""
        return self.next_free_label(_device_label_prefix(spec), used)

    def get_cameras(self):
        return [item for item in self.scene_obj.items() if getattr(item, "object_type", None) == "camera"]

    def get_network_devices(self):
        """Every switch/NVR, whether it's sitting loose on the floor plan or mounted
        inside a rack -- mounted devices are deliberately not added to the scene
        directly (that's the whole point of a rack: they're hidden from the floor
        plan), so callers that need the complete device list (inventory, connectivity
        checks, the object explorer) go through here rather than scene_obj.items()."""
        loose = [item for item in self.scene_obj.items() if getattr(item, "object_type", None) == "device"]
        racked = [slot.device for rack in self.get_racks() for slot in rack.slots]
        return loose + racked

    def get_patch_panels(self):
        return [d for d in self.get_network_devices() if getattr(d, "category", None) == "patch-panel"]

    def get_pass_through_devices(self):
        return [d for d in self.get_network_devices() if d.is_pass_through()]

    def get_cables(self):
        return [item for item in self.scene_obj.items() if getattr(item, "object_type", None) == "cable"]

    def get_custom_objects(self):
        return [item for item in self.scene_obj.items() if getattr(item, "object_type", None) == "custom"]

    def get_labels(self):
        return [item for item in self.scene_obj.items() if getattr(item, "object_type", None) == "label"]

    def get_zones(self):
        return [item for item in self.scene_obj.items() if getattr(item, "object_type", None) == "zone"]

    def get_racks(self):
        return [item for item in self.scene_obj.items() if getattr(item, "object_type", None) == "rack"]

    def find_rack_containing_device(self, device_id):
        for rack in self.get_racks():
            if rack.find_slot(device_id) is not None:
                return rack
        return None

    def select_and_focus_item(self, item):
        """Selects item and centers the view on it -- used by the Object Explorer so
        clicking an entry in a dense real design actually finds it on the canvas,
        not just highlights it somewhere off-screen. A device mounted inside a rack
        isn't a top-level scene item (nothing to center a view on), so this opens its
        rack's editor instead -- the closest equivalent of "jumping to it"."""
        if item.scene() is None and getattr(item, "object_type", None) == "device":
            rack = self.find_rack_containing_device(item.id)
            if rack is not None:
                self.scene_obj.clearSelection()
                rack.setSelected(True)
                self.centerOn(rack)
                self.item_selected.emit(rack)
                self.open_rack_editor(rack)
            return
        self.scene_obj.clearSelection()
        if hasattr(item, "setSelected"):
            item.setSelected(True)
        self.centerOn(item)
        self.item_selected.emit(item)

    # ── Connection topology (derived from cables, not just the connected_device_id
    # bookkeeping field -- a camera<->switch connection made by hand-drawing a cable
    # doesn't set that field, only connect_camera_to_device via the sidebar dropdown
    # does, so anything that only checked connected_device_id could silently show
    # "not connected" for a cable that's plainly right there on the canvas.) ──
    def get_cameras_connected_to_device(self, device_id):
        """All cameras that ultimately reach device_id -- directly, or by walking
        through however many patch panels sit in between (see resolve_camera_chain).
        A camera physically terminated at a patch panel that ISN'T (yet) patched
        through to a real switch/NVR is correctly excluded here."""
        connected = []
        for cam in self.get_cameras():
            chain = self.resolve_camera_chain(cam.id)
            if chain["device"] is not None and chain["device"].id == device_id:
                connected.append(cam)
        return connected

    def get_device_connected_to_camera(self, camera_id):
        """The switch/NVR camera_id ultimately reaches, if any (walking through any
        patch panel(s) in between)."""
        chain = self.resolve_camera_chain(camera_id)
        return chain["device"].id if chain["device"] is not None else None

    def resolve_camera_chain(self, camera_id):
        """Walks a camera's field cable to wherever it ultimately terminates: straight
        to a switch/NVR (single hop, same as a direct connection always worked), or
        through one or more patch panels by following whatever patch cable is plugged
        into the SAME port on the far side of each one. Loop-guarded against patch
        panels accidentally patched back into each other.

        Returns {"device": <switch/NVR item or None>, "port": <port on that final
        device, or None -- switches have real port grids too now, not just patch
        panels>, "via_panel": <PatchPanelItem or None>, "via_port": <int or None>} --
        via_panel/via_port describe the FIRST patch panel hop (nearest the camera,
        i.e. where its field cable actually lands), for a "Switch 1, Port 6 (via
        Patch Panel P1, Port 6)" style label."""
        empty = {"device": None, "port": None, "via_panel": None, "via_port": None}
        cam = self.find_device_or_camera_by_id(camera_id)
        if cam is None:
            return empty

        # Explicit dropdown bookkeeping takes precedence, same as before -- but only
        # if it resolves to a real switch/NVR; a patch panel was never a valid target
        # for that dropdown, so ignore it if it somehow points to one.
        if getattr(cam, "connected_device_id", None):
            dev = self.find_device_or_camera_by_id(cam.connected_device_id)
            if dev is not None and getattr(dev, "object_type", None) == "device" and not dev.is_pass_through():
                return {"device": dev, "port": None, "via_panel": None, "via_port": None}

        other_id, other_port = self._other_field_cable_end(camera_id)
        if not other_id:
            return empty
        other = self.find_device_or_camera_by_id(other_id)
        if other is None or getattr(other, "object_type", None) != "device":
            return empty
        if not other.is_pass_through():
            return {"device": other, "port": other_port, "via_panel": None, "via_port": None}

        first_panel, first_port = other, other_port
        visited_panels = {other.id}
        current_panel, current_port = other, other_port

        for _ in range(len(self.get_pass_through_devices()) + 1):  # loop-safe bound
            if current_port is None:
                return empty
            next_id, next_port = self._other_patch_cable_end(current_panel.id, current_port)
            if not next_id:
                return empty
            next_item = self.find_device_or_camera_by_id(next_id)
            if next_item is None or getattr(next_item, "object_type", None) != "device":
                return empty
            if not next_item.is_pass_through():
                return {"device": next_item, "port": next_port, "via_panel": first_panel, "via_port": first_port}
            if next_item.id in visited_panels:
                return empty  # patch panels looped back into each other
            visited_panels.add(next_item.id)
            current_panel, current_port = next_item, next_port

        return empty

    def _other_field_cable_end(self, item_id):
        """The far end (id, port) of the non-patch cable anchored to item_id, if any."""
        for cable in self.get_cables():
            if cable.cable_type == "Patch":
                continue
            anchors = cable.vertex_anchors
            if not anchors:
                continue
            endpoints = (anchors[0], anchors[-1])
            if item_id not in endpoints:
                continue
            if endpoints[0] == item_id:
                other_id, other_port = endpoints[1], cable.end_port
            else:
                other_id, other_port = endpoints[0], cable.start_port
            if not other_id or other_id == item_id:
                continue
            return other_id, other_port
        return None, None

    def _other_patch_cable_end(self, panel_id, port_num):
        """The far end (id, port) of whatever patch cable is plugged into port_num on
        the patch panel panel_id, if any."""
        for cable in self.get_cables():
            if cable.cable_type != "Patch":
                continue
            anchors = cable.vertex_anchors
            if not anchors:
                continue
            endpoints = (anchors[0], anchors[-1])
            if panel_id not in endpoints:
                continue
            start_matches = endpoints[0] == panel_id
            this_port = cable.start_port if start_matches else cable.end_port
            if this_port != port_num:
                continue
            other_id = endpoints[1] if start_matches else endpoints[0]
            other_port = cable.end_port if start_matches else cable.start_port
            if not other_id or other_id == panel_id:
                continue
            return other_id, other_port
        return None, None

    def get_switched_network_peers(self, device_id):
        """Every device reachable from device_id over device-to-device links.

        Power and data are different relations and have to be tracked separately. A
        camera draws PoE from whatever switch it is plugged into (that's
        get_cameras_connected_to_device, and it's what the PoE budget math must keep
        using), but for *recording* purposes it is on the NVR's network as soon as its
        switch uplinks to that NVR. Walking the switch fabric is what makes an NVR see
        cameras that are two hops away instead of reporting none at all.
        """
        devices = {d.id: d for d in self.get_network_devices()}
        if device_id not in devices:
            return []

        edges = self._switched_edges()

        seen = {device_id}
        queue = [device_id]
        while queue:
            current = queue.pop()
            for neighbor in edges.get(current, ()):
                if neighbor not in seen:
                    seen.add(neighbor)
                    queue.append(neighbor)
        return [devices[d_id] for d_id in seen if d_id != device_id]

    def _switched_edges(self):
        """Device-to-device adjacency across the whole cable plant, memoized.

        get_cameras_on_network_of resolves a chain per camera and each of those walked
        this graph from scratch, so selecting one switch rebuilt it dozens of times. It
        depends on exactly the facts the topology signature already covers.
        """
        signature = self._topology_signature()
        cached = self.scene_obj._switched_edges_cache
        if cached is not None and cached[0] == signature:
            return cached[1]

        edges = {d.id: set() for d in self.get_network_devices()}
        for cable in self.get_cables():
            anchors = cable.vertex_anchors
            if not anchors:
                continue
            # Resolve each end through any passive jacks in between, so a run that
            # crosses a patch panel or wall drop still counts as one link.
            a = self._resolve_through_pass_throughs(anchors[0], cable.start_port, arriving_cable=cable)
            b = self._resolve_through_pass_throughs(anchors[-1], cable.end_port, arriving_cable=cable)
            if a and b and a != b and a in edges and b in edges:
                edges[a].add(b)
                edges[b].add(a)

        self.scene_obj._switched_edges_cache = (signature, edges)
        return edges

    def rack_uplink_device(self, rack):
        """The mounted device a cable anchored to the RACK itself should be read as
        landing on.

        Cables drawn on the floor plan terminate on the rack icon, not on any one piece
        of equipment inside it (complete_cable_drawing anchors them that way on purpose
        -- which slot it patches into is the Rack Editor's business, not the floor
        plan's). But a rack is not a network device, so anything reading the cable graph
        saw a dead end: a trunk run from a switch into the rack holding the aggregation
        switch simply vanished, and every device on the far side looked like an
        unconnected island hanging straight off the WAN in the Network Diagram.

        Resolve to the most plausible aggregation point instead -- a switch first (that's
        what other equipment trunks into), preferring the one with the most ports, then
        an NVR, then anything else mounted. Passive jacks are skipped: a patch panel is
        how a run reaches equipment, never the thing it's actually connected to."""
        candidates = [slot.device for slot in getattr(rack, "slots", [])
                      if slot.device is not None
                      and getattr(slot.device, "object_type", None) == "device"
                      and not slot.device.is_pass_through()]
        if not candidates:
            return None

        def rank(dev):
            category = (dev.spec or {}).get("category")
            order = 0 if category == "switch" else (1 if category == "nvr" else 2)
            return (order, -(dev.port_count or 0), getattr(dev, "label", ""))

        return min(candidates, key=rank)

    def _cable_ends_at(self, item_id, exclude=None):
        """Every (cable, end_index) anchored to item_id, optionally ignoring one cable."""
        found = []
        for cable in self.get_cables():
            if cable is exclude:
                continue
            anchors = cable.vertex_anchors
            if not anchors:
                continue
            if anchors[0] == item_id:
                found.append((cable, 0))
            if len(anchors) > 1 and anchors[-1] == item_id:
                found.append((cable, -1))
        return found

    def _resolve_through_pass_throughs(self, item_id, port, arriving_cable=None):
        """Follows patch panels / wall drops -- and rack icons -- to the real device on
        the far side.

        `arriving_cable` is the run we came in on, so it can be excluded when the jack
        has to be crossed by cable rather than by port number."""
        current_id, current_port = item_id, port
        # +1 for the rack hop, which can only ever happen once (rack_uplink_device
        # always lands on a mounted device, never on another rack).
        for _ in range(len(self.get_pass_through_devices()) + 2):
            item = self.find_device_or_camera_by_id(current_id)
            if item is None:
                return current_id
            if getattr(item, "object_type", None) == "rack":
                device = self.rack_uplink_device(item)
                if device is None:
                    return None
                current_id, current_port = device.id, None
                continue
            if getattr(item, "object_type", None) != "device":
                return current_id
            if not item.is_pass_through():
                return current_id
            if current_port is None:
                # Ports only get assigned in the Rack Editor, so a wall drop cabled up
                # on the floor plan has none on either side -- and giving up here meant
                # every run crossing one vanished from the graph, leaving whole branches
                # of the network floating as their own roots. A jack with exactly one
                # OTHER cable on it is unambiguous no matter what the ports say: that
                # cable is where the run continues. Two or more and it genuinely can't
                # be told which, so stop rather than guess wrong.
                others = self._cable_ends_at(current_id, exclude=arriving_cable)
                if len(others) != 1:
                    return None
                cable, end_index = others[0]
                far = -1 if end_index == 0 else 0
                arriving_cable = cable
                current_id = cable.vertex_anchors[far]
                current_port = cable.start_port if far == 0 else cable.end_port
                continue
            current_id, current_port = self._other_patch_cable_end(current_id, current_port)
            if not current_id:
                return None
        return current_id

    def get_client_devices_of(self, device_id):
        """Non-camera equipment hanging off this device -- access points, misc
        appliances, downstream switches.

        A switch's properties used to list only cameras, so a residential design where
        the interesting loads are APs and TVs showed "no cameras connected" on a switch
        that was in fact feeding three of them. Direction comes from the same walk the
        Network Diagram uses, so a switch's own uplink never shows up as one of its
        clients.
        """
        def find(node):
            if node["device"].id == device_id:
                return node
            for child in node["children"]:
                hit = find(child)
                if hit is not None:
                    return hit
            return None

        for root in self.build_network_topology():
            node = find(root)
            if node is not None:
                return [child["device"] for child in node["children"]]
        return []

    def get_cameras_on_network_of(self, device_id):
        """Cameras this device can see over the switched network, including ones
        powered by a different switch that uplinks to it. Excludes cameras already
        attached directly, so the two lists don't double-count."""
        direct_ids = {c.id for c in self.get_cameras_connected_to_device(device_id)}
        peer_ids = {d.id for d in self.get_switched_network_peers(device_id)}
        if not peer_ids:
            return []
        cameras = []
        for cam in self.get_cameras():
            if cam.id in direct_ids:
                continue
            chain = self.resolve_camera_chain(cam.id)
            if chain["device"] is not None and chain["device"].id in peer_ids:
                cameras.append(cam)
        return cameras

    def get_camera_connectivity_warning(self, camera):
        """None if camera is wired to a switch/NVR and adequately powered, else a short
        warning string -- surfaced as a canvas badge so an unwired or under-powered
        camera doesn't get missed on a real design with dozens of cameras."""
        if not self.get_device_connected_to_camera(camera.id):
            return "Not connected to a switch/NVR"
        # Wired up, but the power path still has to actually reach it with enough juice.
        poe_issues = poe_chain.evaluate_device(self, camera)
        if poe_issues:
            return "\n".join(message for _severity, message in poe_issues)
        return None

    def get_device_connectivity_warnings(self, device):
        """Short warning strings for a switch/NVR that's oversubscribed -- more
        cameras hung off it than it has PoE ports for, or the cameras' combined
        power draw exceeds its rated PoE budget. Many of these switches have fewer
        PoE ports than total ports, so port count alone isn't a safe proxy.

        Patch panels are exempt -- they're passive, port count is structurally
        enforced by the rack editor (a port either has something plugged in or it
        doesn't; there's no way to "oversubscribe" one), and get_cameras_connected_to_
        device deliberately treats a patch panel as a pass-through, not a destination,
        so it would never see any "connected" cameras to warn about anyway."""
        if device.is_pass_through():
            return []

        warnings = []
        connected = self.get_cameras_connected_to_device(device.id)
        spec = device.spec

        # NVR "ports" can be a dict (e.g. {"rj45": 1, "sfp": 1}) rather than a plain
        # count, so only trust it as a numeric port limit when it actually is one.
        poe_ports = spec.get("poePorts") or 0
        total_ports = spec.get("ports")
        total_ports = total_ports if isinstance(total_ports, (int, float)) else 0
        port_limit = poe_ports if poe_ports > 0 else total_ports
        if port_limit and len(connected) > port_limit:
            warnings.append(f"{len(connected)} cameras connected, only {port_limit} PoE ports available")

        # Power budget/class/chain checks come from poe_chain, which walks the whole
        # power path rather than reading this device's poeBudget as a constant -- a
        # PoE-powered switch's real budget depends on how it is itself fed. It also
        # covers every kind of load (APs, appliances, downstream switches), not just
        # the cameras counted above.
        warnings.extend(message for _severity, message in poe_chain.evaluate_device(self, device))

        return warnings

    def refresh_connectivity_badges(self):
        """Forces every camera/device to recompute and repaint its connectivity warning
        badge. Qt only repaints an item when something about *that item* visibly
        changes -- it has no way to know a camera's badge depends on nearby cable
        topology, so deleting/rewiring a cable elsewhere doesn't trigger a repaint of
        the camera on its own. Call this after anything that could change who's wired
        to what or a device's PoE math: drawing/deleting a cable, connecting or
        disconnecting via the sidebar dropdown, deleting a camera/device, or swapping
        either one's catalog model (a heavier camera or a smaller switch can flip a
        budget/port warning without touching any cable at all)."""
        for cable in self.get_cables():
            cable.invalidate_length()      # allowances or endpoints may have changed
        for item in self.get_cameras() + self.get_network_devices():
            # Clear the paint-path throttle so the next repaint recomputes immediately
            # rather than showing a stale badge for up to a quarter second.
            item.invalidate_connectivity_warnings()
            item.update()

    # ── Scene-wide geometry changes ──
    # Item types whose boundingRect()/shape() depend on scene-wide values (the icon
    # scale, the zoom, the label font size).
    GEOMETRY_DEPENDENT_TYPES = ("camera", "device", "rack", "custom", "cable")

    @staticmethod
    def _drag_geometry(item):
        """What "this item moved" means, for deciding whether a drag earns an undo entry.

        pos() alone is not enough: a zone is positioned by its own points and its pos()
        never leaves the origin, so a dragged zone looked unmoved and its undo snapshot
        was thrown away.
        """
        points = getattr(item, "points", None)
        return tuple((pt.x(), pt.y()) for pt in points) if points else None

    def apply_scene_geometry_change(self, apply):
        """Runs `apply`, which changes a value every item's bounding rect depends on.

        Qt's contract is prepareGeometryChange() BEFORE the geometry actually changes:
        it drops the item from the scene's spatial index so the new rect is re-inserted
        on next access. Flipping a scene-wide value first and notifying afterwards
        leaves every item indexed under a rect it no longer has -- the BSP tree ends up
        inconsistent and eventually walks itself into a segfault mid-paint, which is
        exactly the crash this caused. Notify everything first, then change the value.
        """
        items = [item for item in self.scene_obj.items()
                 if getattr(item, "object_type", None) in self.GEOMETRY_DEPENDENT_TYPES]
        for item in items:
            item.prepareGeometryChange()
        apply()
        for item in items:
            item.update()

    def relayout_labels(self):
        """Re-runs label collision avoidance. Cheap when nothing needs to move.

        Changing a label's row changes its bounding rect, so the assignment is applied
        through apply_scene_geometry_change -- the scene index has to be told before the
        geometry shifts, not after (see the note there).
        """
        from src.graphics import label_layout
        changes = label_layout.assign_rows(self.scene_obj)
        if not changes:
            return 0

        def assign():
            for item, row in changes.items():
                item.label_row = row
        self.apply_scene_geometry_change(assign)
        return len(changes)

    def set_label_font_size(self, size):
        """Label text size, applied so the scene index learns about the new extents.

        Labels render at a constant on-screen size and their bounding rects are sized
        from the text, so changing this resizes every item's geometry.
        """
        from src.graphics import label_render
        applied = []
        self.apply_scene_geometry_change(
            lambda: applied.append(label_render.set_label_font_size(size)))
        return applied[0] if applied else label_render.label_font_size()

    # ── Icon scale ──
    def set_icon_scale(self, value):
        """Resizes every equipment icon on the canvas. Returns the value actually used.

        Changing the scale changes each item's boundingRect, so Qt has to be told
        before it reads them again -- without prepareGeometryChange() the scene keeps
        the old rects in its spatial index and items smear or vanish when moved.
        """
        value = clamp_icon_scale(value)
        if value == self.scene_obj.global_icon_scale:
            return value
        # Cables included: their click target is a stroked path whose width follows the
        # scale, so their shape() changes too.
        def assign():
            self.scene_obj.global_icon_scale = value
        self.apply_scene_geometry_change(assign)
        # Cable runs keep their endpoints, but line weights, vertex handles and the
        # fan-out spacing of a bundle are all sized in scene units.
        recalculate_all_cable_offsets(self.scene_obj)
        self.scene_obj.update()
        return value

    def icon_scale(self):
        return self.scene_obj.global_icon_scale

    # A device icon reads well at roughly the footprint of a small appliance -- big
    # enough to pick out and click, small enough not to swallow a 10-foot room.
    ICON_TARGET_FEET = 2.5
    NATURAL_DEVICE_WIDTH = 32.0  # DeviceItem's design width, what the target is against
    REFERENCE_PLAN_PX = 1200.0   # plan width the natural icon sizes were chosen against

    def suggested_icon_scale(self):
        """An icon scale that keeps icons readable against the current floor plan.

        Prefers the project's calibration: sizing an icon as a real-world object means
        it holds its proportions against the rooms at every zoom level, the same way
        the floor plan's own features do. Sizing off the image's pixel dimensions
        instead only picks the right answer for one particular zoom -- it targets a
        whole-plan view, so it reads as bloated the moment you zoom in to work, which
        is most of the time.

        Falls back to the pixel heuristic when the project has not been calibrated,
        and leaves the scale alone when there is nothing to measure against at all.
        """
        scale_ratio = self.scene_obj.scale_ratio  # pixels per foot
        if scale_ratio:
            raw = (self.ICON_TARGET_FEET * scale_ratio) / self.NATURAL_DEVICE_WIDTH
        else:
            item = self.floorplan_item
            if item is None:
                return self.icon_scale()
            rect = item.boundingRect()
            longest = max(rect.width(), rect.height())
            if longest <= 0:
                return self.icon_scale()
            raw = longest / self.REFERENCE_PLAN_PX
        # Quarter steps: fine enough to matter, coarse enough to look deliberate.
        return clamp_icon_scale(round(raw * 4.0) / 4.0)

    # ── Network topology (for the Network Diagram tab) ──
    def _topology_signature(self):
        """Everything build_network_topology's answer depends on, and nothing else.

        Deliberately excludes item positions: dragging a switch across the floor plan
        cannot change what it is plugged into, and rebuilding the graph on every pixel
        of a drag is exactly the cost this cache exists to avoid.
        """
        cables = tuple((cable.cable_type, tuple(cable.vertex_anchors),
                        cable.start_port, cable.end_port)
                       for cable in self.get_cables())
        devices = tuple((d.id, d.label, (d.spec or {}).get("category"))
                        for d in self.get_network_devices())
        cameras = tuple((c.id, c.connected_device_id) for c in self.get_cameras())
        racks = tuple((r.id, tuple(slot.device.id for slot in r.slots))
                      for r in self.get_racks())
        return (cables, devices, cameras, racks)

    def build_network_topology(self):
        """Derives the device hierarchy purely from actual cable connections -- never
        from what a device 'should' be wired to -- so the generated diagram always
        matches what's really drawn on the canvas. Returns a list of root-device nodes
        (each {"device", "cameras", "children"}); the diagram view hangs an imaginary
        WAN node above all of them, since there's no WAN object class yet.

        Device-to-device links (e.g. a switch uplinked into another switch or an NVR)
        come from cables anchored device-to-device at both ends; an undirected graph
        is built from those and then walked into a tree via DFS, preferring NVRs as
        roots (conventionally the WAN-facing hub in a CCTV system) since a cable alone
        can't tell us which end is architecturally "upstream". Any device unreachable
        from an NVR becomes its own root, so nothing placed on the canvas goes missing
        from the diagram even if it isn't wired to anything else yet.

        Patch panels are deliberately excluded -- they're how a camera physically
        reaches a switch (see resolve_camera_chain), not a hop in the switch/NVR
        backbone hierarchy this diagram is meant to show, and a camera patched through
        one is already correctly attributed to the switch it resolves to."""
        signature = self._topology_signature()
        cached = self.scene_obj._topology_cache
        if cached is not None and cached[0] == signature:
            return cached[1]
        roots = self._build_network_topology_uncached()
        self.scene_obj._topology_cache = (signature, roots)
        return roots

    def _build_network_topology_uncached(self):
        devices = [d for d in self.get_network_devices() if not d.is_pass_through()]
        devices_by_id = {d.id: d for d in devices}

        device_edges = {d.id: set() for d in devices}
        for cable in self.get_cables():
            # Patch cables are NOT skipped. Most are panel-to-switch jumpers whose two
            # ends resolve to the same switch, so they fold away on their own below --
            # but an NVR patched straight into a switch inside the rack is a real
            # device-to-device link, and discarding every patch cable left it stranded
            # as its own root beside the switch it is actually plugged into.
            anchors = cable.vertex_anchors
            if not anchors:
                continue
            # Resolve each end through any passive jacks in between. A trunk that runs
            # switch -> wall drop -> switch is anchored to the DROP, not to the far
            # switch, and drops are deliberately excluded from this graph -- so without
            # this the link vanished and every downstream switch looked like its own
            # independent root hanging straight off the WAN.
            a = self._resolve_through_pass_throughs(anchors[0], cable.start_port, arriving_cable=cable)
            b = self._resolve_through_pass_throughs(anchors[-1], cable.end_port, arriving_cable=cable)
            if a and b and a != b and a in device_edges and b in device_edges:
                device_edges[a].add(b)
                device_edges[b].add(a)

        device_cameras = {d.id: self.get_cameras_connected_to_device(d.id) for d in devices}

        visited = set()

        def build_node(dev_id, parent_id):
            visited.add(dev_id)
            children = []
            for neighbor in sorted(device_edges.get(dev_id, ()),
                                   key=lambda n: getattr(devices_by_id[n], "label", "")):
                if neighbor == parent_id or neighbor in visited:
                    continue
                children.append(build_node(neighbor, dev_id))
            return {
                "device": devices_by_id[dev_id],
                "cameras": device_cameras.get(dev_id, []),
                "children": children,
            }

        def root_priority(dev):
            # NVR preferred as the WAN-facing root, then switch, then anything else
            # (access points, or any future category) -- never preferred as a root,
            # but still surfaces on its own if it isn't wired to anything else yet.
            category = dev.spec.get("category")
            if category == "nvr":
                return 0
            if category == "switch":
                return 1
            return 2

        def component_of(start_id):
            seen, queue = {start_id}, [start_id]
            while queue:
                for neighbor in device_edges.get(queue.pop(), ()):
                    if neighbor not in seen:
                        seen.add(neighbor)
                        queue.append(neighbor)
            return seen

        # Root each connected group at its most plausible head rather than at whichever
        # device happened to iterate first -- picking arbitrarily made a downstream Flex
        # look like the head of the network.
        #
        # The head is the device everything else fans out from, so DEGREE decides it: the
        # aggregation switch that four other things trunk into is the head, even when the
        # group also contains an NVR. Category only breaks ties, where it still puts an
        # NVR above a switch (the conventional WAN-facing box in a small CCTV system,
        # where the NVR and its one switch both have a single link). Ranking category
        # first instead re-parented a whole backbone under a leaf NVR hanging off it by
        # one uplink.
        roots = []
        for dev in sorted(devices, key=lambda d: (root_priority(d), getattr(d, "label", ""))):
            if dev.id in visited:
                continue
            members = component_of(dev.id)
            head_id = min(
                members,
                key=lambda d_id: (-len(device_edges.get(d_id, ())),
                                  root_priority(devices_by_id[d_id]),
                                  getattr(devices_by_id[d_id], "label", "")),
            )
            roots.append(build_node(head_id, None))
        return roots

    # ── Orphaned-cable reattachment ──
    def reattach_free_vertices_near(self, item, radius=20.0):
        """Anchors any FREE (unanchored) cable vertex within radius of item's current
        position to item. When a device is deleted, sync_anchored_vertices frees every
        cable that was anchored to it rather than deleting the run -- the ends are left
        dangling at the old device's exact position. This is what lets you replace it:
        drop a new/different device on that same spot (placing it there, or dragging an
        existing one over) and every cable that used to terminate there reattaches in
        one motion, instead of redrawing each run by hand. Returns True if anything
        reattached."""
        item_pos = item.pos()
        reattached_any = False
        for cable in self.get_cables():
            if not cable.points:
                continue
            changed = False
            for i, pt in enumerate(cable.points):
                anchor = cable.vertex_anchors[i] if i < len(cable.vertex_anchors) else None
                if anchor:
                    continue
                if point_hits_item(item, pt, radius):
                    cable.vertex_anchors[i] = item.id
                    cable.points[i] = QPointF(item_pos)
                    changed = True
                    reattached_any = True
            if changed:
                cable.start_device_id = cable.vertex_anchors[0] if cable.vertex_anchors else None
                cable.end_device_id = cable.vertex_anchors[-1] if cable.vertex_anchors else None
                cable.calculate_length()
                cable.update_path()

        if reattached_any:
            recalculate_all_cable_offsets(self.scene_obj)
            self.refresh_connectivity_badges()
        return reattached_any

    # ── Cable vertex editing ──
    def set_editing_cable(self, cable):
        """Puts a single cable into vertex-drag edit mode; only one cable edits at a time."""
        if self.editing_cable is cable:
            return
        if self.editing_cable is not None:
            self.editing_cable.editing_vertices = False
            self.editing_cable.update()
        self.editing_cable = cable
        if cable is not None:
            cable.editing_vertices = True
            cable.update()

    # ── Zone vertex editing ──
    def set_editing_zone(self, zone):
        """Puts a single zone into vertex-drag edit mode; only one zone edits at a time."""
        if self.editing_zone is zone:
            return
        if self.editing_zone is not None:
            self.editing_zone.editing_vertices = False
            self.editing_zone.update()
        self.editing_zone = zone
        if zone is not None:
            zone.editing_vertices = True
            zone.update()

    def find_device_or_camera_by_id(self, item_id):
        if not item_id:
            return None
        for item in self.scene_obj.items():
            if hasattr(item, "id") and item.id == item_id:
                return item
        # Not a top-level scene item -- check whether it's a device mounted inside a
        # rack (those are deliberately kept off the scene; see get_network_devices).
        for rack in self.get_racks():
            slot = rack.find_slot(item_id)
            if slot is not None:
                return slot.device
        return None

    def connect_camera_to_device(self, camera_id, device_id):
        camera = self.find_device_or_camera_by_id(camera_id)
        if not camera:
            return

        self.push_undo_snapshot()
        old_device_id = camera.connected_device_id
        camera.connected_device_id = device_id
        
        # We need to manage connecting cable runs as well.
        # Find if a cable run connects camera and device, or create/delete it.
        # If no device connected now:
        if not device_id:
            # Delete old cable connecting them if any
            if camera.connected_cable_id:
                cable = self.find_device_or_camera_by_id(camera.connected_cable_id)
                if cable:
                    self.scene_obj.removeItem(cable)
                camera.connected_cable_id = None
        else:
            # Connect them via a cable!
            device = self.find_device_or_camera_by_id(device_id)
            if device:
                # If we don't have a cable, draw one directly!
                if not camera.connected_cable_id:
                    cable = CableItem()
                    # Polyline path from camera (x,y) to switch (x,y), both ends anchored
                    cable.set_points([camera.pos(), device.pos()], anchors=[camera.id, device.id])
                    cable.label = f"Cable {camera.label} → {device.label}"
                    cable.setVisible(self.scene_obj.global_show_cables)
                    self.scene_obj.addItem(cable)
                    camera.connected_cable_id = cable.id
                else:
                    # Update existing cable endpoints
                    cable = self.find_device_or_camera_by_id(camera.connected_cable_id)
                    if cable:
                        cable.set_points([camera.pos(), device.pos()], anchors=[camera.id, device.id])
                        cable.label = f"Cable {camera.label} → {device.label}"

        recalculate_all_cable_offsets(self.scene_obj)
        self.refresh_connectivity_badges()

    # ── Unified Place Tool ──
    def snap_point(self, pt):
        """Rounds pt to the nearest grid intersection when Snap to Grid is on;
        returns it unchanged otherwise. Centralized here (rather than in each caller)
        and applied inside every place_* method, so every placement path -- the Place
        tool's menu, catalog drag-and-drop, the Camera/Infra tools, a line-quantity
        batch -- snaps consistently with zero special-casing at the call site."""
        if not self.scene_obj.snap_to_grid:
            return pt
        size = self.scene_obj.effective_grid_size()
        if not size or size <= 0:
            return pt
        return QPointF(round(pt.x() / size) * size, round(pt.y() / size) * size)

    def place_camera(self, spec, world_pt):
        world_pt = self.snap_point(world_pt)
        self.push_undo_snapshot()
        camera = CameraItem(spec, world_pt.x(), world_pt.y())
        camera.label = self.next_free_label("Camera")
        self.scene_obj.addItem(camera)
        self.scene_obj.clearSelection()
        camera.setSelected(True)
        self.item_selected.emit(camera)
        # Placed on top of cable ends left dangling by a deleted camera? Adopt them.
        self.reattach_free_vertices_near(camera)
        return camera

    def place_device(self, spec, world_pt):
        world_pt = self.snap_point(world_pt)
        self.push_undo_snapshot()
        device = make_device_item(spec, world_pt.x(), world_pt.y())
        device.label = self.next_device_label(spec)
        self.scene_obj.addItem(device)
        self.scene_obj.clearSelection()
        device.setSelected(True)
        self.item_selected.emit(device)
        # Placed on top of cable ends left dangling by a deleted switch/NVR? Adopt them
        # -- this is the "replace the device that got deleted" path.
        self.reattach_free_vertices_near(device)
        return device

    def place_custom(self, world_pt, label, icon_type="generic"):
        world_pt = self.snap_point(world_pt)
        self.push_undo_snapshot()
        custom = CustomItem(world_pt.x(), world_pt.y(), label=label, icon_type=icon_type)
        self.scene_obj.addItem(custom)
        self.scene_obj.clearSelection()
        custom.setSelected(True)
        self.item_selected.emit(custom)
        # Placed on top of cable ends left dangling by a deleted anchor? Adopt them --
        # same symmetry as place_camera/place_device/place_rack.
        self.reattach_free_vertices_near(custom)
        return custom

    def place_rack(self, world_pt, label=None, ru_height=12):
        world_pt = self.snap_point(world_pt)
        self.push_undo_snapshot()
        count = len(self.get_racks()) + 1
        rack = RackItem(world_pt.x(), world_pt.y(), label=label or f"Rack {count}", ru_height=ru_height)
        self.scene_obj.addItem(rack)
        self.scene_obj.clearSelection()
        rack.setSelected(True)
        self.item_selected.emit(rack)
        self.reattach_free_vertices_near(rack)
        return rack

    def open_rack_editor(self, rack):
        # A modal dialog can't receive drags from the Equipment Catalog dock (same main
        # window, but the dialog would still block it) -- so opening a rack's editor is
        # a request MainWindow fulfills by opening/focusing a sheet tab instead, where
        # the catalog stays fully reachable the whole time.
        self.rack_open_requested.emit(rack)

    def _rack_at(self, world_pt):
        for rack in self.get_racks():
            if rack.shape().contains(rack.mapFromScene(world_pt)):
                return rack
        return None

    def _drop_device_into_rack(self, spec, rack):
        """Catalog switch/NVR dropped straight onto a rack icon -- mount it inside
        instead of placing it loose on the floor plan, matching drag-and-drop of
        equipment directly into a physical rack."""
        if not rack.can_fit(1):
            QMessageBox.warning(self, "Rack Full", f"{rack.label} has no contiguous RU space left.")
            return
        device = make_device_item(spec, rack.pos().x(), rack.pos().y())
        device.label = self.next_device_label(spec)
        rack.mount(device, ru_size=1)
        rack.update()
        self.scene_obj.clearSelection()
        rack.setSelected(True)
        self.item_selected.emit(rack)

    def show_place_context_menu(self, world_pt, global_pos, line_end=None):
        """Shows a menu at the click point letting the user choose what class of
        equipment to drop there. When line_end is given (the Place tool was used as a
        click-and-drag instead of a plain click), picking a catalog item instead asks
        for a quantity and lays that many out evenly along the press-to-release line --
        Rack/Custom Object are one-off placements so they're omitted in that mode."""
        menu = QMenu(self)

        # Group catalog specs by category -> manufacturer
        grouped = {"camera": {}, "switch": {}, "nvr": {}, "access-point": {}, "drop": {}, "misc": {}}
        for spec in self.catalog_data.values():
            category = spec.get("category", "camera")
            if category not in grouped:
                continue
            mfg = spec.get("manufacturer", "Other")
            grouped[category].setdefault(mfg, []).append(spec)

        def build_submenu(title, category, place_fn):
            submenu = menu.addMenu(title)
            specs_by_mfg = grouped.get(category, {})
            if not specs_by_mfg:
                action = submenu.addAction("No catalog items available")
                action.setEnabled(False)
                return
            for mfg, specs in specs_by_mfg.items():
                mfg_menu = submenu.addMenu(mfg)
                for spec in specs:
                    action = mfg_menu.addAction(spec.get("model", spec.get("id")))
                    if line_end is not None:
                        action.triggered.connect(
                            lambda checked=False, s=spec, fn=place_fn: self._place_line_from_menu(fn, s, world_pt, line_end))
                    else:
                        action.triggered.connect(
                            lambda checked=False, s=spec, fn=place_fn: self._place_from_menu(fn, s, world_pt))

        build_submenu("Camera", "camera", self.place_camera)
        build_submenu("Switch", "switch", self.place_device)
        build_submenu("NVR", "nvr", self.place_device)
        build_submenu("Access Point", "access-point", self.place_device)
        build_submenu("Wall Drop", "drop", self.place_device)
        build_submenu("Misc", "misc", self.place_device)

        if line_end is None:
            menu.addSeparator()
            rack_action = menu.addAction("Rack (with RU slots)")
            rack_action.triggered.connect(lambda: self._place_rack_from_menu(world_pt))
            custom_action = menu.addAction("Custom Object...")
            custom_action.triggered.connect(lambda: self._place_custom_from_menu(world_pt))

        menu.exec(global_pos)

    def _place_from_menu(self, place_fn, spec, world_pt):
        place_fn(spec, world_pt)  # place_camera/place_device apply grid snapping themselves
        # Picking from the menu leaves that item IN HAND rather than dropping you back
        # to Select: the next click places another, with a ghost of it on the cursor.
        # Right-click reopens the menu to swap, Escape puts it down.
        self.set_tool("place", spec)

    def _place_line_from_menu(self, place_fn, spec, p1, p2):
        count, ok = QInputDialog.getInt(self, "Quantity", f"How many {spec.get('model', 'items')}?", 1, 1, 200)
        if ok and count >= 1:
            # One undo entry for the whole batch, not one per item -- push_undo_snapshot
            # is temporarily disabled while place_fn (place_camera/place_device) makes
            # its own per-call snapshot, since a real hook was already pushed above.
            self.push_undo_snapshot()
            real_push = self.push_undo_snapshot
            self.push_undo_snapshot = lambda: None
            try:
                if count == 1:
                    pt = QPointF((p1.x() + p2.x()) / 2.0, (p1.y() + p2.y()) / 2.0)
                    place_fn(spec, pt)
                else:
                    for i in range(count):
                        t = i / (count - 1)
                        pt = QPointF(p1.x() + (p2.x() - p1.x()) * t, p1.y() + (p2.y() - p1.y()) * t)
                        place_fn(spec, pt)
            finally:
                self.push_undo_snapshot = real_push
        self.set_tool("place", spec)

    def _place_rack_from_menu(self, world_pt):
        ru_height, ok = QInputDialog.getInt(self, "New Rack", "Rack height (RU):", 12, 1, 48)
        if not ok:
            self.set_tool("select")
            return
        count = len(self.get_racks()) + 1
        self.place_rack(world_pt, label=f"Rack {count}", ru_height=ru_height)
        self.set_tool("select")

    def _place_custom_from_menu(self, world_pt):
        name, ok = QInputDialog.getText(self, "Custom Object", "Name:", QLineEdit.Normal, "Custom Object")
        if ok and name.strip():
            self.place_custom(world_pt, name.strip())
            self.set_tool("select")

    # ── Unified Label/Zone Tool ──
    def show_labelzone_context_menu(self, world_pt, global_pos):
        menu = QMenu(self)
        add_label_action = menu.addAction("Add Text Label")
        add_label_action.triggered.connect(lambda: self._place_label_from_menu(world_pt))
        draw_zone_action = menu.addAction("Draw Zone Polygon")
        draw_zone_action.triggered.connect(lambda: self.start_zone_drawing(world_pt))
        menu.exec(global_pos)

    def place_label(self, world_pt, text):
        world_pt = self.snap_point(world_pt)
        self.push_undo_snapshot()
        label = LabelItem(world_pt.x(), world_pt.y(), text=text)
        self.scene_obj.addItem(label)
        self.scene_obj.clearSelection()
        label.setSelected(True)
        self.item_selected.emit(label)
        return label

    def _place_label_from_menu(self, world_pt):
        text, ok = QInputDialog.getText(self, "Add Text Label", "Label text:", QLineEdit.Normal, "Label")
        if ok and text.strip():
            self.place_label(world_pt, text.strip())
        self.set_tool("select")

    def start_zone_drawing(self, world_pt):
        self.zone_draw_points = [world_pt]
        self.temp_zone_line = QGraphicsPathItem()
        self.temp_zone_line.setPen(QPen(QColor("#3b82f6"), 2, Qt.DashLine))
        self.scene_obj.addItem(self.temp_zone_line)

        # Highlighted target at the start point, sized to match the actual click-to-
        # close hit radius -- lets you see exactly where to click to finish the loop
        # instead of guessing (see mousePressEvent's polygon-continue branch).
        r = ZONE_CLOSE_SNAP_RADIUS
        self.temp_zone_start_marker = QGraphicsEllipseItem(world_pt.x() - r, world_pt.y() - r, r * 2, r * 2)
        self.temp_zone_start_marker.setPen(QPen(QColor("#f59e0b"), 2, Qt.DashLine))
        self.temp_zone_start_marker.setBrush(Qt.NoBrush)
        self.temp_zone_start_marker.setZValue(10)
        self.scene_obj.addItem(self.temp_zone_start_marker)

        self.update_temp_zone_path(world_pt)
        self.scale_changed.emit("Zone: 1 point placed — double-click or press Enter to finish (min. 3 points)")

    def update_temp_zone_path(self, cursor_pt):
        path = QPainterPath()
        path.moveTo(self.zone_draw_points[0])
        for pt in self.zone_draw_points[1:]:
            path.lineTo(pt)
        path.lineTo(cursor_pt)
        self.temp_zone_line.setPath(path)

        if len(self.zone_draw_points) >= 3:
            hint = (f"Zone: {len(self.zone_draw_points)} point(s) placed — double-click, press Enter, "
                    f"or click the start marker to finish")
        else:
            hint = f"Zone: {len(self.zone_draw_points)} point(s) placed — double-click or press Enter to finish (min. 3 points)"
        self.scale_changed.emit(hint)

    def complete_zone_drawing(self):
        if len(self.zone_draw_points) < 3:
            self.cleanup_temp_shapes()
            self.set_tool("select")
            self.scale_changed.emit(f"Scale: 1 ft = {self.scale_ratio:.1f} px" if self.scale_ratio else "⚠ Not Calibrated")
            return

        text, ok = QInputDialog.getText(self, "Zone Label", "Zone name:", QLineEdit.Normal, "Zone")
        label_text = text.strip() if ok and text.strip() else "Zone"

        self.push_undo_snapshot()
        zone = ZoneItem(list(self.zone_draw_points), label=label_text)
        self.scene_obj.addItem(zone)

        self.scene_obj.clearSelection()
        zone.setSelected(True)
        self.item_selected.emit(zone)

        self.cleanup_temp_shapes()
        self.set_tool("select")
        self.scale_changed.emit(f"Scale: 1 ft = {self.scale_ratio:.1f} px" if self.scale_ratio else "⚠ Not Calibrated")

    # ── Label/Zone tool: click-and-drag quick-draws a rectangular bounding-box label ──
    def _update_labelzone_box_path(self, cursor_pt):
        rect = QRectF(self.labelzone_press_pt, cursor_pt).normalized()
        path = QPainterPath()
        path.addRect(rect)
        self.temp_zone_line.setPath(path)
        self.scale_changed.emit("Zone: drag to size the box, release to finish")

    def _finish_labelzone_box_drag(self, p1, p2):
        self.cleanup_temp_shapes()

        rect = QRectF(p1, p2).normalized()
        if rect.width() < 4.0 or rect.height() < 4.0:
            # Too small to be a deliberate drag (a near-stationary release already
            # falls into the click path below via labelzone_dragging_box staying
            # False, so this only guards a genuine-but-tiny drag) -- just drop it
            # rather than create a sliver box nobody meant to draw.
            self.set_tool("select")
            return

        text, ok = QInputDialog.getText(self, "Zone Label", "Zone name:", QLineEdit.Normal, "Zone")
        label_text = text.strip() if ok and text.strip() else "Zone"

        self.push_undo_snapshot()
        zone = ZoneItem([rect.topLeft(), rect.topRight(), rect.bottomRight(), rect.bottomLeft()], label=label_text)
        zone.is_box = True
        self.scene_obj.addItem(zone)

        self.scene_obj.clearSelection()
        zone.setSelected(True)
        self.item_selected.emit(zone)

        self.set_tool("select")
        self.scale_changed.emit(f"Scale: 1 ft = {self.scale_ratio:.1f} px" if self.scale_ratio else "⚠ Not Calibrated")

    # ── Floorplan management ──
    def load_floorplan(self, filepath):
        pixmap = QPixmap(filepath)
        if pixmap.isNull():
            QMessageBox.critical(self, "Error", "Could not load image file.")
            return
        self._apply_floorplan_pixmap(pixmap, filepath)

    def load_floorplan_from_bytes(self, image_bytes, original_filename=""):
        """Same as load_floorplan, but from raw image bytes embedded in a .lense
        project file rather than a path on disk -- see MainWindow._restore_state.
        This is what lets a floorplan travel WITH the project file (between users,
        or between Linux and Windows) instead of being a dangling local path that
        silently fails to resolve on whatever machine opens the file next."""
        pixmap = QPixmap()
        if not pixmap.loadFromData(image_bytes):
            QMessageBox.critical(self, "Error", "Could not load the floorplan image embedded in this project file.")
            return
        self._apply_floorplan_pixmap(pixmap, original_filename)

    def _apply_floorplan_pixmap(self, pixmap, filepath):
        if self.floorplan_item:
            self.scene_obj.removeItem(self.floorplan_item)

        self.floorplan_item = FloorPlanItem(pixmap)
        self.floorplan_item.filepath = filepath
        self.scene_obj.addItem(self.floorplan_item)
        self.scene_obj.setSceneRect(self.floorplan_item.boundingRect())

        # Fit image in view
        self.fitInView(self.floorplan_item.boundingRect(), Qt.KeepAspectRatio)
        self._zoom_changed()

    # ── Zoom / Pan ──
    def _sync_view_lod(self):
        """Publishes the zoom level to the scene and re-indexes anything sized by it.

        Labels and warning badges are drawn at a constant on-screen size, so their
        footprint in scene units is proportional to 1/zoom. Bounding rects have to grow
        to match, or Qt cannot know those pixels were painted and a partial viewport
        update leaves the old label behind when the item moves. Qt caches bounding rects
        in its spatial index, hence prepareGeometryChange() when the zoom really shifts.
        """
        lod = self.transform().m11() or 1.0
        previous = getattr(self.scene_obj, "view_lod", 1.0)
        if previous and abs(lod - previous) < previous * 0.01:
            return  # negligible change; not worth re-indexing the whole scene
        def assign():
            self.scene_obj.view_lod = lod
        self.apply_scene_geometry_change(assign)

    def _zoom_changed(self):
        self._sync_view_lod()
        # Label footprints scale with 1/zoom, so what collides changes as you move
        # through the zoom range -- this is where stacking and unstacking happens.
        self.relayout_labels()
        self.zoom_changed.emit(self.transform().m11() * 100)

    def zoom_in(self):
        self.scale(ZOOM_STEP, ZOOM_STEP)
        self._zoom_changed()

    def zoom_out(self):
        self.scale(1.0 / ZOOM_STEP, 1.0 / ZOOM_STEP)
        self._zoom_changed()

    def zoom_fit(self):
        if self.floorplan_item:
            self.fitInView(self.floorplan_item.boundingRect(), Qt.KeepAspectRatio)
        else:
            # Fit scene contents
            self.fitInView(self.scene_obj.itemsBoundingRect(), Qt.KeepAspectRatio)
        self._zoom_changed()

    def wheelEvent(self, event):
        # Zoom centered on mouse cursor, by an amount that follows the size of the event.
        # A fixed step per event was right for a mouse (one event per notch) and far too
        # fast for a touchpad, which sends dozens of tiny events for a single swipe --
        # each was handed a full step. See core/zoom_input.py.
        angle = event.angleDelta().y()
        pixel = event.pixelDelta().y()
        amount = zoom_input.notches(angle, pixel)
        if amount == 0:
            event.accept()   # e.g. the zero-length "gesture ended" event a touchpad sends
            return
        device = event.pointingDevice()
        touchpad = zoom_input.is_touchpad(
            angle, pixel,
            event.phase() != Qt.NoScrollPhase,
            device is not None and device.type() == QInputDevice.DeviceType.TouchPad)
        sensitivity = (self.zoom_sensitivity_touchpad if touchpad
                       else self.zoom_sensitivity_mouse)
        zoom_factor = zoom_input.zoom_factor(ZOOM_STEP, amount, sensitivity)

        # Save scene position under mouse
        old_scene_pos = self.mapToScene(event.position().toPoint())
        
        # Apply zoom scale
        self.scale(zoom_factor, zoom_factor)
        
        # Restore position under mouse
        new_scene_pos = self.mapToScene(event.position().toPoint())
        delta = new_scene_pos - old_scene_pos
        self.translate(delta.x(), delta.y())
        
        self._zoom_changed()

    # ── Drag & Drop (Catalog equipment -> Canvas) ──
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
        if mime.hasFormat(CATALOG_SPEC_MIME_TYPE):
            spec_id = bytes(mime.data(CATALOG_SPEC_MIME_TYPE)).decode("utf-8")
            spec = self.catalog_data.get(spec_id)
            if spec:
                world_pt = self.mapToScene(event.position().toPoint())
                category = spec.get("category", "camera")
                if category == "camera":
                    self.place_camera(spec, world_pt)
                elif category in ("switch", "nvr") and self._rack_at(world_pt) is not None:
                    # Dropped straight onto a rack icon -- mount it inside instead of
                    # placing it loose on the floor plan next to the rack.
                    self._drop_device_into_rack(spec, self._rack_at(world_pt))
                else:
                    self.place_device(spec, world_pt)
                # Same as the menu: keep it in hand for the next one.
                self.set_tool("place", spec)
            event.acceptProposedAction()
        else:
            super().dropEvent(event)

    # ── The item in hand ──
    HAND_PREVIEW_OPACITY = 0.55

    def clear_hand_preview(self):
        if getattr(self, "hand_preview", None) is not None:
            if self.hand_preview.scene() is self.scene_obj:
                self.scene_obj.removeItem(self.hand_preview)
            self.hand_preview = None

    def update_hand_preview(self):
        """Shows a ghost of whatever the armed tool will place, following the cursor.

        Built from the real item class rather than drawn separately, so it looks exactly
        like what you are about to get -- right artwork, right size, following the icon
        scale. Its object_type is deliberately something no scan recognises: it must
        never turn up in the inventory, the topology, or a PoE calculation just because
        it happens to be sitting in the scene.
        """
        spec = self.selected_catalog_spec
        if self.active_tool not in ("camera", "infra", "place") or not spec:
            self.clear_hand_preview()
            return
        if getattr(self, "hand_preview", None) is not None and \
                getattr(self.hand_preview, "preview_spec_id", None) == spec.get("id"):
            return  # already holding this one

        self.clear_hand_preview()
        if spec.get("category") == "camera":
            item = CameraItem(spec, 0, 0)
        else:
            item = make_device_item(spec, 0, 0)
        item.object_type = "hand_preview"
        item.preview_spec_id = spec.get("id")
        item.is_preview = True          # keeps validation off it; see _update_connectivity_warnings
        item.label = ""                 # a ghost needs no name tag trailing the cursor
        item.setOpacity(self.HAND_PREVIEW_OPACITY)
        item.setFlags(QGraphicsItem.GraphicsItemFlags())
        item.setAcceptedMouseButtons(Qt.NoButton)
        item.setZValue(10000)
        item.setPos(self.last_mouse_scene_pos or QPointF(0, 0))
        self.scene_obj.addItem(item)
        self.hand_preview = item

    def place_from_hand(self, world_pt):
        """Drops the item currently in hand, whatever kind it is."""
        spec = self.selected_catalog_spec
        if not spec:
            return None
        if spec.get("category") == "camera":
            placed = self.place_camera(spec, world_pt)
        elif spec.get("category") in ("switch", "nvr") and self._rack_at(world_pt) is not None:
            self._drop_device_into_rack(spec, self._rack_at(world_pt))
            placed = None
        else:
            placed = self.place_device(spec, world_pt)
        self._stay_armed()
        return placed

    def _stay_armed(self):
        """Keeps the placement tool loaded with the same item after each drop.

        Laying out a floor means placing the same wall drop twenty times; bouncing back
        to Select after every one meant re-picking the tool and the catalog item between
        each. The tool stays armed until you pick another one or press Escape -- the
        cable tool has always worked this way, and this makes placement match it.
        """
        spec = self.selected_catalog_spec or {}
        name = spec.get("model") or spec.get("id") or "item"
        self.scale_changed.emit(f"{name}: click to place another \u2014 Esc to stop")

    # ── Input Overrides (Drawing, placing, measure) ──
    def mousePressEvent(self, event):
        world_pt = self.mapToScene(event.pos())
        self.coords_changed.emit(world_pt)

        # 1. PAN TOOL (or spacebar panning)
        if self.active_tool == "pan" or self.space_pressed or event.button() == Qt.MiddleButton:
            self.pan_active = True
            self.last_pan_pos = event.pos()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return

        # 2. CALIBRATION TOOL
        if self.active_tool == "calibrate" and event.button() == Qt.LeftButton:
            self.calib_start_pt = world_pt
            self.calib_line = CalibrationLine(world_pt, world_pt)
            self.scene_obj.addItem(self.calib_line)
            event.accept()
            return

        # 3. CAMERA PLACEMENT TOOL
        if self.active_tool == "camera" and event.button() == Qt.LeftButton:
            if self.selected_catalog_spec:
                self.place_camera(self.selected_catalog_spec, world_pt)
                self._stay_armed()
            event.accept()
            return

        # 4. NETWORK DEVICE PLACEMENT TOOL
        if self.active_tool == "infra" and event.button() == Qt.LeftButton:
            if self.selected_catalog_spec:
                spec = self.selected_catalog_spec
                category = spec.get("category")
                if category in ("switch", "nvr") and self._rack_at(world_pt) is not None:
                    # Same rule as a catalog drag: dropped on a rack icon, it mounts
                    # inside rather than landing loose on the floor plan beside it.
                    self._drop_device_into_rack(spec, self._rack_at(world_pt))
                else:
                    self.place_device(spec, world_pt)
                self._stay_armed()
            event.accept()
            return

        # 5. CABLE / FIBER TOOL (Waypoints routing -- identical mechanics, cable_type
        # and render color are the only things that differ; see complete_cable_drawing)
        if self.active_tool in ("cable", "fiber") and event.button() == Qt.LeftButton:
            # Ctrl+click on an existing bundle: trace it back to whatever it feeds and
            # finish the run there, instead of re-clicking the same corridor for every
            # wall drop. Falls through to an ordinary waypoint if the click missed.
            if (event.modifiers() & Qt.ControlModifier) and self.cable_draw_points:
                followed = self.follow_bundle_path(world_pt, split=True)
                if followed:
                    self.cable_draw_points.extend(followed)
                    self.complete_cable_drawing()
                    event.accept()
                    return

            # A *precise* click right on the body of an existing cable inserts an inline
            # vertex there instead of starting a brand-new run -- but only away from any
            # camera/device (cables render above devices, so a device with a cable
            # already attached would otherwise always hit the cable first and block a
            # second connection), and only within a tight radius. A looser click that's
            # merely near the line is presumed to mean "start a new, separate cable
            # here" -- snap_to_devices below will still snap it onto the same line to
            # form a bundle, rather than silently grafting a vertex onto the old cable.
            if not self.cable_draw_points:
                # Racks and custom objects count here too: the MDF is where every run
                # in the project converges, so without them a click on it always landed
                # on a cable body first and grafted a vertex onto an existing run
                # instead of starting the new one.
                near_device = any(
                    distance((world_pt.x(), world_pt.y()), (item.x(), item.y())) <= 20.0
                    for item in (self.get_cameras() + self.get_network_devices()
                                 + self.get_racks() + self.get_custom_objects())
                )
                if not near_device:
                    nearest_cable, nearest_dist = self._closest_cable_to_point(world_pt)
                    if nearest_cable is not None and nearest_dist <= 3.0:
                        nearest_cable.insert_vertex_at(world_pt)
                        event.accept()
                        return

            # Snapping to cameras, switches/NVRs, or existing cable vertices
            snapped_pt = self.snap_to_devices(world_pt)
            self.cable_draw_points.append(snapped_pt)

            if not self.temp_draw_line:
                self.temp_draw_line = QGraphicsPathItem()
                temp_color = QColor("#eab308") if self.active_tool == "fiber" else QColor("#60a5fa")
                self.temp_draw_line.setPen(QPen(temp_color, 2, Qt.DashLine))
                self.scene_obj.addItem(self.temp_draw_line)

            self.update_temp_cable_path(snapped_pt)
            kind = "Fiber" if self.active_tool == "fiber" else "Cable"
            self.scale_changed.emit(
                f"{kind}: {len(self.cable_draw_points)} point(s) placed — double-click or "
                f"Enter to finish, or Ctrl+click an existing run to follow it home")
            event.accept()
            return

        # 6. PLACE TOOL (a plain click shows the equipment menu for a single placement;
        # click-and-drag previews a line -- see mouseMoveEvent/mouseReleaseEvent -- and
        # on release asks for a catalog item plus a quantity to lay out along it)
        if self.active_tool == "place" and event.button() == Qt.LeftButton:
            self.place_press_pt = world_pt
            self.place_press_screen_pt = event.pos()
            self.place_dragging_line = False
            event.accept()
            return

        # Right click on any armed placement tool offers the equipment menu, so you can
        # swap what's in hand without going back to the catalog.
        if (self.active_tool in ("place", "camera", "infra")
                and event.button() == Qt.RightButton):
            self.show_place_context_menu(world_pt, event.globalPos())
            event.accept()
            return

        # 6.5 LABEL/ZONE TOOL (unified: click-and-drag quick-draws a box, a plain click
        # shows the "Add Text Label / Draw Zone Polygon" menu, and once a polygon is
        # in progress every click adds a vertex -- or closes the loop near the start)
        if self.active_tool == "labelzone" and event.button() == Qt.LeftButton:
            if not self.zone_draw_points:
                clicked_item = self.itemAt(event.pos())
                if isinstance(clicked_item, ZoneItem) and not clicked_item.is_box:
                    clicked_item.insert_vertex_at(world_pt)
                    event.accept()
                    return
                # Don't decide click-vs-drag yet -- mouseMoveEvent promotes this to a
                # box drag if the cursor travels far enough before release; otherwise
                # mouseReleaseEvent treats it as a plain click and shows the menu.
                self.labelzone_press_pt = world_pt
                self.labelzone_press_screen_pt = event.pos()
                self.labelzone_dragging_box = False
                event.accept()
                return
            else:
                if len(self.zone_draw_points) >= 3:
                    start_pt = self.zone_draw_points[0]
                    if distance((world_pt.x(), world_pt.y()), (start_pt.x(), start_pt.y())) <= ZONE_CLOSE_SNAP_RADIUS:
                        self.complete_zone_drawing()
                        event.accept()
                        return
                self.zone_draw_points.append(world_pt)
                self.update_temp_zone_path(world_pt)
                event.accept()
                return

        # 6.7 LASSO TOOL -- drag a line across a bundle of cables
        if self.active_tool == "lasso" and event.button() == Qt.LeftButton:
            self.lasso_start_pt = world_pt
            self.lasso_line = QGraphicsLineItem(QLineF(world_pt, world_pt))
            pen = QPen(QColor("#f59e0b"), 2, Qt.DashLine)
            pen.setCosmetic(True)
            self.lasso_line.setPen(pen)
            self.lasso_line.setZValue(50)
            self.scene_obj.addItem(self.lasso_line)
            event.accept()
            return

        # 7. MEASUREMENT TOOL
        if self.active_tool == "measure" and event.button() == Qt.LeftButton:
            self.measure_start_pt = world_pt
            self.measure_line = QGraphicsLineItem(QLineF(world_pt, world_pt))
            self.measure_line.setPen(QPen(QColor("#06b6d4"), 2, Qt.DashLine))
            self.scene_obj.addItem(self.measure_line)
            event.accept()
            return

        # 8. SELECT TOOL (default)
        if self.active_tool == "select" and event.button() == Qt.LeftButton:
            # Clicking away from the cable/zone currently being vertex-edited exits edit mode
            clicked_item = self.itemAt(event.pos())
            if self.editing_cable is not None and clicked_item is not self.editing_cable:
                self.set_editing_cable(None)
            if self.editing_zone is not None and clicked_item is not self.editing_zone:
                self.set_editing_zone(None)

            # Undo for a drag: the pre-drag state has to be captured now, before
            # anything moves, but it's only worth an undo entry if the item actually
            # ends up somewhere different -- decided at release, see mouseReleaseEvent.
            if clicked_item is not None and getattr(clicked_item, "object_type", None) in (
                    "camera", "device", "rack", "custom", "label", "zone"):
                self._move_item = clicked_item
                self._move_start_pos = clicked_item.pos()
                self._move_start_geometry = self._drag_geometry(clicked_item)
                self._pending_move_snapshot = self.capture_undo_state()
            else:
                self._move_item = None
                self._pending_move_snapshot = None

            super().mousePressEvent(event)
            # Find selected item
            sel_items = self.scene_obj.selectedItems()
            if sel_items:
                # Select the highest-level object (Camera, Device, Cable)
                self.item_selected.emit(sel_items[0])
            else:
                self.item_selected.emit(None)
            return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        world_pt = self.mapToScene(event.pos())
        self.coords_changed.emit(world_pt)
        self.last_mouse_scene_pos = world_pt

        if getattr(self, "hand_preview", None) is not None:
            self.hand_preview.setPos(self.snap_point(world_pt))

        # Bundle hover-fan-out (see the select-tool recalc at the end of this method) is
        # deliberately NOT triggered while the cable/fiber tool is active. Every existing
        # bundle your cursor sweeps past while aiming the next waypoint would otherwise
        # keep fanning open and snapping shut around the line you're trying to draw --
        # the whole map appearing to sprawl mid-draw is disorienting, so everything else
        # just sits still (merged, at rest) for the entire time you're using the tool.

        # 1. PAN ACTIVE
        if self.pan_active:
            delta = event.pos() - self.last_pan_pos
            self.last_pan_pos = event.pos()
            
            # Pan scrollbars
            h_bar = self.horizontalScrollBar()
            v_bar = self.verticalScrollBar()
            h_bar.setValue(h_bar.value() - delta.x())
            v_bar.setValue(v_bar.value() - delta.y())
            event.accept()
            return

        # 2. CALIBRATION DRAWING
        if self.active_tool == "calibrate" and self.calib_line and self.calib_start_pt:
            self.calib_line.set_points(self.calib_start_pt, world_pt)
            event.accept()
            return

        # 3. CABLE DRAWING (Draw temporary rubber-band line)
        if self.active_tool in ("cable", "fiber") and self.temp_draw_line and self.cable_draw_points:
            last_pt = self.cable_draw_points[-1]
            snapped_pt = self.snap_to_devices(world_pt)
            
            # Snap cable segments to horizontal/vertical if Shift key is pressed
            if event.modifiers() & Qt.ShiftModifier:
                dx = abs(snapped_pt.x() - last_pt.x())
                dy = abs(snapped_pt.y() - last_pt.y())
                if dx > dy:
                    snapped_pt.setY(last_pt.y())
                else:
                    snapped_pt.setX(last_pt.x())

            self.update_temp_cable_path(snapped_pt)
            if event.modifiers() & Qt.ControlModifier:
                self.update_follow_preview(world_pt)
            else:
                self.clear_follow_preview()
            event.accept()
            return

        # 3.4 LABEL/ZONE BOX-DRAG DETECTION (promotes a still-undecided press into a
        # bounding-box drag once the cursor has moved far enough -- see mousePressEvent)
        if self.active_tool == "labelzone" and self.labelzone_press_pt is not None:
            if not self.labelzone_dragging_box:
                moved = event.pos() - self.labelzone_press_screen_pt
                if moved.manhattanLength() >= 6:
                    self.labelzone_dragging_box = True
                    self.temp_zone_line = QGraphicsPathItem()
                    self.temp_zone_line.setPen(QPen(QColor("#3b82f6"), 2, Qt.DashLine))
                    self.scene_obj.addItem(self.temp_zone_line)
            if self.labelzone_dragging_box:
                self._update_labelzone_box_path(world_pt)
                event.accept()
                return

        # 3.5 ZONE DRAWING (Draw temporary rubber-band polygon outline)
        if self.active_tool == "labelzone" and self.temp_zone_line and self.zone_draw_points:
            self.update_temp_zone_path(world_pt)
            event.accept()
            return

        # 3.6 PLACE TOOL LINE-DRAG DETECTION (same promote-past-a-threshold pattern as
        # the Label/Zone box drag above)
        if self.active_tool == "place" and self.place_press_pt is not None:
            if not self.place_dragging_line:
                moved = event.pos() - self.place_press_screen_pt
                if moved.manhattanLength() >= 6:
                    self.place_dragging_line = True
                    self.temp_place_line = QGraphicsLineItem(QLineF(self.place_press_pt, world_pt))
                    self.temp_place_line.setPen(QPen(QColor("#60a5fa"), 2, Qt.DashLine))
                    self.scene_obj.addItem(self.temp_place_line)
            if self.place_dragging_line:
                self.temp_place_line.setLine(QLineF(self.place_press_pt, world_pt))
                self.scale_changed.emit("Place: release to choose equipment and a quantity to lay out along this line")
                event.accept()
                return

        # 3.7 LASSO DRAWING
        if self.active_tool == "lasso" and self.lasso_line and self.lasso_start_pt:
            self.lasso_line.setLine(QLineF(self.lasso_start_pt, world_pt))
            crossings = len(self._lasso_crossings(self.lasso_start_pt, world_pt))
            self.scale_changed.emit(
                f"Lasso: {crossings} cable(s) crossed \u2014 release to bundle them")
            event.accept()
            return

        # 4. MEASUREMENT DRAWING
        if self.active_tool == "measure" and self.measure_line and self.measure_start_pt:
            self.measure_line.setLine(QLineF(self.measure_start_pt, world_pt))
            # Calculate distance and print to status bar
            px = distance(self.measure_start_pt, world_pt)
            if self.scale_ratio:
                dist_str = format_distance_both(px / self.scale_ratio)
                self.scale_changed.emit(f"Measuring: {dist_str}")
            else:
                self.scale_changed.emit(f"Measuring: {px:.0f} pixels (Calibrate Scale)")
            event.accept()
            return

        super().mouseMoveEvent(event)

        # Select/Move tool: run after the item drag above so a dragged camera/device's
        # freshly-applied position is what any anchored cable vertex snaps to this frame.
        if self.active_tool == "select":
            recalculate_all_cable_offsets(self.scene_obj, hover_pt=world_pt)

    def leaveEvent(self, event):
        # Collapse any hover-expanded cable bundle back to its tight default spacing
        recalculate_all_cable_offsets(self.scene_obj)
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event):
        # The drag is over, so whatever property change it held back can be delivered
        # now, once, against the item's final position.
        self.scene_obj.flush_deferred_property_change()
        world_pt = self.mapToScene(event.pos())

        # 1. PAN RELEASE
        if self.pan_active:
            self.pan_active = False
            self.setCursor(Qt.OpenHandCursor if self.active_tool == "pan" else Qt.ArrowCursor)
            event.accept()
            return

        # 2. CALIBRATION COMPLETION
        if self.active_tool == "calibrate" and self.calib_line and self.calib_start_pt:
            # We finished placing the calibration line. Pop up QDialog to set scale
            px_dist = distance(self.calib_start_pt, world_pt)
            if px_dist > 5:
                dialog = CalibrationDialog(px_dist, self)
                if dialog.exec() == QDialog.Accepted:
                    val, unit = dialog.get_values()
                    
                    # Store calibration values
                    self.scene_obj.calibration_unit = unit
                    self.scene_obj.calibration_dist = val
                    self.scene_obj.calibration_p1 = (self.calib_start_pt.x(), self.calib_start_pt.y())
                    self.scene_obj.calibration_p2 = (world_pt.x(), world_pt.y())
                    
                    # scale_ratio represents pixels per foot.
                    # If meters entered, convert meters to feet first.
                    if unit == "meters":
                        feet = val * 3.28084
                    else:
                        feet = val
                        
                    self.scale_ratio = px_dist / feet
                    self.scene_obj.scale_ratio = self.scale_ratio
                    self.on_calibration_changed()

                    # Update status indicator scale message
                    m_str = f"Scale calibrated: 1 ft = {self.scale_ratio:.1f} pixels"
                    self.scale_changed.emit(m_str)
                    
                    # Trigger redraw on all visual items containing FOV / cables
                    for item in self.scene_obj.items():
                        item.update()
                        
                    # Also recalculate parallel cable runs stack offsets
                    recalculate_all_cable_offsets(self.scene_obj)
            
            # Clean calibration lines
            self.scene_obj.removeItem(self.calib_line)
            self.calib_line = None
            self.calib_start_pt = None
            self.set_tool("select")
            event.accept()
            return

        # 2.5 LASSO RELEASE
        if self.active_tool == "lasso" and self.lasso_line and self.lasso_start_pt:
            start = self.lasso_start_pt
            self.scene_obj.removeItem(self.lasso_line)
            self.lasso_line = None
            self.lasso_start_pt = None
            bundled = self.apply_lasso(start, world_pt)
            self.scale_changed.emit(
                f"Lasso: bundled {bundled} cable(s)" if bundled
                else "Lasso: no cables crossed that line")
            event.accept()
            return

        # 3. MEASUREMENT RELEASE
        if self.active_tool == "measure" and self.measure_line:
            self.scene_obj.removeItem(self.measure_line)
            self.measure_line = None
            self.measure_start_pt = None
            self.scale_changed.emit(f"Scale: 1 ft = {self.scale_ratio:.1f} px" if self.scale_ratio else "⚠ Not Calibrated")
            self.set_tool("select")
            event.accept()
            return

        # 3.5 LABEL/ZONE RELEASE -- a plain click (never promoted to a drag) shows the
        # existing menu; a drag past the threshold quick-draws a rectangular box label.
        if self.active_tool == "labelzone" and self.labelzone_press_pt is not None:
            press_pt = self.labelzone_press_pt
            press_global_pos = event.globalPos()
            was_dragging = self.labelzone_dragging_box
            self.labelzone_press_pt = None
            self.labelzone_press_screen_pt = None
            self.labelzone_dragging_box = False
            if was_dragging:
                self._finish_labelzone_box_drag(press_pt, world_pt)
            else:
                self.show_labelzone_context_menu(press_pt, press_global_pos)
            event.accept()
            return

        # 3.6 PLACE TOOL RELEASE -- a plain click shows the single-placement menu; a
        # drag shows the same menu but wired for line-quantity placement instead.
        if self.active_tool == "place" and self.place_press_pt is not None:
            press_pt = self.place_press_pt
            press_global_pos = event.globalPos()
            was_dragging = self.place_dragging_line
            self.place_press_pt = None
            self.place_press_screen_pt = None
            self.place_dragging_line = False
            if self.temp_place_line:
                self.scene_obj.removeItem(self.temp_place_line)
                self.temp_place_line = None
            if was_dragging:
                self.show_place_context_menu(press_pt, press_global_pos, line_end=world_pt)
            elif self.selected_catalog_spec:
                # Something is in hand: a plain click drops another one. The menu is on
                # the right button now, so laying out a row is click, click, click.
                self.place_from_hand(press_pt)
            else:
                self.show_place_context_menu(press_pt, press_global_pos)
            event.accept()
            return

        super().mouseReleaseEvent(event)

        # A camera/device just dragged to a new spot may have landed on cable ends left
        # dangling by a deleted anchor -- auto-reattach them, so replacing a deleted
        # switch/NVR with an existing one doesn't mean redrawing every cable by hand.
        if self.active_tool == "select":
            movable_types = ("camera", "device", "rack", "custom", "label")
            if self.scene_obj.snap_to_grid:
                # Snap-on-release rather than live during the drag, for simplicity.
                # Each selected item snaps independently, which can shift spacing
                # within a multi-item drag rather than preserving it exactly -- an
                # acceptable simplification for a first pass at grid snapping.
                for item in self.scene_obj.selectedItems():
                    if getattr(item, "object_type", None) in movable_types:
                        item.setPos(self.snap_point(item.pos()))
            for item in self.scene_obj.selectedItems():
                if getattr(item, "object_type", None) in ("camera", "device", "rack", "custom"):
                    self.reattach_free_vertices_near(item)

        # Commit the pre-drag undo snapshot captured on press, but only if the item
        # actually ended up somewhere different -- a plain click (no drag) would
        # otherwise push a no-op entry onto the undo stack for every single selection.
        if self._move_item is not None and self._pending_move_snapshot is not None:
            if (self._move_item.pos() != self._move_start_pos
                    or self._drag_geometry(self._move_item) != self._move_start_geometry):
                self.commit_undo_state(self._pending_move_snapshot)
        self._move_item = None
        self._pending_move_snapshot = None
        self._move_start_geometry = None

    def mouseDoubleClickEvent(self, event):
        world_pt = self.mapToScene(event.pos())

        # Complete cable/fiber drawing double click
        if self.active_tool in ("cable", "fiber"):
            self.complete_cable_drawing()
            event.accept()
            return

        # Complete zone polygon drawing double click
        if self.active_tool == "labelzone" and self.zone_draw_points:
            self.complete_zone_drawing()
            event.accept()
            return

        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event):
        world_pt = self.mapToScene(event.pos())
        clicked_item = self.itemAt(event.pos())

        # Right-clicking an item outside the current selection replaces the selection
        # with just that item (so the menu clearly applies to it) -- but right-clicking
        # one that's already part of a multi-selection leaves the whole selection
        # alone, so Duplicate/Delete apply to the group.
        if clicked_item is not None and not clicked_item.isSelected():
            self.scene_obj.clearSelection()
            clicked_item.setSelected(True)
            self.item_selected.emit(clicked_item)

        sel_items = self.scene_obj.selectedItems()
        single = sel_items[0] if len(sel_items) == 1 else None

        menu = QMenu(self)

        if isinstance(single, CableItem) or (isinstance(single, ZoneItem) and not single.is_box):
            # A box label is locked to 4 corners so its resize-drag math stays valid
            # (see ZoneItem.mouseMoveEvent) -- adding an arbitrary vertex would break
            # that invariant, so this is only offered for cables and hand-drawn zones.
            add_vertex_action = menu.addAction("Add Vertex Here\tX")
            add_vertex_action.triggered.connect(lambda: single.insert_vertex_at(world_pt))
            menu.addSeparator()
        elif isinstance(single, RackItem):
            open_rack_action = menu.addAction("Open Rack Editor")
            open_rack_action.triggered.connect(lambda: self.open_rack_editor(single))
            menu.addSeparator()

        if sel_items:
            suffix = f" ({len(sel_items)})" if len(sel_items) > 1 else ""
            dup_action = menu.addAction(f"Duplicate{suffix}\tCtrl+D")
            dup_action.triggered.connect(self.duplicate_selected)
            del_action = menu.addAction(f"Delete{suffix}\tDel")
            del_action.triggered.connect(self.delete_selected)

        if menu.isEmpty():
            super().contextMenuEvent(event)
            return

        menu.exec(event.globalPos())
        event.accept()

    # ── Keyboard Shortcuts (Delete, Escape, space panning) ──
    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Space:
            self.space_pressed = True
            self.setCursor(Qt.OpenHandCursor)
            event.accept()
            return

        if event.key() == Qt.Key_Escape:
            self.cleanup_temp_shapes()
            self.set_tool("select")
            self.set_editing_cable(None)
            self.set_editing_zone(None)
            event.accept()
            return

        if event.key() in [Qt.Key_Delete, Qt.Key_Backspace]:
            self.delete_selected()
            event.accept()
            return

        if event.key() in [Qt.Key_Return, Qt.Key_Enter]:
            if self.active_tool in ("cable", "fiber") and self.cable_draw_points:
                self.complete_cable_drawing()
                event.accept()
                return
            if self.active_tool == "labelzone" and self.zone_draw_points:
                self.complete_zone_drawing()
                event.accept()
                return

        if event.key() == Qt.Key_X:
            selected_editable = [item for item in self.scene_obj.selectedItems()
                                  if getattr(item, "object_type", None) == "cable"
                                  or (getattr(item, "object_type", None) == "zone" and not item.is_box)]
            if selected_editable:
                selected_editable[0].insert_vertex_at(self.last_mouse_scene_pos)
                event.accept()
                return

        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.key() == Qt.Key_Space:
            self.space_pressed = False
            self.setCursor(Qt.ArrowCursor if self.active_tool == "select" else Qt.CrossCursor)
            event.accept()
            return
        super().keyReleaseEvent(event)

    # ── Cable drawing logic ──
    def update_temp_cable_path(self, cursor_pt):
        """Redraws the full in-progress cable polyline (all placed waypoints plus a rubber-band segment to the cursor)."""
        path = QPainterPath()
        path.moveTo(self.cable_draw_points[0])
        for pt in self.cable_draw_points[1:]:
            path.lineTo(pt)
        path.lineTo(cursor_pt)
        self.temp_draw_line.setPath(path)

    def clear_follow_preview(self):
        if self.follow_preview_line is not None:
            if self.follow_preview_line.scene() is self.scene_obj:
                self.scene_obj.removeItem(self.follow_preview_line)
            self.follow_preview_line = None

    def update_follow_preview(self, cursor_pt):
        """Highlights the route a Ctrl+click would take. Not a confirmation step -- it
        costs no extra clicks -- just enough feedback that following the wrong way out
        of a junction is visible before you commit rather than after."""
        followed = self.follow_bundle_path(cursor_pt) if self.cable_draw_points else None
        if not followed:
            self.clear_follow_preview()
            return
        if self.follow_preview_line is None:
            self.follow_preview_line = QGraphicsPathItem()
            pen = QPen(QColor("#22c55e"), 4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
            pen.setCosmetic(True)
            self.follow_preview_line.setPen(pen)
            self.follow_preview_line.setZValue(9000)
            self.scene_obj.addItem(self.follow_preview_line)
        path = QPainterPath()
        path.moveTo(self.cable_draw_points[-1])
        for pt in followed:
            path.lineTo(pt)
        self.follow_preview_line.setPath(path)

    # ── Follow-the-bundle routing (Ctrl + click while drawing a cable) ──
    FOLLOW_SNAP_RADIUS = 20.0
    FOLLOW_MAX_HOPS = 12

    def _cable_at_point(self, pt, exclude=()):
        """(cable, point on it, segment index) for the nearest cable, or None."""
        best, best_dist = None, self.FOLLOW_SNAP_RADIUS
        for cable in self.get_cables():
            if cable.id in exclude:
                continue
            points = cable.points
            for i in range(1, len(points)):
                a = (points[i - 1].x(), points[i - 1].y())
                b = (points[i].x(), points[i].y())
                proj, _t = closest_point_on_segment((pt.x(), pt.y()), a, b)
                d = distance((pt.x(), pt.y()), proj)
                if d < best_dist:
                    best_dist = d
                    best = (cable, QPointF(proj[0], proj[1]), i)
        return best

    def _upstream_score(self, cable, end_index):
        """How likely one end of `cable` is to be the way back to the MDF. Lower wins.

        Uses the same resolution the network diagram does, so "upstream" can't mean one
        thing here and another there: a rack beats a switch, a switch beats anything
        else, and among equals the better-connected device wins -- that's the
        aggregation point everything trunks into.
        """
        anchors = cable.vertex_anchors or []
        anchor_id = anchors[end_index] if anchors else None
        if not anchor_id:
            return (4, 0)
        raw = self.find_device_or_camera_by_id(anchor_id)
        if getattr(raw, "object_type", None) == "rack":
            return (0, 0)
        port = cable.start_port if end_index == 0 else cable.end_port
        resolved_id = self._resolve_through_pass_throughs(anchor_id, port, arriving_cable=cable)
        resolved = self.find_device_or_camera_by_id(resolved_id) if resolved_id else None
        if resolved is None or getattr(resolved, "object_type", None) != "device":
            return (4, 0)
        degree = len(self._switched_edges().get(resolved.id, ()))
        category = (resolved.spec or {}).get("category")
        return ((1 if category in ("switch", "nvr") else 3), -degree)

    JUNCTION_MERGE_DISTANCE = 8.0

    def _split_cable_at(self, cable, junction, seg_index):
        """Ensures `cable` has a vertex exactly at `junction`; returns its index.

        Reuses a vertex already sitting there rather than stacking a second one on top,
        so several branches joining at the same spot converge on one shared point
        instead of a cluster of near-identical ones.
        """
        for index in (seg_index - 1, seg_index):
            if 0 <= index < len(cable.points):
                existing = cable.points[index]
                if distance((existing.x(), existing.y()),
                            (junction.x(), junction.y())) <= self.JUNCTION_MERGE_DISTANCE:
                    return index

        points = list(cable.points)
        anchors = list(cable.vertex_anchors)
        points.insert(seg_index, QPointF(junction))
        anchors.insert(seg_index, None)  # a bend in free space, attached to nothing
        cable.set_points(points, anchors=anchors)
        return seg_index

    def follow_bundle_path(self, click_pt, split=False):
        """Waypoints tracing an existing bundle from click_pt back to what it feeds.

        Wiring a floor of offices means drawing the same corridor run over and over,
        one wall drop at a time. This reads the route off a run that already goes there:
        land on the bundle and it copies the rest of the path. Because the waypoints are
        copied at identical coordinates, recalculate_all_cable_offsets sees them as the
        same physical run and bundles them with no extra work.

        `split` inserts the junction vertex into the run being followed, which is what
        makes the two genuinely share a segment. It MUTATES that run, so only the commit
        path passes it -- the live preview calls this on every mouse-move, and splitting
        there carved a fresh vertex into the trunk for every pixel the cursor travelled.

        Returns None when the click isn't on a cable.
        """
        hit = self._cable_at_point(click_pt)
        if hit is None:
            return None
        cable, junction, seg_index = hit

        path = [QPointF(junction)]
        visited = set()
        for _ in range(self.FOLLOW_MAX_HOPS):
            if cable.id in visited:
                break  # a loop in the cable plant; stop rather than spin
            visited.add(cable.id)

            # Split the run being followed at the junction, so both cables genuinely
            # share the vertex they meet at. Without this the new cable's first shared
            # stretch starts mid-segment: geometrically it lies right on top of the run
            # it is following, but the two segments have different endpoints, so
            # recalculate_all_cable_offsets sees two unrelated solo runs. They draw over
            # each other looking bundled, while never thickening and never fanning out.
            if split:
                junction_index = self._split_cable_at(cable, junction, seg_index)
                before = cable.points[:junction_index]
                after = cable.points[junction_index + 1:]
            else:
                # Preview: describe the path the split WOULD produce without touching
                # the cable. The junction sits between seg_index-1 and seg_index, so the
                # two halves are simply the points either side of it.
                before = cable.points[:seg_index]
                after = cable.points[seg_index:]

            if self._upstream_score(cable, -1) <= self._upstream_score(cable, 0):
                tail, end_index = list(after), -1
            else:
                tail, end_index = list(reversed(before)), 0
            path.extend(QPointF(p) for p in tail)

            anchors = cable.vertex_anchors or []
            if anchors and anchors[end_index]:
                break  # landed on real equipment -- done

            # The run ends loose in space. If another cable passes through that point
            # this is a bundle handing off to a bigger one, so keep going.
            hop = self._cable_at_point(path[-1], exclude=visited)
            if hop is None:
                break
            cable, junction, seg_index = hop

        # Copied endpoints and hop junctions can land on top of each other.
        deduped = [path[0]]
        for pt in path[1:]:
            if distance((pt.x(), pt.y()), (deduped[-1].x(), deduped[-1].y())) > 0.5:
                deduped.append(pt)
        return deduped

    def _closest_cable_to_point(self, pt):
        """Returns (cable, distance) for whichever existing cable's line is closest to pt,
        or (None, None) if there are no cables. Used to require real precision before
        treating a click as "insert a vertex into this cable" (see the CABLE TOOL case
        in mousePressEvent) rather than the looser radius snap_to_devices uses."""
        best_cable, best_dist = None, None
        for cable in self.get_cables():
            for i in range(1, len(cable.points)):
                a = (cable.points[i - 1].x(), cable.points[i - 1].y())
                b = (cable.points[i].x(), cable.points[i].y())
                proj, _t = closest_point_on_segment((pt.x(), pt.y()), a, b)
                d = distance((pt.x(), pt.y()), proj)
                if best_dist is None or d < best_dist:
                    best_dist = d
                    best_cable = cable
        return best_cable, best_dist

    def snap_to_devices(self, pt):
        """Snaps a cable waypoint to, in priority order: a nearby camera/device, an existing
        cable's vertex (reusing a corner), or the nearest point along an existing cable's
        line (not just its endpoints). Line-snapping is what lets a new run drawn roughly
        alongside an existing one land exactly on top of it instead of running parallel-but-
        slightly-off, so recalculate_all_cable_offsets recognizes them as one bundle."""
        snap_radius = 20.0

        # Cameras, switches/NVRs, racks (an empty rack has no mounted device to match),
        # then custom objects -- an arbitrary named object is just as valid a cable
        # endpoint as anything catalog-backed. point_hits_item also accepts a click
        # anywhere inside the icon's shape, not just within snap_radius of its centre,
        # which is what a big icon like a rack (34x44) needs to be reliably targetable.
        for item in (self.get_cameras() + self.get_network_devices()
                     + self.get_racks() + self.get_custom_objects()):
            if point_hits_item(item, pt, snap_radius):
                return item.pos()

        # Check existing cable vertices (reuse a corner to join a bundle)
        for cable in self.get_cables():
            for vertex in cable.points:
                if distance((pt.x(), pt.y()), (vertex.x(), vertex.y())) <= snap_radius:
                    return QPointF(vertex)

        # Snap onto the nearest point along any existing cable's line
        best_proj = None
        best_dist = snap_radius
        for cable in self.get_cables():
            for i in range(1, len(cable.points)):
                a = (cable.points[i - 1].x(), cable.points[i - 1].y())
                b = (cable.points[i].x(), cable.points[i].y())
                proj, _t = closest_point_on_segment((pt.x(), pt.y()), a, b)
                d = distance((pt.x(), pt.y()), proj)
                if d < best_dist:
                    best_dist = d
                    best_proj = proj
        if best_proj is not None:
            return QPointF(best_proj[0], best_proj[1])

        return pt

    def complete_cable_drawing(self):
        if len(self.cable_draw_points) < 2:
            # Stay armed on whichever tool (cable/fiber) is active rather than
            # bouncing back to Select -- an aborted attempt shouldn't force a re-pick
            # of the tool any more than a successful one should (see below).
            self.cleanup_temp_shapes()
            self.scale_changed.emit(f"Scale: 1 ft = {self.scale_ratio:.1f} px" if self.scale_ratio else "⚠ Not Calibrated")
            return

        self.push_undo_snapshot()
        # Create Cable item
        cable = CableItem()

        # Any waypoint sitting exactly on a camera/device/rack becomes anchored to it, so
        # the cable follows if it's later moved. Waypoints "out in space" stay free.
        # Devices mounted inside a rack are deliberately excluded here even though
        # get_network_devices() includes them -- they sit at the exact same position as
        # their rack (nothing else to visually aim a hand-drawn waypoint at), so a cable
        # anchors to the rack itself rather than an arbitrary device inside it. That way
        # it stays valid across however the rack gets reorganized inside.
        loose_devices = [item for item in self.scene_obj.items() if getattr(item, "object_type", None) == "device"]
        devices_and_cameras = self.get_cameras() + loose_devices + self.get_racks() + self.get_custom_objects()
        anchors = []
        for pt in self.cable_draw_points:
            anchor_id = None
            for item in devices_and_cameras:
                if distance((pt.x(), pt.y()), (item.x(), item.y())) < 1.0:
                    anchor_id = item.id
                    break
            anchors.append(anchor_id)

        cable.set_points(list(self.cable_draw_points), anchors=anchors)

        # Track the camera's primary uplink cable at whichever end it's anchored, and
        # if the *other* end is anchored to a switch/NVR, record that connection too --
        # otherwise a hand-drawn run never registers as a connection anywhere that reads
        # connected_device_id (the switch's "Connected Clients" list, the camera's own
        # "Connect to Switch/NVR" dropdown), even though the cable visually joins them.
        end_anchor_ids = (anchors[0], anchors[-1])
        for item in devices_and_cameras:
            if item.object_type == "camera" and item.id in end_anchor_ids:
                item.connected_cable_id = cable.id
                other_end = end_anchor_ids[1] if item.id == end_anchor_ids[0] else end_anchor_ids[0]
                other_item = self.find_device_or_camera_by_id(other_end) if other_end else None
                if other_item is not None and getattr(other_item, "object_type", None) == "device":
                    item.connected_device_id = other_item.id

        is_fiber = self.active_tool == "fiber"
        cable.cable_type = "Fiber" if is_fiber else "CAT6"
        kind = "Fiber" if is_fiber else "Cable"
        same_kind_count = len([c for c in self.get_cables() if c.cable_type == cable.cable_type]) + 1
        cable.label = f"{kind} {same_kind_count}"
        cable.setVisible(self.scene_obj.global_show_cables)

        self.scene_obj.addItem(cable)

        # A run into a multi-gang wall plate is really one run per outlet -- drawing it
        # six times down the same corridor is busywork, so the rest are spawned here.
        # Both cable tools end up in this method (the plain one and Ctrl+click
        # follow-the-bundle), which is exactly why they behave identically.
        self.spawn_multidrop_runs(cable)

        self.scene_obj.clearSelection()
        cable.setSelected(True)
        self.item_selected.emit(cable)
        
        # Recalculate stacking parallel cables
        recalculate_all_cable_offsets(self.scene_obj)
        self.refresh_connectivity_badges()

        # Stay armed on the Cable/Fiber tool -- ready to start the next run
        # immediately, without having to reselect the tool after every single cable.
        self.cleanup_temp_shapes()
        self.scale_changed.emit(f"Scale: 1 ft = {self.scale_ratio:.1f} px" if self.scale_ratio else "⚠ Not Calibrated")

    # ── Multi-gang wall drops ──
    # What can sit at the far end of a home run: the MDF, a panel, a switch/NVR, or
    # nothing anchored at all (a Ctrl+click branch landing on a trunk out in the middle
    # of the floor). A camera or an access point is the opposite case -- one device
    # needs one cable, and the leg from a plate out to it must never be multiplied.
    HOME_RUN_SOURCES = ("patch-panel", "switch", "nvr")

    def _feeds_home_runs(self, item):
        if item is None:
            return True   # a free end is a trunk or a corridor, not a single device
        if getattr(item, "object_type", None) == "rack":
            return True
        return getattr(item, "category", None) in self.HOME_RUN_SOURCES

    def multidrop_target(self, cable):
        """(drop, end_index) when this run is a home run into a multi-gang drop.

        Either end may be the drop: the plain cable tool draws MDF -> plate, while
        Ctrl+click starts at the plate and follows the bundle back.
        """
        if not getattr(self, "multidrop_auto_cables", True):
            return (None, None)
        if cable.cable_type != "CAT6":
            return (None, None)   # fiber doesn't land on a keystone plate
        anchors = list(cable.vertex_anchors or [])
        if len(anchors) < 2:
            return (None, None)
        for end_index in (0, -1):
            drop = self.find_device_or_camera_by_id(anchors[end_index])
            if (drop is None or getattr(drop, "category", None) != "drop"
                    or (getattr(drop, "port_count", 1) or 1) < 2):
                continue
            far_end = self.find_device_or_camera_by_id(anchors[-1 if end_index == 0 else 0])
            if self._feeds_home_runs(far_end):
                return (drop, end_index)
        return (None, None)

    @staticmethod
    def multidrop_label(drop, port):
        """The outlet this run lands on, named for its plate: "(Wall Drop 3)-2"."""
        return f"({drop.label})-{port}"

    def spawn_multidrop_runs(self, cable):
        """Duplicate `cable` once per still-free outlet on the drop it lands on.

        The drawn run takes the first free outlet and the copies take the rest, so a
        second run drawn to the same plate later fills what's left rather than doubling
        up. They share a path exactly, which is what makes the fan-out logic draw them
        as the single bundle they physically are.
        """
        drop, end_index = self.multidrop_target(cable)
        if drop is None:
            return []
        free_ports = [port for port in range(1, (drop.port_count or 1) + 1)
                      if drop.port_free_for_new_cable(port, self, "CAT6", ignore_cable=cable)]
        if not free_ports:
            return []   # every outlet already has a run; this one is a spare

        port_attribute = "start_port" if end_index == 0 else "end_port"
        setattr(cable, port_attribute, free_ports[0])
        cable.label = self.multidrop_label(drop, free_ports[0])

        siblings = []
        for port in free_ports[1:]:
            twin = CableItem()
            twin.cable_type = cable.cable_type
            # Fresh points: sharing QPointF objects would tie the copies' geometry to
            # the original's, so dragging one vertex later would move all six.
            twin.set_points([QPointF(pt) for pt in cable.points],
                            anchors=list(cable.vertex_anchors))
            # After set_points, which clears ports whenever an end's anchor changes.
            setattr(twin, port_attribute, port)
            twin.label = self.multidrop_label(drop, port)
            twin.setVisible(self.scene_obj.global_show_cables)
            self.scene_obj.addItem(twin)
            siblings.append(twin)
        return siblings

    # ── Lasso (cable dressing) ──
    def _lasso_crossings(self, p1, p2):
        """[(cable, segment_index, crossing_point)] for every cable the lasso line cuts.

        One crossing per cable -- the first one along the cable's own path. A cable that
        happens to weave back across the line twice would otherwise get two vertices
        both dragged to the same spot, which is a zero-length detour that does nothing
        but complicate the run.
        """
        a = (p1.x(), p1.y())
        b = (p2.x(), p2.y())
        found = []
        for cable in self.get_cables():
            if cable.cable_type == "Patch":
                continue  # internal rack jumpers aren't drawn on the floor plan
            points = cable.points
            for i in range(1, len(points)):
                hit = segment_intersection(a, b, (points[i - 1].x(), points[i - 1].y()),
                                            (points[i].x(), points[i].y()))
                if hit is not None:
                    found.append((cable, i, QPointF(hit[0], hit[1])))
                    break
        return found

    def apply_lasso(self, p1, p2):
        """Gather every cable crossing the p1-p2 line into a single shared point.

        Drops a new vertex into each crossed cable at the midpoint of the lasso line.
        Because they all land on exactly the same point, recalculate_all_cable_offsets
        then sees the converging segments as one bundle and draws them merged instead of
        as a fan of separate strands -- which is the whole point: turning a starburst of
        runs out of a rack into dressed cabling.

        Returns how many cables were bundled.
        """
        crossings = self._lasso_crossings(p1, p2)
        if not crossings:
            return 0

        center = QPointF((p1.x() + p2.x()) / 2.0, (p1.y() + p2.y()) / 2.0)
        self.push_undo_snapshot()
        for cable, index, _hit in crossings:
            points = list(cable.points)
            anchors = list(cable.vertex_anchors) or [None] * len(points)
            if len(anchors) < len(points):
                anchors += [None] * (len(points) - len(anchors))
            # Inserted strictly between existing vertices, so neither end's anchor (or
            # its port assignment) is disturbed.
            points.insert(index, QPointF(center))
            anchors.insert(index, None)
            cable.set_points(points, anchors=anchors)

        recalculate_all_cable_offsets(self.scene_obj)
        self.refresh_connectivity_badges()
        return len(crossings)

    # ── Object duplication ──
    DUPLICATE_OFFSET = 40.0

    def duplicate_selected(self):
        """Clones every selected item, offset so the copies land next to (not on top
        of) the originals, and selects the copies -- consistent with most editors'
        Ctrl+D behavior. A cable is only duplicated if BOTH of its ends are things
        that got duplicated too (a run to something outside the selection has no
        duplicate endpoint to attach the copy to); a duplicated rack brings its
        mounted equipment and internal patch cables along, since an empty copy of a
        fully-built rack would be useless."""
        if not self.is_active_tab:
            return
        sel_items = self.scene_obj.selectedItems()
        if not sel_items:
            return

        self.push_undo_snapshot()
        dx = dy = self.DUPLICATE_OFFSET

        # "Access Point 1" duplicates to "Access Point 2", not "Access Point 1 Copy" --
        # equipment is numbered, and a design ends up reading like a real bill of
        # materials instead of a chain of "Copy Copy Copy". Names with no trailing
        # number have nothing to increment and still fall back to " Copy".
        used_labels = self.used_labels()

        def next_label(label):
            match = re.match(r"^(.*?)(\d+)$", label or "")
            if not match:
                candidate = f"{label} Copy"
                suffix = 2
                while candidate in used_labels:
                    candidate = f"{label} Copy {suffix}"
                    suffix += 1
            else:
                base, number = match.group(1), int(match.group(2))
                # Skip past every number already taken, so duplicating three at once
                # gives 2, 3, 4 rather than three fights over 2.
                number += 1
                while f"{base}{number}" in used_labels:
                    number += 1
                candidate = f"{base}{number}"
            used_labels.add(candidate)
            return candidate

        id_map = {}     # old id -> newly-created item (for remapping cable anchors)
        new_items = []  # (original, duplicate) for top-level items, so they can be selected after

        for item in sel_items:
            obj_type = getattr(item, "object_type", None)

            if obj_type == "camera":
                dup = CameraItem(item.spec, item.x() + dx, item.y() + dy)
                dup.label = next_label(item.label)
                dup.elevation = item.elevation
                dup.rotation_deg = item.rotation_deg
                dup.downtilt_deg = item.downtilt_deg
                dup.notes = item.notes
                dup.show_fov = item.show_fov
                dup.show_ir = item.show_ir
                dup.fov_opacity = item.fov_opacity
                id_map[item.id] = dup
                new_items.append(dup)

            elif obj_type == "device":
                dup = make_device_item(item.spec, item.x() + dx, item.y() + dy)
                dup.label = next_label(item.label)
                dup.notes = item.notes
                dup.elevation = item.elevation
                id_map[item.id] = dup
                new_items.append(dup)

            elif obj_type == "rack":
                dup = RackItem(item.x() + dx, item.y() + dy, label=next_label(item.label), ru_height=item.ru_height)
                dup.notes = item.notes
                id_map[item.id] = dup
                new_items.append(dup)

                # Bring mounted devices along, at the new rack's position (same
                # convention as RackItem.mount).
                mounted_id_map = {}
                for slot in item.slots:
                    dev = slot.device
                    new_dev = make_device_item(dev.spec, dup.x(), dup.y())
                    new_dev.label = next_label(dev.label)
                    new_dev.notes = dev.notes
                    new_dev.elevation = dev.elevation
                    dup.slots.append(RackSlot(new_dev, slot.start_ru, slot.ru_size))
                    mounted_id_map[dev.id] = new_dev.id
                    id_map[dev.id] = new_dev

                # Patch cables purely internal to this rack (both ends among its
                # mounted devices) get duplicated between the corresponding new ones.
                for cable in self.get_cables():
                    if cable.cable_type != "Patch":
                        continue
                    anchors = cable.vertex_anchors
                    if not anchors or anchors[0] not in mounted_id_map or anchors[-1] not in mounted_id_map:
                        continue
                    new_cable = CableItem()
                    new_cable.cable_type = "Patch"
                    new_cable.label = cable.label
                    new_cable.notes = cable.notes
                    new_anchors = [mounted_id_map[a] for a in anchors]
                    new_cable.set_points([dup.pos()] * len(cable.points), anchors=new_anchors)
                    new_cable.start_port = cable.start_port
                    new_cable.end_port = cable.end_port
                    new_cable.setVisible(False)  # internal to the rack, not drawn on the floor plan
                    self.scene_obj.addItem(new_cable)

            elif obj_type == "custom":
                dup = CustomItem(item.x() + dx, item.y() + dy, label=next_label(item.label), icon_type=item.icon_type)
                dup.notes = item.notes
                id_map[item.id] = dup
                new_items.append(dup)

            elif obj_type == "label":
                dup = LabelItem(item.x() + dx, item.y() + dy, text=item.text)
                dup.font_size = item.font_size
                dup.text_color = item.text_color
                dup.outline_color = item.outline_color
                dup.notes = item.notes
                new_items.append(dup)

            elif obj_type == "zone":
                new_points = [QPointF(p.x() + dx, p.y() + dy) for p in item.points]
                dup = ZoneItem(new_points, label=next_label(item.label))
                dup.notes = item.notes
                dup.fill_color = item.fill_color
                dup.fill_opacity = item.fill_opacity
                dup.border_color = item.border_color
                dup.is_box = item.is_box
                dup.label_position = item.label_position
                new_items.append(dup)

            # Field/fiber cables are handled in the pass below, once id_map is complete.

        for dup in new_items:
            self.scene_obj.addItem(dup)

        for item in sel_items:
            if getattr(item, "object_type", None) != "cable" or item.cable_type == "Patch":
                continue
            anchors = item.vertex_anchors
            if not anchors:
                continue
            start_id, end_id = anchors[0], anchors[-1]
            if start_id not in id_map or end_id not in id_map:
                continue
            new_cable = CableItem()
            new_cable.cable_type = item.cable_type
            new_cable.label = item.label
            new_cable.notes = item.notes
            new_points = [QPointF(p.x() + dx, p.y() + dy) for p in item.points]
            new_anchors = [id_map[a].id if a in id_map else None for a in anchors]
            new_cable.set_points(new_points, anchors=new_anchors)
            new_cable.start_port = item.start_port
            new_cable.end_port = item.end_port
            new_cable.setVisible(self.scene_obj.global_show_cables)
            self.scene_obj.addItem(new_cable)
            new_items.append(new_cable)

        recalculate_all_cable_offsets(self.scene_obj)
        self.refresh_connectivity_badges()

        self.scene_obj.clearSelection()
        for dup in new_items:
            dup.setSelected(True)
        if new_items:
            self.item_selected.emit(new_items[-1])

    # ── Object deletion ──
    def delete_selected(self):
        # Refuse outright unless the Canvas is the tab actually on screen -- Qt's
        # keyboard focus can end up back here even while looking at a Rack Editor (or
        # Network Diagram) tab, and a Delete/Backspace meant for something in THAT tab
        # has no business touching the Canvas's selection at all. This isn't a
        # judgment call like the populated-rack confirmation below -- there is simply
        # no legitimate way to trigger this while the Canvas isn't visible.
        if not self.is_active_tab:
            return

        sel_items = self.scene_obj.selectedItems()
        if not sel_items:
            return

        # A populated rack represents real, easy-to-lose-track-of work -- mounted
        # equipment, cable routing, patch cables -- so losing one to an accidental
        # selection + stray Delete/Backspace is a much bigger deal than losing a
        # single camera or cable. Confirm before it actually happens rather than only
        # after the fact.
        populated_racks = [item for item in sel_items
                            if getattr(item, "object_type", None) == "rack" and item.slots]
        if populated_racks and _confirm_rack_delete_enabled():
            names = ", ".join(r.label for r in populated_racks)
            plural = "s" if len(populated_racks) > 1 else ""
            res = QMessageBox.question(
                self, "Delete Populated Rack" + plural,
                f"{names} still has equipment mounted inside. Delete it anyway? "
                f"Mounted devices and their cable routing will be lost.",
                QMessageBox.Yes | QMessageBox.No)
            if res != QMessageBox.Yes:
                return

        self.push_undo_snapshot()

        # A patch cable is a purely internal jumper with no floor-plan existence of its
        # own -- it jumpers the FRONT of a passive jack (patch panel / wall drop) port to
        # a switch, serving the field run punched down on the back. Delete that field run,
        # or the equipment either end sits on, and the jumper is left dangling: carrying
        # nothing, but still counted by cables_at_port, so the port keeps reading occupied
        # (green in the Rack Editor) and the switch port it consumes stays unavailable.
        # Collect what's about to stop existing and sweep those jumpers below -- the same
        # "one end gone means the whole cable goes" rule the Rack Editor already applies
        # when unmounting equipment.
        removed_device_ids = set()
        orphaned_patch_ports = set()

        for item in sel_items:
            # Clean up stale bookkeeping references -- but never delete a cable just
            # because one end's anchor is going away. A cable anchored to a
            # camera/device/rack that's about to be removed frees into a dangling free
            # vertex automatically (sync_anchored_vertices, via recalculate_all_cable_
            # offsets below, once the anchor target is no longer findable in the
            # scene) -- exactly what lets reattach_free_vertices_near re-terminate it
            # on a replacement afterward instead of redrawing every run from scratch.
            # Deleting the cable outright here (as this used to do, via
            # connect_camera_to_device(..., None)) would silently wipe out every run
            # connected to whatever device/NVR/rack you just deleted.
            if getattr(item, "object_type", None) == "camera":
                pass  # its own cable end frees automatically once removed below

            elif getattr(item, "object_type", None) == "device":
                removed_device_ids.add(item.id)
                for cam in self.get_cameras():
                    if cam.connected_device_id == item.id:
                        cam.connected_device_id = None

            elif getattr(item, "object_type", None) == "rack":
                mounted_ids = {slot.device.id for slot in item.slots}
                removed_device_ids |= mounted_ids
                for cam in self.get_cameras():
                    if cam.connected_device_id in mounted_ids:
                        cam.connected_device_id = None

            elif getattr(item, "object_type", None) == "cable":
                # Clear references to this cable in connected cameras/devices -- both
                # fields, not just connected_cable_id, or a camera whose only link to a
                # switch was this cable would still read as "connected" afterwards via
                # its now-stale connected_device_id.
                for cam in self.get_cameras():
                    if cam.connected_cable_id == item.id:
                        cam.connected_cable_id = None
                        cam.connected_device_id = None
                # Port occupancy itself is derived live from each cable's own
                # start_port/end_port/start_device_id/end_device_id (see
                # DeviceItem.cables_at_port) rather than cached anywhere, so dropping
                # this cable from the scene below is all it takes to free its ports.
                # What doesn't clean itself up is the patch cable on the other side of
                # a passive jack, which only ever existed to carry THIS run onward.
                if item.cable_type != "Patch":
                    for dev_id, port in ((item.start_device_id, item.start_port),
                                         (item.end_device_id, item.end_port)):
                        if dev_id is None or port is None:
                            continue
                        dev = self.find_device_or_camera_by_id(dev_id)
                        if (dev is not None
                                and getattr(dev, "object_type", None) == "device"
                                and dev.is_pass_through()):
                            orphaned_patch_ports.add((dev_id, port))

            if item is self.editing_cable:
                self.editing_cable = None
            if item is self.editing_zone:
                self.editing_zone = None

            self.scene_obj.removeItem(item)

        for cable in list(self.get_cables()):
            if cable.cable_type != "Patch" or cable.scene() is None:
                continue
            if (cable.start_device_id in removed_device_ids
                    or cable.end_device_id in removed_device_ids
                    or (cable.start_device_id, cable.start_port) in orphaned_patch_ports
                    or (cable.end_device_id, cable.end_port) in orphaned_patch_ports):
                self.scene_obj.removeItem(cable)

        # Update offsets for parallel cabling
        recalculate_all_cable_offsets(self.scene_obj)
        self.refresh_connectivity_badges()
        self.item_selected.emit(None)


# Opt-in performance probe (LENSE_PERF=1), a no-op otherwise. Installed out here rather
# than inside the methods so the normal path keeps its original, unwrapped functions and
# pays nothing at all for the instrumentation.
if perf.enabled:
    CanvasView.mouseMoveEvent = perf.wrap("mouse_move", CanvasView.mouseMoveEvent)
    CanvasView.mousePressEvent = perf.wrap("mouse_press", CanvasView.mousePressEvent)
    CanvasView.paintEvent = perf.wrap("paint", CanvasView.paintEvent)
    CanvasScene.drawBackground = perf.wrap("draw_background", CanvasScene.drawBackground)
    # Rebinds the module global the methods above call, so the wrapper is what they hit.
    recalculate_all_cable_offsets = perf.wrap("cable_bundling", recalculate_all_cable_offsets)
