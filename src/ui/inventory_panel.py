from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                             QTreeWidget, QTreeWidgetItem, QDoubleSpinBox, QFormLayout,
                             QGridLayout, QGroupBox)
from PySide6.QtCore import QSettings, QTimer

from src.core import termination

# Unit prices live in QSettings rather than in the project file: they are what YOUR
# supplier charges, so they should follow you from job to job instead of being frozen
# into whichever project happened to be open when you typed them. Kept here rather than
# with the settings-dialog keys because this panel is the only thing that reads or
# writes them.
KEY_COPPER_RATE = "pricing/copper_per_foot"
KEY_FIBER_RATE = "pricing/fiber_per_foot"
KEY_PLUG_PRICE = "pricing/rj45_plug"
KEY_JACK_PRICE = "pricing/keystone_jack"
KEY_PATCH_PRICE = "pricing/patch_cable"

DEFAULT_PRICES = {
    KEY_COPPER_RATE: 0.15,
    KEY_FIBER_RATE: 1.50,
    KEY_PLUG_PRICE: 0.35,
    KEY_JACK_PRICE: 3.50,
    KEY_PATCH_PRICE: 3.00,
}


def price(key):
    stored = QSettings().value(key)
    if stored is None:
        return DEFAULT_PRICES[key]
    try:
        return max(0.0, float(stored))
    except (TypeError, ValueError):
        return DEFAULT_PRICES[key]


class InventoryPanel(QWidget):
    """Live equipment inventory + cost breakdown, and total cable footage mapped so far.
    Refreshes on a light timer while visible rather than needing to be wired into every
    place/delete/drag call site individually."""

    def __init__(self, canvas_view, parent=None):
        super().__init__(parent)
        self.canvas_view = canvas_view
        self.cost_per_foot = price(KEY_COPPER_RATE)
        self.fiber_cost_per_foot = price(KEY_FIBER_RATE)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(12)

        # ── Summary ──
        summary_group = QGroupBox("Summary")
        summary_layout = QFormLayout(summary_group)

        self.lbl_total_cable_ft = QLabel("0 ft")
        self.lbl_total_fiber_ft = QLabel("0 ft")
        self.lbl_equipment_cost = QLabel("$0")
        self.lbl_cable_cost = QLabel("$0")
        self.lbl_fiber_cost = QLabel("$0")
        self.lbl_termination_cost = QLabel("$0")
        self.lbl_total_cost = QLabel("$0")
        self.lbl_total_cost.setStyleSheet("font-weight: bold; font-size: 14px; color: #22c55e;")

        summary_layout.addRow("Copper Cable Mapped:", self.lbl_total_cable_ft)
        summary_layout.addRow("Fiber Link Mapped:", self.lbl_total_fiber_ft)
        summary_layout.addRow("Equipment Cost:", self.lbl_equipment_cost)
        summary_layout.addRow("Est. Copper Cost:", self.lbl_cable_cost)
        summary_layout.addRow("Est. Fiber Cost:", self.lbl_fiber_cost)
        summary_layout.addRow("Est. Termination Cost:", self.lbl_termination_cost)
        summary_layout.addRow("Grand Total:", self.lbl_total_cost)

        layout.addWidget(summary_group)

        # ── Unit prices ──
        price_group = QGroupBox("Unit Prices")
        price_grid = QGridLayout(price_group)
        self.price_spins = {}
        rows = ((KEY_COPPER_RATE, "Copper:", " / ft"),
                (KEY_FIBER_RATE, "Fiber:", " / ft"),
                (KEY_PLUG_PRICE, "RJ45 plug:", " ea"),
                (KEY_JACK_PRICE, "Keystone jack:", " ea"),
                (KEY_PATCH_PRICE, "Patch cable:", " ea"))
        for index, (key, caption, suffix) in enumerate(rows):
            spin = QDoubleSpinBox()
            spin.setRange(0, 500)
            spin.setDecimals(2)
            spin.setPrefix("$")
            spin.setSuffix(suffix)
            spin.setValue(price(key))
            spin.valueChanged.connect(lambda value, k=key: self._on_price_changed(k, value))
            column = (index % 2) * 2
            price_grid.addWidget(QLabel(caption), index // 2, column)
            price_grid.addWidget(spin, index // 2, column + 1)
            self.price_spins[key] = spin
        price_grid.setColumnStretch(4, 1)
        layout.addWidget(price_group)

        # Kept as attributes under their old names so anything already reaching for the
        # copper/fiber spin boxes still finds them.
        self.cost_per_foot_spin = self.price_spins[KEY_COPPER_RATE]
        self.fiber_cost_per_foot_spin = self.price_spins[KEY_FIBER_RATE]

        # ── Equipment inventory ──
        inv_group = QGroupBox("Equipment Inventory")
        inv_layout = QVBoxLayout(inv_group)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Item", "Qty", "Unit Price", "Subtotal"])
        self.tree.setColumnCount(4)
        inv_layout.addWidget(self.tree)
        layout.addWidget(inv_group, 1)

        btn_row = QHBoxLayout()
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh)
        btn_row.addStretch()
        btn_row.addWidget(refresh_btn)
        layout.addLayout(btn_row)

        # Light periodic refresh while visible -- catches cases that don't have a direct
        # signal wired (e.g. a cable's length changing mid vertex-drag) without needing
        # every mutation call site in canvas_view to remember to notify this panel.
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(1500)
        self._refresh_timer.timeout.connect(self._tick)
        self._refresh_timer.start()

        self.refresh()

    def _tick(self):
        if self.isVisible():
            self.refresh()

    def _on_price_changed(self, key, value):
        QSettings().setValue(key, value)
        if key == KEY_COPPER_RATE:
            self.cost_per_foot = value
        elif key == KEY_FIBER_RATE:
            self.fiber_cost_per_foot = value
        self.refresh()

    def refresh(self):
        self.tree.clear()

        # Patch cables are short internal jumpers within a rack, not field runs -- kept
        # out of the copper/fiber footage and priced by the piece under Terminations,
        # alongside the jacks they jumper out of.
        all_cables = self.canvas_view.get_cables()
        copper_cables = [c for c in all_cables if c.cable_type not in ("Fiber", "Patch")]
        fiber_cables = [c for c in all_cables if c.cable_type == "Fiber"]

        devices = self.canvas_view.get_network_devices()
        categories = [("Cameras", self.canvas_view.get_cameras()),
                      ("Switches", [d for d in devices if d.spec.get("category") == "switch"]),
                      ("Access Points", [d for d in devices if d.spec.get("category") == "access-point"]),
                      ("NVRs", [d for d in devices if d.spec.get("category") == "nvr"]),
                      ("Patch Panels", [d for d in devices if d.spec.get("category") == "patch-panel"]),
                      ("Wall Drops", [d for d in devices if d.spec.get("category") == "drop"]),
                      ("Misc Devices", [d for d in devices if d.spec.get("category") == "misc"])]

        equipment_total = 0.0
        for cat_label, items in categories:
            if not items:
                continue

            groups = {}
            for item in items:
                spec = item.spec
                key = spec.get("id", item.label)
                g = groups.setdefault(key, {
                    "label": f"{spec.get('manufacturer', '')} {spec.get('model', item.label)}".strip(),
                    "qty": 0,
                    "unit_price": spec.get("msrp", 0) or 0,
                })
                g["qty"] += 1

            cat_item = QTreeWidgetItem([cat_label, "", "", ""])
            font = cat_item.font(0)
            font.setBold(True)
            cat_item.setFont(0, font)
            self.tree.addTopLevelItem(cat_item)

            for _key, g in sorted(groups.items(), key=lambda kv: kv[1]["label"]):
                subtotal = g["qty"] * g["unit_price"]
                equipment_total += subtotal
                child = QTreeWidgetItem([g["label"], str(g["qty"]), f"${g['unit_price']:,.0f}", f"${subtotal:,.0f}"])
                cat_item.addChild(child)
            cat_item.setExpanded(True)

        # Cabling: itemized separately by medium, since fiber and copper are priced
        # and run-limited very differently -- not just a color difference under the hood.
        total_cable_ft = sum((c.get_length_feet() or 0.0) for c in copper_cables)
        total_fiber_ft = sum((c.get_length_feet() or 0.0) for c in fiber_cables)
        cable_cost = total_cable_ft * self.cost_per_foot
        fiber_cost = total_fiber_ft * self.fiber_cost_per_foot

        if copper_cables or fiber_cables:
            cat_item = QTreeWidgetItem(["Cabling", "", "", ""])
            font = cat_item.font(0)
            font.setBold(True)
            cat_item.setFont(0, font)
            self.tree.addTopLevelItem(cat_item)

            if copper_cables:
                child = QTreeWidgetItem(["Copper (CAT6)", f"{total_cable_ft:,.0f} ft",
                                          f"${self.cost_per_foot:,.2f}/ft", f"${cable_cost:,.0f}"])
                cat_item.addChild(child)
            if fiber_cables:
                child = QTreeWidgetItem(["Fiber Link", f"{total_fiber_ft:,.0f} ft",
                                          f"${self.fiber_cost_per_foot:,.2f}/ft", f"${fiber_cost:,.0f}"])
                cat_item.addChild(child)
            cat_item.setExpanded(True)

        # ── Terminations ──
        # Every run has to land on something at both ends, and how it lands is real
        # hardware: a crimped plug, or a jack plus a patch cable. This is the part a
        # footage-only takeoff misses entirely. See core/termination.py.
        materials = termination.count_materials(self.canvas_view)
        plug_price = price(KEY_PLUG_PRICE)
        jack_price = price(KEY_JACK_PRICE)
        patch_price = price(KEY_PATCH_PRICE)
        termination_cost = (materials.plugs * plug_price
                            + materials.jacks * jack_price
                            + materials.patch_cables * patch_price)

        if materials.plugs or materials.jacks or materials.patch_cables:
            cat_item = QTreeWidgetItem(["Terminations", "", "", ""])
            font = cat_item.font(0)
            font.setBold(True)
            cat_item.setFont(0, font)
            self.tree.addTopLevelItem(cat_item)

            def add_line(label, qty, unit_price, detail_field=None):
                child = QTreeWidgetItem([label, f"{qty} qty", f"${unit_price:,.2f}",
                                          f"${qty * unit_price:,.0f}"])
                cat_item.addChild(child)
                if detail_field:
                    # Which kinds of object account for the count, so a number can be
                    # checked against the job rather than taken on faith.
                    for category, counts in sorted(materials.by_category.items()):
                        if counts[detail_field]:
                            child.addChild(QTreeWidgetItem(
                                [f"  {termination.category_label(category)}",
                                 f"{counts[detail_field]} qty", "", ""]))
                    child.setExpanded(True)
                return child

            if materials.plugs:
                add_line("RJ45 Plugs", materials.plugs, plug_price, "plugs")
            if materials.jacks:
                add_line("Keystone Jacks", materials.jacks, jack_price, "jacks")
            if materials.patch_cables:
                patch_line = add_line("Patch Cables", materials.patch_cables, patch_price)
                # Drawn ones exist in the Rack Editor; implied ones are what the jacks
                # still need. Split out so the two can be reconciled against the racks.
                patch_line.setToolTip(0, f"{materials.patch_cables_drawn} drawn in the "
                                          f"Rack Editor, {materials.patch_cables_implied} "
                                          f"implied by jack terminations")
                if materials.patch_cables_drawn:
                    patch_line.addChild(QTreeWidgetItem(
                        ["  Drawn in Rack Editor", f"{materials.patch_cables_drawn} qty", "", ""]))
                if materials.patch_cables_implied:
                    patch_line.addChild(QTreeWidgetItem(
                        ["  Implied by jacks", f"{materials.patch_cables_implied} qty", "", ""]))
                patch_line.setExpanded(True)
            cat_item.setExpanded(True)

        for i in range(4):
            self.tree.resizeColumnToContents(i)

        grand_total = equipment_total + cable_cost + fiber_cost + termination_cost

        def uncalibrated(cables):
            return any(c.get_length_feet() is None and c.pixel_length > 0 for c in cables)

        cable_text = f"{total_cable_ft:,.0f} ft"
        if uncalibrated(copper_cables):
            cable_text += " (some not calibrated)"
        fiber_text = f"{total_fiber_ft:,.0f} ft"
        if uncalibrated(fiber_cables):
            fiber_text += " (some not calibrated)"

        self.lbl_total_cable_ft.setText(cable_text)
        self.lbl_total_fiber_ft.setText(fiber_text)
        self.lbl_equipment_cost.setText(f"${equipment_total:,.0f}")
        self.lbl_cable_cost.setText(f"${cable_cost:,.0f}")
        self.lbl_fiber_cost.setText(f"${fiber_cost:,.0f}")
        self.lbl_termination_cost.setText(f"${termination_cost:,.0f}")
        self.lbl_total_cost.setText(f"${grand_total:,.0f}")
