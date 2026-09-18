from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QTreeWidget, QTreeWidgetItem, QPushButton, QLabel
from PySide6.QtCore import Qt, QTimer


class ObjectExplorerPanel(QWidget):
    """Flat itemized listing of every placed object, grouped by type, so a dense real
    design (dozens of cameras/devices/labels) can be scanned and jumped to without
    hunting for it on the canvas. Cabling is deliberately left out for now -- cable
    itemization already lives in the Inventory panel and how it should be represented
    here (by run? by bundle?) isn't decided yet."""

    def __init__(self, canvas_view, parent=None):
        super().__init__(parent)
        self.canvas_view = canvas_view

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        hint = QLabel("Click an object to select and jump to it on the canvas.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #8888a0; font-size: 11px;")
        layout.addWidget(hint)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Object", "Details"])
        self.tree.setColumnCount(2)
        self.tree.itemClicked.connect(self._on_item_clicked)
        layout.addWidget(self.tree, 1)

        btn_row = QHBoxLayout()
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh)
        btn_row.addStretch()
        btn_row.addWidget(refresh_btn)
        layout.addLayout(btn_row)

        # Placement/deletion elsewhere on canvas has no single central signal, so a
        # light periodic refresh while visible (same pattern as InventoryPanel) keeps
        # this in sync without wiring every mutation call site to notify it directly.
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(1500)
        self._refresh_timer.timeout.connect(self._tick)
        self._refresh_timer.start()

        self.refresh()

    def _tick(self):
        if self.isVisible():
            self.refresh()

    def _on_item_clicked(self, tree_item, _column):
        target = tree_item.data(0, Qt.UserRole)
        if target is None or self.canvas_view is None:
            return
        self.canvas_view.select_and_focus_item(target)

    def _add_category(self, label_text):
        cat_item = QTreeWidgetItem([label_text, ""])
        font = cat_item.font(0)
        font.setBold(True)
        cat_item.setFont(0, font)
        self.tree.addTopLevelItem(cat_item)
        return cat_item

    def refresh(self):
        self.tree.clear()

        if self.canvas_view is None:
            return

        cameras = self.canvas_view.get_cameras()
        devices = self.canvas_view.get_network_devices()
        switches = [d for d in devices if d.spec.get("category") == "switch"]
        patch_panels = [d for d in devices if d.spec.get("category") == "patch-panel"]
        nvrs = [d for d in devices if d.spec.get("category") == "nvr"]
        access_points = [d for d in devices if d.spec.get("category") == "access-point"]
        drops = [d for d in devices if d.spec.get("category") == "drop"]
        misc_devices = [d for d in devices if d.spec.get("category") == "misc"]
        racks = self.canvas_view.get_racks()
        customs = self.canvas_view.get_custom_objects()
        deco_racks = [c for c in customs if getattr(c, "icon_type", "generic") == "rack"]
        generic_customs = [c for c in customs if getattr(c, "icon_type", "generic") != "rack"]
        labels = self.canvas_view.get_labels()
        zones = self.canvas_view.get_zones()

        def add_group(title, items, name_fn, detail_fn):
            if not items:
                return
            cat_item = self._add_category(f"{title} ({len(items)})")
            for obj in items:
                row = QTreeWidgetItem([name_fn(obj), detail_fn(obj)])
                row.setData(0, Qt.UserRole, obj)
                cat_item.addChild(row)
            cat_item.setExpanded(True)

        spec_name = lambda o: f"{o.spec.get('manufacturer', '')} {o.spec.get('model', '')}".strip()
        by_label = lambda o: o.label

        add_group("Cameras", cameras, by_label, spec_name)
        add_group("Switches", switches, by_label, spec_name)
        add_group("Access Points", access_points, by_label, spec_name)
        add_group("NVRs", nvrs, by_label, spec_name)
        add_group("Patch Panels", patch_panels, by_label, spec_name)
        add_group("Wall Drops", drops, by_label, spec_name)
        add_group("Misc Devices", misc_devices, by_label, spec_name)
        add_group("Racks", racks, by_label, lambda r: f"{r.used_ru()}/{r.ru_height} RU used")
        add_group("Data Racks", deco_racks, by_label, lambda c: "")
        add_group("Custom Objects", generic_customs, by_label, lambda c: "")
        add_group("Labels", labels, lambda l: l.text or "(empty)", lambda l: "")
        add_group("Zones", zones, by_label,
                   lambda z: f"{z.get_area_sqft():.0f} sq ft" if hasattr(z, "get_area_sqft") and z.get_area_sqft() else "")

        for i in range(2):
            self.tree.resizeColumnToContents(i)

        if self.tree.topLevelItemCount() == 0:
            placeholder = QTreeWidgetItem(["No objects placed yet.", ""])
            placeholder.setDisabled(True)
            self.tree.addTopLevelItem(placeholder)
