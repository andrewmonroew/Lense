import os
import shutil
import uuid
import re
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QVBoxLayout, QFormLayout, QTreeWidget, QTreeWidgetItem,
    QPushButton, QLineEdit, QComboBox, QDoubleSpinBox, QCheckBox, QLabel,
    QGroupBox, QScrollArea, QWidget, QMessageBox, QFileDialog, QSplitter, QTextEdit
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap

from src.core import image_assets
from src.ui.catalog_tree import CATEGORY_LABELS

CAMERA_TYPES = ["bullet", "dome", "ptz", "fisheye", "dual-lens", "turret"]
ZOOM_TYPES = ["none", "digital", "optical", "hybrid"]
MOUNT_OPTIONS = ["wall", "ceiling", "pole", "desk"]
WIFI_STANDARDS = ["Wi-Fi 5 (802.11ac)", "Wi-Fi 6 (802.11ax)", "Wi-Fi 6E (802.11ax)", "Wi-Fi 7 (802.11be)"]
POE_INPUT_TYPES = ["802.3af (PoE)", "802.3at (PoE+)", "802.3bt (PoE++)", "24V Passive"]
AP_MOUNT_OPTIONS = ["ceiling", "wall", "in-wall", "outdoor"]


def slugify(manufacturer, model):
    base = f"{manufacturer}-{model}".lower()
    base = re.sub(r"[^a-z0-9]+", "-", base).strip("-")
    return base or str(uuid.uuid4())


class CatalogManagerDialog(QDialog):
    """Full add/edit/delete manager for the equipment catalog. Edits and additions are
    stored in the custom_equipment.json overlay; the shipped catalog files are never
    modified directly (see CatalogTree.save_overlay_entry / delete_entry)."""

    def __init__(self, catalog_tree, parent=None):
        super().__init__(parent)
        self.catalog_tree = catalog_tree
        self.current_spec_id = None  # None while composing a brand-new entry
        self.current_category = "camera"
        self.pending_image_path = None  # local source path chosen but not yet copied in

        self.setWindowTitle("Manage Equipment Catalog")
        self.resize(940, 660)

        root_layout = QVBoxLayout(self)
        splitter = QSplitter(Qt.Horizontal)
        root_layout.addWidget(splitter, 1)

        splitter.addWidget(self._build_left_pane())
        splitter.addWidget(self._build_right_pane())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([260, 680])

        root_layout.addLayout(self._build_bottom_bar())

        self.refresh_entry_tree()
        self.start_new_entry("camera")

    # ── Left pane: entry list ──
    def _build_left_pane(self):
        left = QWidget()
        layout = QVBoxLayout(left)
        layout.setContentsMargins(0, 0, 0, 0)

        self.entry_tree = QTreeWidget()
        self.entry_tree.setHeaderHidden(True)
        self.entry_tree.itemClicked.connect(self.on_entry_clicked)
        layout.addWidget(self.entry_tree)

        new_row = QHBoxLayout()
        btn_camera = QPushButton("+ Camera")
        btn_switch = QPushButton("+ Switch")
        btn_nvr = QPushButton("+ NVR")
        btn_ap = QPushButton("+ Access Point")
        btn_drop = QPushButton("+ Wall Drop")
        btn_misc = QPushButton("+ Misc/Appliance")
        btn_camera.clicked.connect(lambda: self.start_new_entry("camera"))
        btn_switch.clicked.connect(lambda: self.start_new_entry("switch"))
        btn_nvr.clicked.connect(lambda: self.start_new_entry("nvr"))
        btn_ap.clicked.connect(lambda: self.start_new_entry("access-point"))
        btn_drop.clicked.connect(lambda: self.start_new_entry("drop"))
        btn_misc.clicked.connect(lambda: self.start_new_entry("misc"))
        for b in (btn_camera, btn_switch, btn_nvr, btn_ap, btn_drop, btn_misc):
            new_row.addWidget(b)
        layout.addLayout(new_row)
        return left

    def refresh_entry_tree(self):
        self.entry_tree.clear()
        nodes = {}
        for spec_id, entry in sorted(self.catalog_tree.catalog_data.items(),
                                      key=lambda kv: (kv[1].get("manufacturer", ""), kv[1].get("model", ""))):
            mfg = entry.get("manufacturer", "Other")
            cat_label = CATEGORY_LABELS.get(entry.get("category", "camera"), "Other")
            key = (mfg, cat_label)
            if key not in nodes:
                if mfg not in nodes:
                    mfg_item = QTreeWidgetItem(self.entry_tree)
                    mfg_item.setText(0, mfg)
                    mfg_item.setFlags(mfg_item.flags() & ~Qt.ItemIsSelectable)
                    nodes[mfg] = mfg_item
                cat_item = QTreeWidgetItem(nodes[mfg])
                cat_item.setText(0, cat_label)
                cat_item.setFlags(cat_item.flags() & ~Qt.ItemIsSelectable)
                nodes[key] = cat_item
            child = QTreeWidgetItem(nodes[key])
            child.setText(0, entry.get("model", spec_id))
            child.setData(0, Qt.UserRole, spec_id)
            if not self.catalog_tree.is_builtin(spec_id):
                font = child.font(0)
                font.setItalic(True)
                child.setFont(0, font)
        self.entry_tree.expandAll()

    def on_entry_clicked(self, item, _col):
        spec_id = item.data(0, Qt.UserRole)
        if spec_id:
            self.load_entry(spec_id)

    # ── Right pane: form ──
    def _build_right_pane(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        container.setObjectName("container")  # picks up the QWidget#container dark
        # background rule in main_window.py's stylesheet -- a QScrollArea's own
        # content widget is a separate widget from the scroll area frame and needs
        # its own explicit styling, or it falls back to the palette's light default.
        self.form_layout = QVBoxLayout(container)
        scroll.setWidget(container)

        self.fields = {}  # key -> widget

        self.form_layout.addWidget(self._build_common_group())
        self.camera_group = self._build_camera_group()
        self.switch_group = self._build_switch_group()
        self.nvr_group = self._build_nvr_group()
        self.ap_group = self._build_ap_group()
        self.drop_group = self._build_drop_group()
        self.misc_group = self._build_misc_group()
        self.form_layout.addWidget(self.camera_group)
        self.form_layout.addWidget(self.switch_group)
        self.form_layout.addWidget(self.nvr_group)
        self.form_layout.addWidget(self.ap_group)
        self.form_layout.addWidget(self.drop_group)
        self.form_layout.addWidget(self.misc_group)
        self.form_layout.addStretch()
        return scroll

    def _add_line(self, form, key, label):
        w = QLineEdit()
        form.addRow(label, w)
        self.fields[key] = w
        return w

    def _add_spin(self, form, key, label, minimum=0, maximum=1000000, decimals=0, suffix=""):
        w = QDoubleSpinBox()
        w.setRange(minimum, maximum)
        w.setDecimals(decimals)
        if suffix:
            w.setSuffix(suffix)
        form.addRow(label, w)
        self.fields[key] = w
        return w

    def _add_check(self, form, key, label):
        w = QCheckBox()
        form.addRow(label, w)
        self.fields[key] = w
        return w

    def _add_combo(self, form, key, label, options):
        w = QComboBox()
        w.addItems(options)
        form.addRow(label, w)
        self.fields[key] = w
        return w

    def _build_common_group(self):
        group = QGroupBox("Identity")
        form = QFormLayout(group)

        self._add_line(form, "manufacturer", "Manufacturer:")
        self._add_line(form, "model", "Model:")
        self._add_spin(form, "msrp", "MSRP:", 0, 100000, 2, " $")
        self._add_line(form, "productUrl", "Product URL:")
        self._add_line(form, "dimensions", "Dimensions:")
        self._add_line(form, "weight", "Weight:")
        self._add_line(form, "operatingTemp", "Operating Temp:")

        # Image row: preview + picker
        img_row = QHBoxLayout()
        self.image_preview = QLabel("No image")
        self.image_preview.setFixedSize(96, 96)
        self.image_preview.setAlignment(Qt.AlignCenter)
        self.image_preview.setStyleSheet("border: 1px solid #2e2e3f; border-radius: 4px; color: #555568;")
        btn_choose_img = QPushButton("Choose Image...")
        btn_choose_img.clicked.connect(self.choose_image)
        img_row.addWidget(self.image_preview)
        img_row.addWidget(btn_choose_img)
        img_row.addStretch()
        form.addRow("Thumbnail:", img_row)

        return group

    def _build_camera_group(self):
        group = QGroupBox("Camera Specifications")
        form = QFormLayout(group)

        self._add_line(form, "series", "Series:")
        self._add_combo(form, "type", "Type:", CAMERA_TYPES)
        self._add_line(form, "resolution", "Resolution:")

        self._add_spin(form, "fov_h", "FOV Horizontal:", 0, 360, 1, "°")
        self._add_spin(form, "fov_v", "FOV Vertical:", 0, 360, 1, "°")
        self._add_spin(form, "fov_d", "FOV Diagonal:", 0, 360, 1, "°")

        self._add_spin(form, "ir_feet", "IR Range:", 0, 2000, 0, " ft")

        self._add_line(form, "nightVision", "Night Vision:")
        self._add_combo(form, "zoom_type", "Zoom Type:", ZOOM_TYPES)
        self._add_spin(form, "zoom_optical", "Optical Zoom:", 0, 100, 0, "x (0 = none)")

        mount_row = QHBoxLayout()
        self.mount_checks = {}
        for opt in MOUNT_OPTIONS:
            cb = QCheckBox(opt.capitalize())
            self.mount_checks[opt] = cb
            mount_row.addWidget(cb)
        form.addRow("Mounting:", mount_row)

        self._add_line(form, "ipRating", "IP Rating:")
        self._add_line(form, "ikRating", "IK Rating:")
        self._add_check(form, "poe", "PoE Powered:")
        self._add_spin(form, "maxPower", "Max Power:", 0, 200, 1, " W")

        self._add_check(form, "audio_mic", "Microphone:")
        self._add_check(form, "audio_speaker", "Speaker (2-way audio):")
        self._add_line(form, "smartDetection", "Smart Detection (comma-separated):")
        self._add_check(form, "microSdSlot", "MicroSD Slot:")
        self._add_spin(form, "maxCapacityTb", "Max Local Storage:", 0, 128, 1, " TB")

        return group

    def _build_switch_group(self):
        group = QGroupBox("Switch Specifications")
        form = QFormLayout(group)
        self._add_spin(form, "ports", "Total Ports:", 0, 128, 0)
        self._add_spin(form, "poePorts", "PoE Ports:", 0, 128, 0)
        self._add_spin(form, "poeBudget", "PoE Budget:", 0, 4000, 0, " W")
        self._add_spin(form, "sfpPorts", "SFP Ports:", 0, 32, 0)
        self._add_check(form, "managed", "Managed:")
        self._add_line(form, "mountType", "Mount Type:")
        self._add_spin(form, "switchingCapacityGbps", "Switching Capacity:", 0, 1000, 1, " Gbps")

        # Per-port PoE map. Free text rather than a grid of widgets because the number
        # of banks varies per model, and one line per bank stays readable and quick to
        # edit -- this is what drives both the coloured port grid in the Rack Editor and
        # the Port Map section of the Properties panel.
        self.poe_groups_edit = QTextEdit()
        self.poe_groups_edit.setPlaceholderText("1-16: 802.3at 32W\n17-24: 802.3bt 64W")
        self.poe_groups_edit.setFixedHeight(70)
        self.poe_groups_edit.setToolTip(
            "One bank per line: <first>-<last>: <standard> <watts>W\n"
            "e.g. 1-16: 802.3at 32W   (802.3af = PoE, 802.3at = PoE+, 802.3bt = PoE++)\n"
            "Ports left out of every line are treated as data-only.")
        form.addRow("PoE Port Map:", self.poe_groups_edit)
        self._add_spin(form, "poeInputPort", "PoE Input Port (0=none):", 0, 128, 0)
        self._add_line(form, "sfpNote", "SFP Note:")
        return group

    # ── PoE port-map text <-> poePortGroups ──
    @staticmethod
    def _format_poe_groups(groups):
        lines = []
        first = 1
        for g in groups or []:
            count = int(g.get("count") or 0)
            if count <= 0:
                continue
            start = int(g.get("firstPort") or first)
            end = start + count - 1
            lines.append(f"{start}-{end}: {g.get('standard', '')} {g.get('maxWatts', 0):g}W")
            first = end + 1
        return "\n".join(lines)

    @staticmethod
    def _parse_poe_groups(text):
        """'1-16: 802.3at 32W' lines -> poePortGroups. Bad lines are skipped rather
        than raising -- a typo shouldn't cost the user the rest of the entry."""
        groups = []
        for raw in (text or "").splitlines():
            line = raw.strip()
            if not line or ":" not in line:
                continue
            span, _, rest = line.partition(":")
            span, rest = span.strip(), rest.strip()
            try:
                if "-" in span:
                    start_s, end_s = span.split("-", 1)
                    start, end = int(start_s), int(end_s)
                else:
                    start = end = int(span)
                if end < start:
                    continue
                parts = rest.replace("W", " ").split()
                standard = parts[0] if parts else ""
                watts = float(parts[1]) if len(parts) > 1 else 0.0
            except (ValueError, IndexError):
                continue
            if not standard:
                continue
            groups.append({"firstPort": start, "count": end - start + 1,
                           "standard": standard, "maxWatts": watts})
        return groups

    def _build_nvr_group(self):
        group = QGroupBox("NVR Specifications")
        form = QFormLayout(group)
        self._add_spin(form, "maxChannels", "Max Channels:", 0, 256, 0)
        self._add_spin(form, "driveBays", "Drive Bays:", 0, 64, 0)
        self._add_check(form, "builtInPoe", "Built-in PoE:")
        # Distinct field keys from the Switch group's "poePorts"/"poeBudget" (same
        # underlying JSON field on save/load) -- self.fields is a flat dict keyed by
        # name, so sharing those keys here silently stole the widget reference the
        # Switch group's save/load logic depends on (NVR group is built after Switch
        # in _build_right_pane, so it always won the collision).
        self._add_spin(form, "nvr_poePorts", "PoE Ports:", 0, 128, 0)
        self._add_spin(form, "nvr_poeBudget", "PoE Budget:", 0, 4000, 0, " W")
        self._add_spin(form, "ports_rj45", "RJ45 Uplink Ports:", 0, 16, 0)
        self._add_spin(form, "ports_sfp", "SFP Uplink Ports:", 0, 16, 0)
        self._add_spin(form, "maxDriveCapacityTb", "Max Drive Capacity (per bay):", 0, 128, 0, " TB")
        self._add_check(form, "raidSupport", "RAID Support:")
        return group

    def _build_ap_group(self):
        group = QGroupBox("Access Point Specifications")
        form = QFormLayout(group)
        self._add_combo(form, "wifiStandard", "Wi-Fi Standard:", WIFI_STANDARDS)
        self._add_spin(form, "maxThroughputMbps", "Max Throughput:", 0, 20000, 0, " Mbps")

        radio_row = QHBoxLayout()
        self.radio_checks = {}
        for key, label in (("24ghz", "2.4GHz"), ("5ghz", "5GHz"), ("6ghz", "6GHz")):
            cb = QCheckBox(label)
            self.radio_checks[key] = cb
            radio_row.addWidget(cb)
        form.addRow("Radios:", radio_row)

        self._add_combo(form, "poeInput", "PoE Input:", POE_INPUT_TYPES)

        ap_mount_row = QHBoxLayout()
        self.ap_mount_checks = {}
        for opt in AP_MOUNT_OPTIONS:
            cb = QCheckBox(opt.capitalize())
            self.ap_mount_checks[opt] = cb
            ap_mount_row.addWidget(cb)
        form.addRow("Mounting:", ap_mount_row)

        self._add_spin(form, "ratedClients", "Rated Client Count:", 0, 2000, 0)
        return group

    def _build_drop_group(self):
        group = QGroupBox("Wall Drop Specifications")
        form = QFormLayout(group)
        # Distinct field key from the Switch group's "ports" (same underlying JSON
        # field on save/load, see save_current/load_entry) -- self.fields is a flat
        # dict keyed by name, so sharing "ports" here would silently steal the widget
        # reference the Switch group's save/load logic depends on.
        self._add_spin(form, "drop_ports", "Number of Ports (gangs):", 1, 8, 0)
        self._add_line(form, "keystoneType", "Keystone/Jack Type:")
        self._add_line(form, "drop_mountType", "Mount Type:")  # see drop_ports note above
        return group

    def _build_misc_group(self):
        group = QGroupBox("Misc / Appliance Specifications")
        form = QFormLayout(group)
        self._add_line(form, "applianceType", "Appliance Type:")
        # Distinct field key from the Switch group's "ports", same reasoning as
        # drop_ports above.
        self._add_spin(form, "misc_ports", "Number of Ethernet Ports:", 0, 8, 0)
        return group

    def _build_bottom_bar(self):
        row = QHBoxLayout()
        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet("color: #8888a0;")
        self.btn_delete = QPushButton("Delete Entry")
        self.btn_delete.clicked.connect(self.delete_current)
        self.btn_save = QPushButton("Save")
        self.btn_save.clicked.connect(self.save_current)
        self.btn_close = QPushButton("Close")
        self.btn_close.clicked.connect(self.accept)
        row.addWidget(self.lbl_status, 1)
        row.addWidget(self.btn_delete)
        row.addWidget(self.btn_save)
        row.addWidget(self.btn_close)
        return row

    # ── Category visibility ──
    def show_category(self, category):
        self.current_category = category
        self.camera_group.setVisible(category == "camera")
        self.switch_group.setVisible(category == "switch")
        self.nvr_group.setVisible(category == "nvr")
        self.ap_group.setVisible(category == "access-point")
        self.drop_group.setVisible(category == "drop")
        self.misc_group.setVisible(category == "misc")

    # ── Loading an entry into the form ──
    def start_new_entry(self, category):
        self.current_spec_id = None
        self.pending_image_path = None
        self.show_category(category)
        self.clear_form()
        if category in ("drop", "misc"):
            # Not a real purchasable SKU -- pre-fill instead of loosening the shared
            # "Manufacturer and Model are required" validation for these categories.
            self.fields["manufacturer"].setText("Generic")
        self.lbl_status.setText(f"New {CATEGORY_LABELS.get(category, category)[:-1]} — fill in the fields and Save")
        self.btn_delete.setEnabled(False)
        self.entry_tree.clearSelection()

    def clear_form(self):
        for key, w in self.fields.items():
            if isinstance(w, QLineEdit):
                w.setText("")
            elif isinstance(w, QDoubleSpinBox):
                w.setValue(0)
            elif isinstance(w, QCheckBox):
                w.setChecked(False)
            elif isinstance(w, QComboBox):
                w.setCurrentIndex(0)
        self.poe_groups_edit.clear()  # not in self.fields (it's a QTextEdit, not a row widget)
        for cb in self.mount_checks.values():
            cb.setChecked(False)
        for cb in self.radio_checks.values():
            cb.setChecked(False)
        for cb in self.ap_mount_checks.values():
            cb.setChecked(False)
        self.image_preview.setPixmap(QPixmap())
        self.image_preview.setText("No image")

    def load_entry(self, spec_id):
        entry = self.catalog_tree.catalog_data.get(spec_id)
        if not entry:
            return
        self.current_spec_id = spec_id
        self.pending_image_path = None
        category = entry.get("category", "camera")
        self.show_category(category)
        self.clear_form()

        self.fields["manufacturer"].setText(entry.get("manufacturer", ""))
        self.fields["model"].setText(entry.get("model", ""))
        self.fields["msrp"].setValue(entry.get("msrp", 0) or 0)
        self.fields["productUrl"].setText(entry.get("productUrl", "") or "")
        self.fields["dimensions"].setText(entry.get("dimensions", "") or "")
        self.fields["weight"].setText(entry.get("weight", "") or "")
        self.fields["operatingTemp"].setText(entry.get("operatingTemp", "") or "")

        if category == "camera":
            self.fields["series"].setText(entry.get("series", "") or "")
            self._set_combo("type", entry.get("type", "bullet"))
            self.fields["resolution"].setText(entry.get("resolution", "") or "")
            fov = entry.get("fov", {}) or {}
            self.fields["fov_h"].setValue(fov.get("horizontal", 0) or 0)
            self.fields["fov_v"].setValue(fov.get("vertical", 0) or 0)
            self.fields["fov_d"].setValue(fov.get("diagonal", 0) or 0)
            self.fields["ir_feet"].setValue((entry.get("irRange", {}) or {}).get("feet", 0) or 0)
            self.fields["nightVision"].setText(entry.get("nightVision", "") or "")
            zoom = entry.get("zoom", {}) or {}
            self._set_combo("zoom_type", zoom.get("type", "none"))
            self.fields["zoom_optical"].setValue(zoom.get("optical", 0) or 0)
            for opt, cb in self.mount_checks.items():
                cb.setChecked(opt in (entry.get("mounting") or []))
            self.fields["ipRating"].setText(entry.get("ipRating", "") or "")
            self.fields["ikRating"].setText(entry.get("ikRating", "") or "")
            self.fields["poe"].setChecked(bool(entry.get("poe")))
            self.fields["maxPower"].setValue(entry.get("maxPower", 0) or 0)
            audio = entry.get("audio", {}) or {}
            self.fields["audio_mic"].setChecked(bool(audio.get("microphone")))
            self.fields["audio_speaker"].setChecked(bool(audio.get("speaker")))
            self.fields["smartDetection"].setText(", ".join(entry.get("smartDetection") or []))
            storage = entry.get("localStorage", {}) or {}
            self.fields["microSdSlot"].setChecked(bool(storage.get("microSdSlot")))
            self.fields["maxCapacityTb"].setValue(storage.get("maxCapacityTb", 0) or 0)
        elif category == "switch":
            self.fields["ports"].setValue(entry.get("ports", 0) or 0)
            self.fields["poePorts"].setValue(entry.get("poePorts", 0) or 0)
            self.fields["poeBudget"].setValue(entry.get("poeBudget", 0) or 0)
            self.fields["sfpPorts"].setValue(entry.get("sfpPorts", 0) or 0)
            self.fields["managed"].setChecked(bool(entry.get("managed")))
            self.fields["mountType"].setText(entry.get("mountType", "") or "")
            self.fields["switchingCapacityGbps"].setValue(entry.get("switchingCapacityGbps", 0) or 0)
            self.fields["poeInputPort"].setValue(entry.get("poeInputPort", 0) or 0)
            self.fields["sfpNote"].setText(entry.get("sfpNote", "") or "")
        elif category == "nvr":
            self.fields["maxChannels"].setValue(entry.get("maxChannels", 0) or 0)
            self.fields["driveBays"].setValue(entry.get("driveBays", 0) or 0)
            self.fields["builtInPoe"].setChecked(bool(entry.get("builtInPoe")))
            self.fields["nvr_poePorts"].setValue(entry.get("poePorts", 0) or 0)
            self.fields["nvr_poeBudget"].setValue(entry.get("poeBudget", 0) or 0)
            ports = entry.get("ports", {}) or {}
            self.fields["ports_rj45"].setValue(ports.get("rj45", 0) or 0)
            self.fields["ports_sfp"].setValue(ports.get("sfp", 0) or 0)
            self.fields["maxDriveCapacityTb"].setValue(entry.get("maxDriveCapacityTb", 0) or 0)
            self.fields["raidSupport"].setChecked(bool(entry.get("raidSupport")))
        elif category == "access-point":
            self._set_combo("wifiStandard", entry.get("wifiStandard", WIFI_STANDARDS[1]))
            self.fields["maxThroughputMbps"].setValue(entry.get("maxThroughputMbps", 0) or 0)
            radios = entry.get("radios", {}) or {}
            for key, cb in self.radio_checks.items():
                cb.setChecked(bool(radios.get(key)))
            self._set_combo("poeInput", entry.get("poeInput", POE_INPUT_TYPES[0]))
            mounting = entry.get("mounting") or []
            for opt, cb in self.ap_mount_checks.items():
                cb.setChecked(opt in mounting)
            self.fields["ratedClients"].setValue(entry.get("ratedClients", 0) or 0)
        if category == "switch":
            self.poe_groups_edit.setPlainText(self._format_poe_groups(entry.get("poePortGroups")))

        if category == "drop":
            self.fields["drop_ports"].setValue(entry.get("ports", 1) or 1)
            self.fields["keystoneType"].setText(entry.get("keystoneType", "") or "")
            self.fields["drop_mountType"].setText(entry.get("mountType", "") or "")
        elif category == "misc":
            self.fields["applianceType"].setText(entry.get("applianceType", "") or "")
            self.fields["misc_ports"].setValue(entry.get("ports", 1) or 1)

        self._load_image_preview(entry.get("imagePath"))
        tag = "Built-in (editing creates an override)" if self.catalog_tree.is_builtin(spec_id) else "Custom entry"
        self.lbl_status.setText(f"Editing {entry.get('model', spec_id)} — {tag}")
        self.btn_delete.setEnabled(True)

    def _set_combo(self, key, value):
        combo = self.fields[key]
        idx = combo.findText(value or "", Qt.MatchFixedString)
        combo.setCurrentIndex(idx if idx >= 0 else 0)

    def _load_image_preview(self, image_path):
        if image_path:
            full_path = os.path.join(self.catalog_tree.get_data_dir(), image_path)
            if os.path.exists(full_path):
                pix = QPixmap(full_path)
                if not pix.isNull():
                    self.image_preview.setPixmap(pix.scaled(96, 96, Qt.KeepAspectRatio, Qt.SmoothTransformation))
                    self.image_preview.setText("")
                    return
        self.image_preview.setPixmap(QPixmap())
        self.image_preview.setText("No image")

    def choose_image(self):
        filepath, _ = QFileDialog.getOpenFileName(
            self, "Choose Thumbnail Image", "",
            "Images (" + " ".join(f"*{s}" for s in image_assets.ACCEPTED_SUFFIXES) + ")"
        )
        if filepath:
            self.pending_image_path = filepath
            pix = QPixmap(filepath)
            if not pix.isNull():
                self.image_preview.setPixmap(pix.scaled(96, 96, Qt.KeepAspectRatio, Qt.SmoothTransformation))
                self.image_preview.setText("")

    # ── Save / Delete ──
    def save_current(self):
        manufacturer = self.fields["manufacturer"].text().strip()
        model = self.fields["model"].text().strip()
        if not manufacturer or not model:
            QMessageBox.warning(self, "Missing Fields", "Manufacturer and Model are required.")
            return

        spec_id = self.current_spec_id or slugify(manufacturer, model)
        entry = {
            "id": spec_id,
            "manufacturer": manufacturer,
            "model": model,
            "category": self.current_category,
            "msrp": self.fields["msrp"].value(),
            "productUrl": self.fields["productUrl"].text().strip(),
            "dimensions": self.fields["dimensions"].text().strip(),
            "weight": self.fields["weight"].text().strip(),
            "operatingTemp": self.fields["operatingTemp"].text().strip(),
        }

        if self.current_category == "camera":
            entry.update({
                "series": self.fields["series"].text().strip(),
                "type": self.fields["type"].currentText(),
                "resolution": self.fields["resolution"].text().strip(),
                "fov": {
                    "horizontal": self.fields["fov_h"].value(),
                    "vertical": self.fields["fov_v"].value(),
                    "diagonal": self.fields["fov_d"].value(),
                },
                "irRange": {
                    "feet": self.fields["ir_feet"].value(),
                    "meters": round(self.fields["ir_feet"].value() * 0.3048, 1),
                },
                "nightVision": self.fields["nightVision"].text().strip(),
                "zoom": {
                    "type": self.fields["zoom_type"].currentText(),
                    "optical": self.fields["zoom_optical"].value() or None,
                },
                "mounting": [opt for opt, cb in self.mount_checks.items() if cb.isChecked()],
                "ipRating": self.fields["ipRating"].text().strip(),
                "ikRating": self.fields["ikRating"].text().strip(),
                "poe": self.fields["poe"].isChecked(),
                "maxPower": self.fields["maxPower"].value(),
                "audio": {
                    "microphone": self.fields["audio_mic"].isChecked(),
                    "speaker": self.fields["audio_speaker"].isChecked(),
                },
                "smartDetection": [s.strip() for s in self.fields["smartDetection"].text().split(",") if s.strip()],
                "localStorage": {
                    "microSdSlot": self.fields["microSdSlot"].isChecked(),
                    "maxCapacityTb": self.fields["maxCapacityTb"].value() or None,
                },
            })
        elif self.current_category == "switch":
            entry.update({
                "ports": int(self.fields["ports"].value()),
                "poePorts": int(self.fields["poePorts"].value()),
                "poeBudget": self.fields["poeBudget"].value(),
                "sfpPorts": int(self.fields["sfpPorts"].value()),
                "managed": self.fields["managed"].isChecked(),
                "mountType": self.fields["mountType"].text().strip(),
                "switchingCapacityGbps": self.fields["switchingCapacityGbps"].value() or None,
            })
        elif self.current_category == "nvr":
            entry.update({
                "maxChannels": int(self.fields["maxChannels"].value()),
                "driveBays": int(self.fields["driveBays"].value()),
                "builtInPoe": self.fields["builtInPoe"].isChecked(),
                "poePorts": int(self.fields["nvr_poePorts"].value()),
                "poeBudget": self.fields["nvr_poeBudget"].value(),
                "ports": {
                    "rj45": int(self.fields["ports_rj45"].value()),
                    "sfp": int(self.fields["ports_sfp"].value()),
                },
                # Mirrored at the top level under the same key switches use --
                # DeviceItem.sfp_port_count reads spec.get("sfpPorts") generically for
                # any category, so an NVR's SFP uplink count needs to live there too,
                # not just nested under "ports".
                "sfpPorts": int(self.fields["ports_sfp"].value()),
                "maxDriveCapacityTb": self.fields["maxDriveCapacityTb"].value() or None,
                "raidSupport": self.fields["raidSupport"].isChecked(),
            })
        elif self.current_category == "access-point":
            entry.update({
                "ports": 1,  # a single PoE uplink jack, same convention as an NVR's uplink port
                "wifiStandard": self.fields["wifiStandard"].currentText(),
                "maxThroughputMbps": self.fields["maxThroughputMbps"].value() or None,
                "radios": {key: cb.isChecked() for key, cb in self.radio_checks.items()},
                "poeInput": self.fields["poeInput"].currentText(),
                "mounting": [opt for opt, cb in self.ap_mount_checks.items() if cb.isChecked()],
                "ratedClients": int(self.fields["ratedClients"].value()) or None,
            })
        if self.current_category == "switch":
            parsed = self._parse_poe_groups(self.poe_groups_edit.toPlainText())
            if parsed:
                entry["poePortGroups"] = parsed
            else:
                entry.pop("poePortGroups", None)
            input_port = int(self.fields["poeInputPort"].value())
            if input_port:
                entry["poeInputPort"] = input_port
            else:
                entry.pop("poeInputPort", None)
            sfp_note = self.fields["sfpNote"].text().strip()
            if sfp_note:
                entry["sfpNote"] = sfp_note
            else:
                entry.pop("sfpNote", None)

        if self.current_category == "drop":
            entry.update({
                "ports": int(self.fields["drop_ports"].value()),
                "keystoneType": self.fields["keystoneType"].text().strip(),
                "mountType": self.fields["drop_mountType"].text().strip(),
            })
        elif self.current_category == "misc":
            entry.update({
                "applianceType": self.fields["applianceType"].text().strip(),
                "ports": int(self.fields["misc_ports"].value()),
            })

        # Copy in a newly-chosen image, or keep whatever was already set
        if self.pending_image_path:
            entry["imagePath"] = self._copy_image_in(spec_id, self.pending_image_path)
        elif self.current_spec_id:
            existing = self.catalog_tree.catalog_data.get(self.current_spec_id, {})
            if existing.get("imagePath"):
                entry["imagePath"] = existing["imagePath"]

        self.catalog_tree.save_overlay_entry(entry)
        self.current_spec_id = spec_id
        self.pending_image_path = None
        self.refresh_entry_tree()
        self.lbl_status.setText(f"Saved {model}")
        self.btn_delete.setEnabled(True)

    def _copy_image_in(self, spec_id, source_path):
        """Bring a chosen image into the catalog, normalised to thumbnail policy.

        Not a straight copy: a 3 MB product shot off a vendor site would otherwise
        sit in the catalog at full resolution and be baked into every build, for a
        picture never drawn wider than 220 px. See core/image_assets.py.
        """
        stored = image_assets.write_thumbnail(
            source_path, self.catalog_tree.get_images_dir(), spec_id)
        if stored is not None:
            return stored
        # Unreadable by Qt (a PDF renamed .png, a corrupt file) -- fall back to
        # copying it verbatim so the user's choice isn't silently discarded.
        extension = os.path.splitext(source_path)[1] or ".png"
        images_dir = self.catalog_tree.get_images_dir()
        os.makedirs(images_dir, exist_ok=True)
        shutil.copyfile(source_path, os.path.join(images_dir, f"{spec_id}{extension}"))
        return f"images/{spec_id}{extension}"

    def delete_current(self):
        if not self.current_spec_id:
            return
        entry = self.catalog_tree.catalog_data.get(self.current_spec_id, {})
        res = QMessageBox.question(
            self, "Delete Entry",
            f"Remove '{entry.get('model', self.current_spec_id)}' from the catalog?",
            QMessageBox.Yes | QMessageBox.No
        )
        if res != QMessageBox.Yes:
            return
        self.catalog_tree.delete_entry(self.current_spec_id)
        self.refresh_entry_tree()
        self.start_new_entry(self.current_category)
