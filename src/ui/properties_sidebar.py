import os
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QLabel, QFormLayout, QLineEdit,
                             QAbstractSpinBox, QApplication,
                             QDoubleSpinBox, QSlider, QCheckBox, QTextEdit, QScrollArea,
                             QGroupBox, QComboBox, QHBoxLayout, QPushButton, QColorDialog)
from PySide6.QtCore import Qt, Signal, Slot, QObject, QEvent
from PySide6.QtGui import QPixmap, QColor, QDoubleValidator
from src.core.utils import format_distance_both, get_data_dir, get_ir_range_feet
from src.graphics.zone_item import LABEL_POSITIONS
from src.core import poe_chain


def _spec_sfp_port_count(spec):
    """Same lookup DeviceItem.sfp_port_count uses (top-level "sfpPorts", falling back
    to ports["sfp"] for the NVR-style nested shape) -- kept in sync manually since this
    reads a raw catalog spec dict, not an already-constructed DeviceItem."""
    raw_sfp = spec.get("sfpPorts")
    if not (isinstance(raw_sfp, int) and raw_sfp > 0):
        raw_ports = spec.get("ports")
        if isinstance(raw_ports, dict):
            raw_sfp = raw_ports.get("sfp")
    return raw_sfp if isinstance(raw_sfp, int) and raw_sfp > 0 else None


class _WheelGuard(QObject):
    """Stops a hovered spin box / combo / slider from eating the scroll wheel.

    Qt hands wheel events to whatever widget is under the cursor, so scrolling the
    properties panel would silently change whatever value happened to be passing under
    the pointer -- elevation, opacity, model, rack height. Here the value only responds
    to the wheel once it has been deliberately focused (clicked into); otherwise the
    scroll is passed on to the panel so it just scrolls, as expected.
    """

    def __init__(self, scroll_area):
        super().__init__(scroll_area)
        self._scroll_area = scroll_area

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Wheel and not obj.hasFocus():
            QApplication.sendEvent(self._scroll_area.viewport(), event)
            return True
        return False


class PropertiesSidebar(QScrollArea):
    # Signals to notify canvas of changes
    property_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        # Core container
        self.container = QWidget()
        self.container.setObjectName("container")  # matches the QWidget#container rule
        # in main_window.py's dark stylesheet -- without this the selector matches
        # nothing and the scroll area's actual content widget (not the QScrollArea
        # frame around it) falls back to the palette's default light background.
        self.layout = QVBoxLayout(self.container)
        self.layout.setContentsMargins(10, 10, 10, 10)
        self.layout.setSpacing(12)
        self.setWidget(self.container)

        self.current_item = None
        self.canvas_view = None
        self._wheel_guard = _WheelGuard(self)

        # Build UI placeholders
        self.show_empty()

    def set_canvas_view(self, view):
        self.canvas_view = view

    def show_empty(self):
        # Clear layout
        self.clear_layout()
        
        # Empty placeholder layout
        label = QLabel("Select an object to view properties")
        label.setAlignment(Qt.AlignCenter)
        label.setStyleSheet("color: #8888a0; font-size: 13px; font-weight: 500;")
        
        hint = QLabel("Click on a camera, cable, switch, NVR, custom object, label, or zone on the canvas")
        hint.setAlignment(Qt.AlignCenter)
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #555568; font-size: 11px;")

        self.layout.addStretch()
        self.layout.addWidget(label)
        self.layout.addWidget(hint)
        self.layout.addStretch()

    def _guard_value_widgets(self):
        """Apply the wheel guard to every value widget in the freshly-built panel.

        Done by walking the finished panel rather than at each widget's construction
        site, so it covers every section without needing to remember it in each one.
        """
        # PySide6's findChildren takes a single type, so query each one.
        for widget_type in (QAbstractSpinBox, QComboBox, QSlider):
            for widget in self.container.findChildren(widget_type):
                widget.setFocusPolicy(Qt.StrongFocus)  # don't take focus just from hovering
                widget.installEventFilter(self._wheel_guard)

    def clear_layout(self):
        while self.layout.count():
            item = self.layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

    def build_thumbnail(self, spec):
        """Returns a QLabel showing the catalog spec's product image, or None if it has none."""
        image_path = spec.get("imagePath") if spec else None
        if not image_path:
            return None
        full_path = os.path.join(get_data_dir(), image_path)
        if not os.path.exists(full_path):
            return None
        pixmap = QPixmap(full_path)
        if pixmap.isNull():
            return None
        label = QLabel()
        label.setPixmap(pixmap.scaledToWidth(220, Qt.SmoothTransformation))
        label.setAlignment(Qt.AlignCenter)
        label.setStyleSheet("background-color: #111118; border: 1px solid #2e2e3f; "
                             "border-radius: 6px; padding: 8px;")
        return label

    # ── CATALOG PREVIEW (click an item in the equipment catalog, before placing it) ──
    def preview_catalog_item(self, spec):
        self.current_item = None
        self.clear_layout()
        category = spec.get("category", "camera")

        thumb = self.build_thumbnail(spec)
        if thumb:
            self.layout.addWidget(thumb)

        id_group = QGroupBox("Catalog Preview")
        id_layout = QFormLayout(id_group)
        id_layout.addRow("Manufacturer:", QLabel(spec.get("manufacturer", "Unknown")))
        id_layout.addRow("Model:", QLabel(spec.get("model", "Unknown")))
        id_layout.addRow("MSRP:", QLabel(f"${spec.get('msrp', 0):,.0f}"))
        self.layout.addWidget(id_group)

        spec_group = QGroupBox("Specifications")
        spec_layout = QFormLayout(spec_group)
        if category == "camera":
            spec_layout.addRow("Resolution:", QLabel(spec.get("resolution", "N/A")))
            spec_layout.addRow("H-FOV:", QLabel(f"{spec.get('fov', {}).get('horizontal', 0)}°"))
            spec_layout.addRow("V-FOV:", QLabel(f"{spec.get('fov', {}).get('vertical', 0)}°"))
            ir_feet_display = get_ir_range_feet(spec, None)
            spec_layout.addRow("IR Range:", QLabel(f"{ir_feet_display} ft" if ir_feet_display is not None else "N/A (no IR illuminator)"))
            spec_layout.addRow("Night Vision:", QLabel(spec.get("nightVision", "N/A")))
            spec_layout.addRow("Zoom Type:", QLabel(spec.get("zoom", {}).get("type", "N/A")))
            spec_layout.addRow("IP Rating:", QLabel(spec.get("ipRating") or "N/A"))
            spec_layout.addRow("PoE:", QLabel("Yes" if spec.get("poe") else "No"))
        elif category == "switch":
            spec_layout.addRow("Total Ports:", QLabel(str(spec.get("ports", "N/A"))))
            spec_layout.addRow("PoE Ports:", QLabel(str(spec.get("poePorts", 0))))
            spec_layout.addRow("PoE Budget:", QLabel(f"{spec.get('poeBudget', 0)} W"))
            spec_layout.addRow("Managed:", QLabel("Yes" if spec.get("managed") else "No"))
            sfp_count = _spec_sfp_port_count(spec)
            if sfp_count:
                spec_layout.addRow("SFP Ports:", QLabel(str(sfp_count)))
        elif category == "nvr":
            spec_layout.addRow("Max Channels:", QLabel(str(spec.get("maxChannels", "N/A"))))
            spec_layout.addRow("Drive Bays:", QLabel(str(spec.get("driveBays", "N/A"))))
            spec_layout.addRow("Built-in PoE:", QLabel("Yes" if spec.get("builtInPoe") else "No"))
            ports = spec.get("ports")
            if isinstance(ports, dict) and ports.get("rj45"):
                spec_layout.addRow("RJ45 Uplink Ports:", QLabel(str(ports.get("rj45"))))
            sfp_count = _spec_sfp_port_count(spec)
            if sfp_count:
                spec_layout.addRow("SFP Ports:", QLabel(str(sfp_count)))
        self.layout.addWidget(spec_group)

        extra_rows = [
            ("Dimensions:", spec.get("dimensions")),
            ("Weight:", spec.get("weight")),
            ("Operating Temp:", spec.get("operatingTemp")),
        ]
        extra_rows = [(label_text, value) for label_text, value in extra_rows if value]
        if extra_rows:
            extra_group = QGroupBox("Additional Info")
            extra_layout = QFormLayout(extra_group)
            for label_text, value in extra_rows:
                extra_layout.addRow(label_text, QLabel(value))
            self.layout.addWidget(extra_group)

        hint = QLabel("Double-click, drag onto the canvas, or use the Place Equipment tool to add this.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #555568; font-size: 11px;")
        self.layout.addWidget(hint)
        self.layout.addStretch()
        self._guard_value_widgets()

    def select_item(self, item):
        self.current_item = item
        if not item:
            self.show_empty()
            return

        self.clear_layout()
        item_type = getattr(item, "object_type", None)

        if item_type == "camera":
            self.build_camera_properties(item)
        elif item_type == "cable":
            self.build_cable_properties(item)
        elif item_type == "device":
            self.build_device_properties(item)
        elif item_type == "custom":
            self.build_custom_properties(item)
        elif item_type == "label":
            self.build_label_properties(item)
        elif item_type == "zone":
            self.build_zone_properties(item)
        elif item_type == "rack":
            self.build_rack_properties(item)
        else:
            self.show_empty()

        self._guard_value_widgets()

    # ── CAMERA PROPERTIES ──
    def build_cable_allowance_group(self, item):
        """Per-object rise, service loop and termination, or follow the defaults.

        Inheriting is the normal state: the placeholder shows the number actually in
        force so the field is never a mystery, and typing over it pins this object to
        its own value. Clearing it hands the object back to the category default.
        """
        from src.core import cable_length, termination
        category = cable_length.category_of(item)
        if category is None:
            return None

        group = QGroupBox("Cabling")
        form = QFormLayout(group)

        def add_row(caption, attribute, tip):
            edit = QLineEdit()
            edit.setValidator(QDoubleValidator(0.0, 500.0, 1, edit))
            inherited = cable_length.is_inherited(item, attribute)
            effective = (cable_length.rise_feet(item) if attribute == "vertical_rise"
                         else cable_length.loop_feet(item))
            edit.setPlaceholderText(f"{effective:g} ft (default)")
            if not inherited:
                edit.setText(f"{float(getattr(item, attribute)):g}")
            edit.setToolTip(tip + "\nLeave blank to follow Settings \u203a Cable allowances.")
            edit.editingFinished.connect(
                lambda e=edit, a=attribute: self.update_cable_allowance(item, a, e))
            form.addRow(caption, edit)

        add_row("Vertical rise:", "vertical_rise",
                "Cable spent getting between this object and the pathway.")
        add_row("Service loop:", "service_loop",
                "Slack coiled at this end.")

        # How runs land here decides real hardware -- a plug, or a jack plus a jumper --
        # so it is quoted per connection in the Inventory panel. See core/termination.py.
        method_combo = QComboBox()
        inherited_label = termination.METHOD_LABELS[termination.default_method(category)]
        method_combo.addItem(f"Default \u2014 {inherited_label}", None)
        for method in termination.METHODS:
            method_combo.addItem(termination.METHOD_LABELS[method], method)
        current = termination.normalize(getattr(item, "termination", None))
        method_combo.setCurrentIndex(max(0, method_combo.findData(current)))
        method_combo.setToolTip(
            "How every run landing on this object is terminated.\n"
            "RJ45 plug: one crimped plug per connection.\n"
            "Jack & patch cable: one jack per connection, plus a patch cable to the gear.")
        # Connected after setCurrentIndex so building the panel never looks like an edit.
        method_combo.currentIndexChanged.connect(
            lambda _index, c=method_combo: self.update_termination(item, c))
        form.addRow("Termination:", method_combo)

        note = QLabel("Applies to every run landing on this object.")
        note.setStyleSheet("color: #8888a0; font-size: 11px;")
        note.setWordWrap(True)
        form.addRow("", note)
        return group

    def update_cable_allowance(self, item, attribute, edit):
        text = edit.text().strip()
        if not text:
            setattr(item, attribute, None)          # back to following the default
        else:
            try:
                setattr(item, attribute, max(0.0, float(text)))
            except ValueError:
                return
        if self.canvas_view:
            self.canvas_view.push_undo_snapshot()
            self.canvas_view.refresh_connectivity_badges()
            self.canvas_view.scene_obj.update()
            window = self.canvas_view.window()
            if hasattr(window, "inventory_panel") and window.inventory_panel.isVisible():
                window.inventory_panel.refresh()
        self.select_item(item)   # redraw the field as inherited/overridden

    def update_termination(self, item, combo):
        item.termination = combo.currentData()
        if self.canvas_view:
            self.canvas_view.push_undo_snapshot()
            window = self.canvas_view.window()
            if hasattr(window, "inventory_panel") and window.inventory_panel.isVisible():
                window.inventory_panel.refresh()

    def build_camera_properties(self, camera):
        spec = camera.spec

        thumb = self.build_thumbnail(spec)
        if thumb:
            self.layout.addWidget(thumb)

        # Group 1: Identity
        id_group = QGroupBox("Identity")
        id_layout = QFormLayout(id_group)
        
        mfg_lbl = QLabel(spec.get("manufacturer", "Unknown"))

        # Model is a dropdown, not a static label, so you can swap a placed camera to a
        # different catalog model in place (position/rotation/downtilt/notes are kept;
        # only the spec-driven fields -- resolution, FOV, IR range, etc. -- change).
        model_combo = QComboBox()
        camera_specs = sorted(
            (s for s in (self.canvas_view.catalog_data.values() if self.canvas_view else [])
             if s.get("category", "camera") == "camera"),
            key=lambda s: (s.get("manufacturer", ""), s.get("model", ""))
        )
        if camera_specs:
            current_index = 0
            for i, s in enumerate(camera_specs):
                model_combo.addItem(f"{s.get('manufacturer', '')} {s.get('model', '')}".strip(), s.get("id"))
                if s.get("id") == spec.get("id"):
                    current_index = i
            model_combo.setCurrentIndex(current_index)
            model_combo.currentIndexChanged.connect(
                lambda idx, combo=model_combo: self.update_camera_spec(camera, combo.itemData(idx)))
        else:
            model_combo.addItem(spec.get("model", "Unknown"))
            model_combo.setEnabled(False)

        label_edit = QLineEdit(camera.label)
        label_edit.textChanged.connect(lambda text: self.update_camera_label(camera, text))

        id_layout.addRow("Manufacturer:", mfg_lbl)
        id_layout.addRow("Model:", model_combo)
        id_layout.addRow("Label:", label_edit)
        self.layout.addWidget(id_group)

        # Group 2: Technical Specifications
        spec_group = QGroupBox("Specifications")
        spec_layout = QFormLayout(spec_group)
        spec_layout.addRow("Resolution:", QLabel(spec.get("resolution", "N/A")))
        spec_layout.addRow("H-FOV:", QLabel(f"{spec.get('fov', {}).get('horizontal', 0)}°"))
        spec_layout.addRow("V-FOV:", QLabel(f"{spec.get('fov', {}).get('vertical', 0)}°"))
        ir_feet_display = get_ir_range_feet(spec, None)
        spec_layout.addRow("IR Range:", QLabel(f"{ir_feet_display} ft" if ir_feet_display is not None else "N/A (no IR illuminator)"))
        spec_layout.addRow("Night Vision:", QLabel(spec.get("nightVision", "N/A")))
        spec_layout.addRow("Zoom Type:", QLabel(spec.get("zoom", {}).get("type", "N/A")))
        self.layout.addWidget(spec_group)

        # Group 3: Placement
        placement_group = QGroupBox("Placement")
        place_layout = QFormLayout(placement_group)
        
        # Elevation
        elev_spin = QDoubleSpinBox()
        elev_spin.setRange(0, 100)
        elev_spin.setSuffix(" ft")
        elev_spin.setValue(camera.elevation)
        elev_spin.valueChanged.connect(lambda v: self.update_camera_elevation(camera, v))
        
        # Rotation Slider & Spinbox
        rot_widget = QWidget()
        rot_layout = QHBoxLayout(rot_widget)
        rot_layout.setContentsMargins(0, 0, 0, 0)
        
        rot_spin = QDoubleSpinBox()
        rot_spin.setRange(0, 359)
        rot_spin.setSuffix("°")
        rot_spin.setValue(camera.rotation_deg)
        
        rot_slider = QSlider(Qt.Horizontal)
        rot_slider.setRange(0, 359)
        rot_slider.setValue(int(camera.rotation_deg))
        
        rot_spin.valueChanged.connect(lambda v: self.update_camera_rotation_spin(camera, v, rot_slider))
        rot_slider.valueChanged.connect(lambda v: self.update_camera_rotation_slider(camera, v, rot_spin))
        
        rot_layout.addWidget(rot_slider)
        rot_layout.addWidget(rot_spin)

        # Downtilt Slider & Spinbox
        tilt_widget = QWidget()
        tilt_layout = QHBoxLayout(tilt_widget)
        tilt_layout.setContentsMargins(0, 0, 0, 0)

        tilt_spin = QDoubleSpinBox()
        tilt_spin.setRange(0, 90)
        tilt_spin.setSuffix("°")
        tilt_spin.setValue(camera.downtilt_deg)

        tilt_slider = QSlider(Qt.Horizontal)
        tilt_slider.setRange(0, 90)
        tilt_slider.setValue(int(camera.downtilt_deg))

        tilt_spin.valueChanged.connect(lambda v: self.update_camera_downtilt_spin(camera, v, tilt_slider))
        tilt_slider.valueChanged.connect(lambda v: self.update_camera_downtilt_slider(camera, v, tilt_spin))

        tilt_layout.addWidget(tilt_slider)
        tilt_layout.addWidget(tilt_spin)

        place_layout.addRow("Elevation:", elev_spin)
        place_layout.addRow("Orientation (Pan):", rot_widget)
        place_layout.addRow("Downtilt:", tilt_widget)

        self.range_lbl = QLabel()
        self.range_lbl.setWordWrap(True)
        self._update_range_label(camera)
        place_layout.addRow("Effective Ground Range:", self.range_lbl)

        place_layout.addRow("Position X/Y:", QLabel(f"{int(camera.x())}, {int(camera.y())}"))
        self.layout.addWidget(placement_group)

        # Group 4: Visualization
        vis_group = QGroupBox("Visualization")
        vis_layout = QFormLayout(vis_group)
        
        chk_fov = QCheckBox()
        chk_fov.setChecked(camera.show_fov)
        chk_fov.stateChanged.connect(lambda state: self.update_camera_bool(camera, "show_fov", state))
        
        chk_ir = QCheckBox()
        chk_ir.setChecked(camera.show_ir)
        chk_ir.stateChanged.connect(lambda state: self.update_camera_bool(camera, "show_ir", state))
        
        opacity_slider = QSlider(Qt.Horizontal)
        opacity_slider.setRange(10, 100)
        opacity_slider.setValue(int(camera.fov_opacity * 100))
        opacity_slider.valueChanged.connect(lambda v: self.update_camera_opacity(camera, v / 100.0))

        vis_layout.addRow("Show FOV Cone:", chk_fov)
        vis_layout.addRow("Show IR Ring:", chk_ir)
        vis_layout.addRow("FOV Opacity:", opacity_slider)
        self.layout.addWidget(vis_group)

        # Group 5: Connections & Details
        conn_group = QGroupBox("Connection")
        conn_layout = QFormLayout(conn_group)
        
        # Switch selection dropdown
        self.conn_combo = QComboBox()
        self.conn_combo.addItem("None", None)
        
        # Populate switches/NVRs in the combo box -- everything else (patch panels,
        # wall drops, access points, ...) excluded, since this dropdown makes a direct
        # whole-device connection and doesn't offer port assignment; routing a camera
        # through a passive jack is a Rack Editor thing (drag the field cable onto a
        # port, then patch that port to a switch), and an access point isn't a valid
        # camera uplink target at all.
        if self.canvas_view:
            for dev in self.canvas_view.get_network_devices():
                if getattr(dev, "category", None) not in ("switch", "nvr"):
                    continue
                self.conn_combo.addItem(f"{dev.label} ({dev.spec.get('model')})", dev.id)

        # Select currently connected device. Falls back to deriving the connection from
        # cable anchor topology (including a chain walked through any patch panel(s) in
        # between), since a hand-drawn cable doesn't set connected_device_id directly --
        # see resolve_camera_chain.
        empty_chain = {"device": None, "port": None, "via_panel": None, "via_port": None}
        chain = self.canvas_view.resolve_camera_chain(camera.id) if self.canvas_view else empty_chain
        connected_id = camera.connected_device_id or (chain["device"].id if chain["device"] is not None else None)
        if connected_id:
            index = self.conn_combo.findData(connected_id)
            if index >= 0:
                self.conn_combo.setCurrentIndex(index)

        self.conn_combo.currentIndexChanged.connect(lambda idx: self.update_camera_connection(camera))
        conn_layout.addRow("Connect to Switch/NVR:", self.conn_combo)

        if chain["device"] is not None and chain["port"] is not None:
            port_name = chain["device"].port_display_name(chain["port"])
            port_lbl = QLabel(port_name)
            port_lbl.setStyleSheet("color: #8888a0; font-size: 11px;")
            conn_layout.addRow("Switch Port:", port_lbl)

        if chain["via_panel"] is not None:
            via_port_name = chain["via_panel"].port_display_name(chain["via_port"])
            via_lbl = QLabel(f"via {chain['via_panel'].label}, {via_port_name}")
            via_lbl.setStyleSheet("color: #8888a0; font-size: 11px;")
            conn_layout.addRow("Patched Through:", via_lbl)

        if not connected_id:
            warn_lbl = QLabel("⚠ Not connected to a switch/NVR")
            warn_lbl.setStyleSheet("color: #f59e0b; font-weight: bold;")
            conn_layout.addRow(warn_lbl)

        if camera.connected_cable_id:
            cable_lbl = QLabel(str(camera.connected_cable_id))
            conn_layout.addRow("Connected Cable:", cable_lbl)
            
        self.layout.addWidget(conn_group)

        allowance_group = self.build_cable_allowance_group(camera)
        if allowance_group:
            self.layout.addWidget(allowance_group)

        # Group 6: Notes
        notes_group = QGroupBox("Notes")
        notes_layout = QVBoxLayout(notes_group)
        notes_edit = QTextEdit(camera.notes)
        notes_edit.textChanged.connect(lambda: self.update_camera_notes(camera, notes_edit.toPlainText()))
        notes_layout.addWidget(notes_edit)
        self.layout.addWidget(notes_group)

    # ── CABLE PROPERTIES ──
    def build_cable_properties(self, cable):
        # Group 1: Identity & Path Details
        id_group = QGroupBox("Cable Details")
        id_layout = QFormLayout(id_group)
        
        label_edit = QLineEdit(cable.label)
        label_edit.textChanged.connect(lambda text: self.update_cable_label(cable, text))
        
        type_lbl = QLabel(cable.cable_type)
        
        # Dynamic length based on scale
        length_ft = cable.get_length_feet()
        len_str = format_distance_both(length_ft) if length_ft else "Not Calibrated (Pixels: " + str(int(cable.pixel_length)) + ")"
        length_lbl = QLabel(len_str)
        over_limit = cable.is_over_limit()
        if over_limit:
            length_lbl.setStyleSheet("color: #ef4444; font-weight: bold;")

        id_layout.addRow("Label:", label_edit)
        id_layout.addRow("Type:", type_lbl)
        id_layout.addRow("Calculated Length:", length_lbl)

        # Spell out where the number comes from. A single total invites the question
        # "measured off the plan, or the real pull?" -- this answers it in place.
        parts = cable.length_breakdown()
        if parts is not None and (parts[1] or parts[2]):
            breakdown_lbl = QLabel(f"{parts[0]:,.0f} ft across  +  {parts[1]:,.0f} ft vertical"
                                   f"  +  {parts[2]:,.0f} ft service loop")
            breakdown_lbl.setStyleSheet("color: #8888a0; font-size: 11px;")
            breakdown_lbl.setWordWrap(True)
            id_layout.addRow("", breakdown_lbl)

        # Display limit warning (fiber's run limit is far longer than copper's)
        if over_limit:
            warn_lbl = QLabel(f"⚠ EXCEEDS {cable.max_run_feet():.0f} FT LIMIT (Signal Error)")
            warn_lbl.setStyleSheet("color: #ef4444; font-weight: bold;")
            warn_lbl.setWordWrap(True)
            id_layout.addRow("", warn_lbl)

        self.layout.addWidget(id_group)

        # Group 2: Connections
        conn_group = QGroupBox("Connections")
        conn_layout = QFormLayout(conn_group)
        
        start_lbl = QLabel("None")
        end_lbl = QLabel("None")
        if self.canvas_view:
            start_dev = self.canvas_view.find_device_or_camera_by_id(cable.start_device_id)
            end_dev = self.canvas_view.find_device_or_camera_by_id(cable.end_device_id)
            # A cable end can be anchored to a rack (or a custom object), neither of
            # which is catalog-backed -- reading .spec unguarded crashed the whole
            # properties panel the moment you selected a cable run into a rack.
            def endpoint_text(item):
                spec = getattr(item, "spec", None)
                model = spec.get("model") if isinstance(spec, dict) else None
                return f"{item.label} ({model})" if model else item.label

            if start_dev:
                start_lbl.setText(endpoint_text(start_dev))
            if end_dev:
                end_lbl.setText(endpoint_text(end_dev))
                
        conn_layout.addRow("Source Device:", start_lbl)
        conn_layout.addRow("Destination Device:", end_lbl)
        self.layout.addWidget(conn_group)

        # Group 3: Notes
        notes_group = QGroupBox("Notes")
        notes_layout = QVBoxLayout(notes_group)
        notes_edit = QTextEdit(cable.notes)
        notes_edit.textChanged.connect(lambda: self.update_cable_notes(cable, notes_edit.toPlainText()))
        notes_layout.addWidget(notes_edit)
        self.layout.addWidget(notes_group)

    # ── DEVICE PROPERTIES (SWITCH / NVR) ──
    def build_port_map_group(self, device):
        """Per-port capability breakdown -- which ports are PoE+, which are PoE++, and
        which are data-only.

        This is the information that decides whether a design works (a Pro 24's ports
        1-16 leave a downstream Flex with 20W; ports 17-24 leave it 46W) and it was
        previously invisible anywhere in the UI -- the PoE logic silently used it while
        the designer had no way to see it.
        """
        spec = getattr(device, "spec", None) or {}
        groups = poe_chain.port_groups(device)
        sfp_count = getattr(device, "sfp_port_count", None)
        if not groups and not sfp_count:
            return None

        group_box = QGroupBox("Port Map")
        form = QFormLayout(group_box)

        for grp in groups:
            span = (f"{grp['first']}" if grp["count"] == 1
                    else f"{grp['first']}\u2013{grp['last']}")
            swatch = QLabel(f"  Ports {span}")
            colour = {"802.3bt": "#c2820a", "802.3at": "#5b8dd9",
                      "802.3af": "#2dd4bf"}.get(grp["standard"], "#8888a0")
            swatch.setStyleSheet(f"color: {colour}; font-weight: bold;")
            form.addRow(swatch,
                        QLabel(f"{poe_chain.class_label(grp['standard'])} "
                               f"\u00b7 up to {grp['maxWatts']:g}W per port"))

        # Work out the genuinely un-powered ports from the actual group ranges rather
        # than by counting -- powered ports don't necessarily start at port 1. The USW
        # Flex is the case in point: its port 1 is the PoE *input* that powers the
        # switch, and ports 2-5 are the outputs.
        total_rj45 = getattr(device, "port_count", None) or 0
        covered = {n for g in groups for n in range(g["first"], g["last"] + 1)}
        input_port = spec.get("poeInputPort")
        if input_port:
            lbl = QLabel(f"  Port {input_port}")
            lbl.setStyleSheet("color: #fb923c; font-weight: bold;")
            form.addRow(lbl, QLabel("PoE input \u00b7 powers this device"))
            covered.add(input_port)
        leftover = [n for n in range(1, total_rj45 + 1) if n not in covered]
        if leftover:
            span = (f"{leftover[0]}" if len(leftover) == 1
                    else f"{leftover[0]}\u2013{leftover[-1]}")
            lbl = QLabel(f"  Ports {span}" if len(leftover) > 1 else f"  Port {span}")
            lbl.setStyleSheet("color: #8888a0; font-weight: bold;")
            form.addRow(lbl, QLabel("Data only \u00b7 no PoE"))

        if sfp_count:
            base = total_rj45 + 1
            span = f"{base}" if sfp_count == 1 else f"{base}\u2013{base + sfp_count - 1}"
            lbl = QLabel(f"  SFP {span}")
            lbl.setStyleSheet("color: #a78bfa; font-weight: bold;")
            note = spec.get("sfpNote") or "Data only \u00b7 no PoE"
            form.addRow(lbl, QLabel(note.split(":", 1)[-1].strip() if ":" in note else note))

        budget = spec.get("poeBudget")
        if isinstance(budget, (int, float)) and budget > 0:
            form.addRow("Total budget:", QLabel(f"{budget:g}W shared across all PoE ports"))
        return group_box

    def build_power_chain_group(self, device):
        """Where this device's power comes from and what's left of the budget.

        Returns None for a device that neither draws nor sources PoE, so a mains-only
        box doesn't get an empty section.
        """
        if self.canvas_view is None:
            return None
        is_load = poe_chain.is_powered_device(device)
        is_source = poe_chain.sources_power(device)
        if not is_load and not is_source:
            return None

        group = QGroupBox("PoE Power")
        form = QFormLayout(group)

        if is_load:
            draw, estimated = poe_chain.power_draw(device)
            needed = poe_chain.required_class(device)
            source = poe_chain.resolve_power_source(self.canvas_view, device)

            # A switch that re-sources power negotiates down to whatever its port
            # offers rather than demanding its headline maximum, so show what it
            # actually pulls here -- "60W (PoE)" would read as 60 watts over 802.3af,
            # which is impossible.
            negotiates = isinstance(device.spec.get("poeOutputByInput"), dict)
            if negotiates and source.port_max_watts:
                draw = min(draw, source.port_max_watts)
            shown_class = source.standard if (negotiates and source.standard) else needed
            qualifier = f" ({poe_chain.class_label(shown_class)})" if shown_class else ""
            if negotiates and needed:
                qualifier += f", min {poe_chain.class_label(needed)}"
            form.addRow("Draws:", QLabel(f"{'~' if estimated else ''}{draw:g}W{qualifier}"))
            if source.device is None:
                fed = QLabel("No PoE source reaches this device")
                fed.setStyleSheet("color: #f59e0b;")
            else:
                where = f"{source.device.label}"
                if source.port:
                    where += f" port {source.port}"
                elif source.assumed_port:
                    where += " (port unassigned)"
                detail = f"{poe_chain.class_label(source.standard)} · {source.port_max_watts:g}W/port"
                if source.via:
                    detail += " · via " + ", ".join(v.label for v in source.via)
                fed = QLabel(f"{where}\n{detail}")
            fed.setWordWrap(True)
            form.addRow("Powered by:", fed)

        if is_source:
            upstream_class = None
            if is_load:
                upstream_class = poe_chain.resolve_power_source(self.canvas_view, device).standard
            budget = poe_chain.output_budget(device, upstream_class)
            loads = poe_chain.powered_devices_on(self.canvas_view, device)
            used = sum(poe_chain.power_draw(load)[0] for load in loads)
            summary = QLabel(f"{used:g}W used of {budget:g}W"
                             + (f"  ({len(loads)} device{'s' if len(loads) != 1 else ''})" if loads else ""))
            if budget and used > budget:
                summary.setStyleSheet("color: #ef4444; font-weight: bold;")
            form.addRow("Downstream:", summary)
            if is_load and upstream_class:
                form.addRow("", QLabel(f"Budget set by its {poe_chain.class_label(upstream_class)} uplink"))
        return group

    def build_device_properties(self, device):
        spec = device.spec

        thumb = self.build_thumbnail(spec)
        if thumb:
            self.layout.addWidget(thumb)

        # Group 1: Identity
        id_group = QGroupBox("Identity")
        id_layout = QFormLayout(id_group)

        mfg_lbl = QLabel(spec.get("manufacturer", "Unknown"))
        category_lbl = QLabel(spec.get("category", "Unknown").upper())

        # Model is a dropdown, not a static label, so you can swap a placed switch/NVR to
        # a different catalog model in place -- position, label, notes, and every cable
        # anchored to it are kept; only the spec-driven fields (ports, PoE budget, etc.)
        # change. Deleting and re-placing to change a model would strand every cable
        # anchored to the old device, which on a real design can mean dozens of runs.
        model_combo = QComboBox()
        same_category_specs = sorted(
            (s for s in (self.canvas_view.catalog_data.values() if self.canvas_view else [])
             if s.get("category") == spec.get("category")),
            key=lambda s: (s.get("manufacturer", ""), s.get("model", ""))
        )
        if same_category_specs:
            current_index = 0
            for i, s in enumerate(same_category_specs):
                model_combo.addItem(f"{s.get('manufacturer', '')} {s.get('model', '')}".strip(), s.get("id"))
                if s.get("id") == spec.get("id"):
                    current_index = i
            model_combo.setCurrentIndex(current_index)
            model_combo.currentIndexChanged.connect(
                lambda idx, combo=model_combo: self.update_device_spec(device, combo.itemData(idx)))
        else:
            model_combo.addItem(spec.get("model", "Unknown"))
            model_combo.setEnabled(False)

        label_edit = QLineEdit(device.label)
        label_edit.textChanged.connect(lambda text: self.update_device_label(device, text))

        id_layout.addRow("Manufacturer:", mfg_lbl)
        id_layout.addRow("Model:", model_combo)
        id_layout.addRow("Category:", category_lbl)
        id_layout.addRow("Label:", label_edit)
        self.layout.addWidget(id_group)

        # Group 2: Tech Specs
        spec_group = QGroupBox("Device Specifications")
        spec_layout = QFormLayout(spec_group)
        
        ports = spec.get("ports")
        if spec.get("category") == "switch":
            spec_layout.addRow("Total Ports:", QLabel(str(ports if ports is not None else "N/A")))
            spec_layout.addRow("PoE Ports:", QLabel(str(spec.get("poePorts", 0))))
            spec_layout.addRow("PoE Budget:", QLabel(f"{spec.get('poeBudget', 0)} W"))
            sfp_count = _spec_sfp_port_count(spec)
            if sfp_count:
                spec_layout.addRow("SFP Ports:", QLabel(str(sfp_count)))
        elif spec.get("category") == "nvr":
            spec_layout.addRow("Drive Bays:", QLabel(str(spec.get("driveBays", 0))))
            spec_layout.addRow("Max Channels:", QLabel(str(spec.get("maxChannels", 0))))
            if isinstance(ports, dict) and ports.get("rj45"):
                spec_layout.addRow("RJ45 Uplink Ports:", QLabel(str(ports.get("rj45"))))
            sfp_count = _spec_sfp_port_count(spec)
            if sfp_count:
                spec_layout.addRow("SFP Ports:", QLabel(str(sfp_count)))
            if spec.get("builtInPoe"):
                spec_layout.addRow("Built-in PoE Ports:", QLabel(str(spec.get("poePorts", 0))))
                spec_layout.addRow("PoE Budget:", QLabel(f"{spec.get('poeBudget', 0)} W"))
        else:
            spec_layout.addRow("Total Ports:", QLabel(str(ports if ports is not None else "N/A")))

        spec_layout.addRow("MSRP:", QLabel(f"${spec.get('msrp', 0)}"))
        self.layout.addWidget(spec_group)

        # Group 3: Connected Clients List
        clients_group = QGroupBox("Connected Clients / Cameras")
        clients_layout = QVBoxLayout(clients_group)
        
        connected_list = []
        networked_list = []
        client_devices = []
        if self.canvas_view:
            connected_list = self.canvas_view.get_cameras_connected_to_device(device.id)
            networked_list = self.canvas_view.get_cameras_on_network_of(device.id)
            # Cameras are only one kind of client. A switch in a residential design is
            # just as likely to be feeding access points, downstream switches and misc
            # appliances -- listing only cameras made a busy switch read as empty.
            client_devices = self.canvas_view.get_client_devices_of(device.id)

        if not connected_list and not client_devices:
            # An NVR usually powers nothing itself -- its cameras hang off a switch
            # that uplinks to it -- so "none directly attached" is normal and says
            # nothing about whether it can actually see them. Only call that out as a
            # problem when there's nothing on the network either.
            msg = ("Nothing connected directly." if networked_list
                   else "Nothing reaches this device.")
            lbl = QLabel(msg)
            if not networked_list:
                lbl.setStyleSheet("color: #f59e0b;")
            clients_layout.addWidget(lbl)
        else:
            total_power_used = 0.0
            for cam in connected_list:
                cam_power = cam.spec.get("maxPower", 0) or 0
                total_power_used += cam_power
                clients_layout.addWidget(QLabel(f"• {cam.label} ({cam.spec.get('model')}) - {cam_power}W"))

            for client in client_devices:
                client_spec = getattr(client, "spec", None) or {}
                model = client_spec.get("model", client_spec.get("category", "device"))
                # Only PoE loads add to the budget -- an AC-powered downstream switch is
                # still a client worth listing, it just doesn't draw from this one.
                if poe_chain.is_powered_device(client):
                    watts, estimated = poe_chain.power_draw(client)
                    total_power_used += watts
                    suffix = f" - {watts:g}W{' (est.)' if estimated else ''}"
                else:
                    suffix = " - not PoE powered"
                lbl = QLabel(f"• {client.label} ({model}){suffix}")
                lbl.setWordWrap(True)
                clients_layout.addWidget(lbl)

            poe_budget = spec.get("poeBudget", 0) or 0
            if poe_budget > 0:
                power_lbl = QLabel(f"PoE Budget: {total_power_used:.1f}W / {poe_budget}W")
                if total_power_used > poe_budget:
                    power_lbl.setStyleSheet("color: #ef4444; font-weight: bold;")
                clients_layout.addWidget(power_lbl)

        if networked_list:
            header = QLabel(f"On this network ({len(networked_list)}) \u2014 via uplinked switches:")
            header.setStyleSheet("color: #8888a0; font-size: 11px; font-weight: bold;")
            header.setWordWrap(True)
            clients_layout.addWidget(header)
            for cam in networked_list:
                via = self.canvas_view.resolve_camera_chain(cam.id)["device"]
                via_name = f" \u2190 {via.label}" if via is not None else ""
                lbl = QLabel(f"\u00b7 {cam.label} ({cam.spec.get('model')}){via_name}")
                lbl.setStyleSheet("color: #a0a0b8;")
                lbl.setWordWrap(True)
                clients_layout.addWidget(lbl)

        # Oversubscription warnings (mirrors the canvas badge) -- these switches commonly
        # have fewer PoE ports than total ports, so port count alone can't tell you
        # whether every connected camera can actually be powered.
        if self.canvas_view:
            for warning in self.canvas_view.get_device_connectivity_warnings(device):
                warn_lbl = QLabel(f"⚠ {warning}")
                warn_lbl.setStyleSheet("color: #f59e0b; font-weight: bold;")
                warn_lbl.setWordWrap(True)
                clients_layout.addWidget(warn_lbl)

        self.layout.addWidget(clients_group)

        port_map_group = self.build_port_map_group(device)
        if port_map_group:
            self.layout.addWidget(port_map_group)

        power_group = self.build_power_chain_group(device)
        if power_group:
            self.layout.addWidget(power_group)

        allowance_group = self.build_cable_allowance_group(device)
        if allowance_group:
            self.layout.addWidget(allowance_group)

        # Group 4: Notes
        notes_group = QGroupBox("Notes")
        notes_layout = QVBoxLayout(notes_group)
        notes_edit = QTextEdit(device.notes)
        notes_edit.textChanged.connect(lambda: self.update_device_notes(device, notes_edit.toPlainText()))
        notes_layout.addWidget(notes_edit)
        self.layout.addWidget(notes_group)

    # ── CUSTOM OBJECT PROPERTIES ──
    def build_custom_properties(self, custom):
        id_group = QGroupBox("Identity")
        id_layout = QFormLayout(id_group)

        label_edit = QLineEdit(custom.label)
        label_edit.textChanged.connect(lambda text: self.update_custom_label(custom, text))

        id_layout.addRow("Name:", label_edit)
        id_layout.addRow("Position X/Y:", QLabel(f"{int(custom.x())}, {int(custom.y())}"))
        self.layout.addWidget(id_group)

        allowance_group = self.build_cable_allowance_group(custom)
        if allowance_group:
            self.layout.addWidget(allowance_group)


        notes_group = QGroupBox("Notes")
        notes_layout = QVBoxLayout(notes_group)
        notes_edit = QTextEdit(custom.notes)
        notes_edit.textChanged.connect(lambda: self.update_custom_notes(custom, notes_edit.toPlainText()))
        notes_layout.addWidget(notes_edit)
        self.layout.addWidget(notes_group)

    # ── LABEL PROPERTIES ──
    def build_label_properties(self, label):
        id_group = QGroupBox("Label")
        id_layout = QFormLayout(id_group)

        text_edit = QLineEdit(label.text)
        text_edit.textChanged.connect(lambda text: self.update_label_text(label, text))
        id_layout.addRow("Text:", text_edit)

        font_spin = QDoubleSpinBox()
        font_spin.setRange(6, 96)
        font_spin.setSuffix(" pt")
        font_spin.setValue(label.font_size)
        font_spin.valueChanged.connect(lambda v: self.update_label_font_size(label, v))
        id_layout.addRow("Font Size:", font_spin)

        text_color_btn = self._make_color_button(label.text_color, lambda c: self.update_label_text_color(label, c))
        id_layout.addRow("Text Color:", text_color_btn)

        outline_color_btn = self._make_color_button(label.outline_color, lambda c: self.update_label_outline_color(label, c))
        id_layout.addRow("Outline Color:", outline_color_btn)

        id_layout.addRow("Position X/Y:", QLabel(f"{int(label.x())}, {int(label.y())}"))
        self.layout.addWidget(id_group)

        notes_group = QGroupBox("Notes")
        notes_layout = QVBoxLayout(notes_group)
        notes_edit = QTextEdit(label.notes)
        notes_edit.textChanged.connect(lambda: self.update_label_notes(label, notes_edit.toPlainText()))
        notes_layout.addWidget(notes_edit)
        self.layout.addWidget(notes_group)

    # ── ZONE PROPERTIES ──
    def build_zone_properties(self, zone):
        id_group = QGroupBox("Zone")
        id_layout = QFormLayout(id_group)

        label_edit = QLineEdit(zone.label)
        label_edit.textChanged.connect(lambda text: self.update_zone_label(zone, text))
        id_layout.addRow("Name:", label_edit)

        fill_btn = self._make_color_button(zone.fill_color, lambda c: self.update_zone_fill_color(zone, c))
        id_layout.addRow("Fill Color:", fill_btn)

        opacity_slider = QSlider(Qt.Horizontal)
        opacity_slider.setRange(5, 90)
        opacity_slider.setValue(int(zone.fill_opacity * 100))
        opacity_slider.valueChanged.connect(lambda v: self.update_zone_fill_opacity(zone, v / 100.0))
        id_layout.addRow("Fill Opacity:", opacity_slider)

        border_btn = self._make_color_button(zone.border_color, lambda c: self.update_zone_border_color(zone, c))
        id_layout.addRow("Border Color:", border_btn)

        label_pos_combo = QComboBox()
        for value, display_name in LABEL_POSITIONS:
            label_pos_combo.addItem(display_name, value)
        current_index = label_pos_combo.findData(zone.label_position)
        label_pos_combo.setCurrentIndex(current_index if current_index >= 0 else 0)
        label_pos_combo.currentIndexChanged.connect(
            lambda idx, cb=label_pos_combo: self.update_zone_label_position(zone, cb.itemData(idx)))
        id_layout.addRow("Label Position:", label_pos_combo)

        area = zone.get_area_sqft()
        area_text = f"{area:,.0f} sq ft" if area is not None else "Not calibrated"
        id_layout.addRow("Area:", QLabel(area_text))
        id_layout.addRow("Vertices:", QLabel(str(len(zone.points))))
        self.layout.addWidget(id_group)

        notes_group = QGroupBox("Notes")
        notes_layout = QVBoxLayout(notes_group)
        notes_edit = QTextEdit(zone.notes)
        notes_edit.textChanged.connect(lambda: self.update_zone_notes(zone, notes_edit.toPlainText()))
        notes_layout.addWidget(notes_edit)
        self.layout.addWidget(notes_group)

        hint = QLabel("Double-click with Select/Move to drag vertices. Right-click a vertex-editing "
                       "zone (or press X) to add a new vertex at the cursor.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #555568; font-size: 11px;")
        self.layout.addWidget(hint)

    # ── RACK PROPERTIES ──
    def build_rack_properties(self, rack):
        id_group = QGroupBox("Rack")
        id_layout = QFormLayout(id_group)

        label_edit = QLineEdit(rack.label)
        label_edit.textChanged.connect(lambda text: self.update_rack_label(rack, text))
        id_layout.addRow("Name:", label_edit)

        id_lbl = QLabel(rack.id)
        id_lbl.setStyleSheet("color: #555568; font-size: 10px;")
        id_layout.addRow("Internal ID:", id_lbl)

        used = rack.used_ru()
        id_layout.addRow("Capacity:", QLabel(f"{used} / {rack.ru_height} RU used"))
        self.layout.addWidget(id_group)

        contents_group = QGroupBox(f"Mounted Equipment ({len(rack.slots)})")
        contents_layout = QVBoxLayout(contents_group)
        if not rack.slots:
            contents_layout.addWidget(QLabel("Empty -- open the Rack Editor to mount switches/NVRs."))
        else:
            for slot in sorted(rack.slots, key=lambda s: -s.start_ru):
                dev = slot.device
                ru_text = f"RU {slot.start_ru}" if slot.ru_size == 1 else f"RU {slot.start_ru}–{slot.start_ru + slot.ru_size - 1}"
                contents_layout.addWidget(QLabel(f"• [{ru_text}] {dev.label} ({dev.spec.get('model', '')})"))
        self.layout.addWidget(contents_group)

        allowance_group = self.build_cable_allowance_group(rack)
        if allowance_group:
            self.layout.addWidget(allowance_group)


        edit_btn = QPushButton("Open Rack Editor…")
        edit_btn.clicked.connect(lambda: self._open_rack_editor(rack))
        self.layout.addWidget(edit_btn)

        notes_group = QGroupBox("Notes")
        notes_layout = QVBoxLayout(notes_group)
        notes_edit = QTextEdit(rack.notes)
        notes_edit.textChanged.connect(lambda: self.update_rack_notes(rack, notes_edit.toPlainText()))
        notes_layout.addWidget(notes_edit)
        self.layout.addWidget(notes_group)

    def _open_rack_editor(self, rack):
        if not self.canvas_view:
            return
        self.canvas_view.open_rack_editor(rack)  # opens/focuses its sheet tab; not modal

    def update_rack_label(self, rack, label):
        rack.label = label or "Rack"
        rack.update()
        self.property_changed.emit()

    def update_rack_notes(self, rack, notes):
        rack.notes = notes
        self.property_changed.emit()

    def _make_color_button(self, color_hex, on_pick):
        btn = QPushButton()
        btn.setFixedWidth(70)
        current = {"color": color_hex}

        def refresh(c):
            btn.setStyleSheet(f"background-color: {c}; border: 1px solid #2e2e3f; border-radius: 4px;")

        refresh(current["color"])

        def pick():
            chosen = QColorDialog.getColor(QColor(current["color"]), self, "Choose Color")
            if chosen.isValid():
                hex_val = chosen.name()
                current["color"] = hex_val
                refresh(hex_val)
                on_pick(hex_val)

        btn.clicked.connect(pick)
        return btn

    # ── UPDATER HANDLERS ──
    def update_camera_spec(self, camera, new_spec_id):
        if not new_spec_id or not self.canvas_view:
            return
        new_spec = self.canvas_view.catalog_data.get(new_spec_id)
        if not new_spec or new_spec is camera.spec:
            return
        camera.prepareGeometryChange()  # IR range/FOV drive boundingRect(); must precede the swap
        camera.spec = new_spec
        camera.update()
        # A different camera model can draw more/less power, which can flip whether its
        # switch is now over its PoE budget -- refresh badges, not just this camera.
        if self.canvas_view:
            self.canvas_view.refresh_connectivity_badges()
        self.property_changed.emit()
        self.select_item(camera)  # rebuild the panel so every spec-driven field refreshes

    def update_device_spec(self, device, new_spec_id):
        if not new_spec_id or not self.canvas_view:
            return
        new_spec = self.canvas_view.catalog_data.get(new_spec_id)
        if not new_spec or new_spec is device.spec:
            return
        device.prepareGeometryChange()
        device.spec = new_spec
        device.category = new_spec.get("category", device.category)
        device.update()
        # New model may have a different port count / PoE budget -- refresh this
        # device's own oversubscription badge (its connected cameras haven't changed).
        self.canvas_view.refresh_connectivity_badges()
        self.property_changed.emit()
        self.select_item(device)  # rebuild the panel so every spec-driven field refreshes

    def update_camera_label(self, camera, label):
        camera.label = label
        camera.update()
        self.property_changed.emit()

    def update_camera_elevation(self, camera, elev):
        camera.elevation = elev
        camera.update()
        self._update_range_label(camera)
        self.property_changed.emit()

    def _update_range_label(self, camera):
        from src.core.utils import compute_ground_range, get_ir_range_feet
        vfov_deg = camera.spec.get("fov", {}).get("vertical", 50.0)
        ir_feet = get_ir_range_feet(camera.spec, 0.0)
        if ir_feet <= 0:
            self.range_lbl.setText("No rated IR/detection range for this camera")
            return
        near_ft, far_ft = compute_ground_range(camera.elevation, camera.downtilt_deg, vfov_deg, ir_feet)
        if near_ft > 0.5:
            self.range_lbl.setText(f"{near_ft:.0f}–{far_ft:.0f} ft (blind spot within {near_ft:.0f} ft)")
        else:
            self.range_lbl.setText(f"0–{far_ft:.0f} ft")

    def update_camera_rotation_spin(self, camera, deg, slider):
        camera.rotation_deg = deg
        slider.blockSignals(True)
        slider.setValue(int(deg))
        slider.blockSignals(False)
        camera.update()
        self.property_changed.emit()

    def update_camera_rotation_slider(self, camera, val, spinbox):
        camera.rotation_deg = val
        spinbox.blockSignals(True)
        spinbox.setValue(val)
        spinbox.blockSignals(False)
        camera.update()
        self.property_changed.emit()

    def update_camera_downtilt_spin(self, camera, deg, slider):
        camera.downtilt_deg = deg
        slider.blockSignals(True)
        slider.setValue(int(deg))
        slider.blockSignals(False)
        camera.update()
        self._update_range_label(camera)
        self.property_changed.emit()

    def update_camera_downtilt_slider(self, camera, val, spinbox):
        camera.downtilt_deg = val
        spinbox.blockSignals(True)
        spinbox.setValue(val)
        spinbox.blockSignals(False)
        camera.update()
        self._update_range_label(camera)
        self.property_changed.emit()

    def update_camera_bool(self, camera, prop_name, state):
        setattr(camera, prop_name, state == Qt.Checked or state == 2)
        camera.update()
        self.property_changed.emit()

    def update_camera_opacity(self, camera, opacity):
        camera.fov_opacity = opacity
        camera.update()
        self.property_changed.emit()

    def update_camera_notes(self, camera, notes):
        camera.notes = notes

    def update_camera_connection(self, camera):
        device_id = self.conn_combo.currentData()
        # Update connection on canvas
        if self.canvas_view:
            self.canvas_view.connect_camera_to_device(camera.id, device_id)
        self.property_changed.emit()

    def update_cable_label(self, cable, label):
        cable.label = label
        cable.update()
        self.property_changed.emit()

    def update_cable_notes(self, cable, notes):
        cable.notes = notes

    def update_device_label(self, device, label):
        device.label = label
        device.update()
        self.property_changed.emit()

    def update_device_notes(self, device, notes):
        device.notes = notes

    def update_custom_label(self, custom, label):
        custom.label = label
        custom.update()
        self.property_changed.emit()

    def update_custom_notes(self, custom, notes):
        custom.notes = notes

    def update_label_text(self, label, text):
        label.prepareGeometryChange()
        label.text = text
        label.update()
        self.property_changed.emit()

    def update_label_font_size(self, label, size):
        label.prepareGeometryChange()
        label.font_size = size
        label.update()
        self.property_changed.emit()

    def update_label_text_color(self, label, color_hex):
        label.text_color = color_hex
        label.update()
        self.property_changed.emit()

    def update_label_outline_color(self, label, color_hex):
        label.outline_color = color_hex
        label.update()
        self.property_changed.emit()

    def update_label_notes(self, label, notes):
        label.notes = notes

    def update_zone_label(self, zone, text):
        zone.label = text
        zone.update()
        self.property_changed.emit()

    def update_zone_fill_color(self, zone, color_hex):
        zone.fill_color = color_hex
        zone.update()
        self.property_changed.emit()

    def update_zone_fill_opacity(self, zone, opacity):
        zone.fill_opacity = opacity
        zone.update()
        self.property_changed.emit()

    def update_zone_border_color(self, zone, color_hex):
        zone.border_color = color_hex
        zone.update()
        self.property_changed.emit()

    def update_zone_label_position(self, zone, position):
        zone.label_position = position
        zone.update()
        self.property_changed.emit()

    def update_zone_notes(self, zone, notes):
        zone.notes = notes
