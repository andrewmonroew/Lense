from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QTreeWidget, QTreeWidgetItem, QPushButton, QLabel
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor

from src.core import poe_chain


class ValidationPanel(QWidget):
    """Every PoE problem in the design, in one list.

    The per-device warning badge on the canvas is easy to miss on a dense layout and
    says nothing until you hover it, so this is the "show me everything that won't
    work" view: errors first, then the conditional warnings, each row clickable
    straight to the offending device.
    """

    SEVERITY_COLORS = {"error": "#ef4444", "warning": "#f59e0b"}

    def __init__(self, canvas_view, parent=None):
        super().__init__(parent)
        self.canvas_view = canvas_view

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        hint = QLabel("PoE power-chain problems. Click a row to jump to the device.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #8888a0; font-size: 11px;")
        layout.addWidget(hint)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet("font-size: 11px; font-weight: bold;")
        layout.addWidget(self.summary)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Device", "Problem"])
        self.tree.setColumnCount(2)
        self.tree.setWordWrap(True)
        self.tree.itemClicked.connect(self._on_item_clicked)
        layout.addWidget(self.tree, 1)

        btn_row = QHBoxLayout()
        refresh_btn = QPushButton("Re-check")
        refresh_btn.clicked.connect(self.refresh)
        btn_row.addStretch()
        btn_row.addWidget(refresh_btn)
        layout.addLayout(btn_row)

        # Same light periodic refresh the Inventory/Object Explorer panels use -- there's
        # no single central "design changed" signal to hang off, and re-walking the power
        # chains is cheap at real project sizes.
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

    def refresh(self):
        self.tree.clear()
        if self.canvas_view is None:
            return

        found = poe_chain.evaluate_project(self.canvas_view)
        errors = sum(1 for severity, _, _ in found if severity == "error")
        warnings = len(found) - errors

        if not found:
            self.summary.setText("No PoE problems found.")
            self.summary.setStyleSheet("color: #22c55e; font-size: 11px; font-weight: bold;")
            return

        bits = []
        if errors:
            bits.append(f"{errors} error{'s' if errors != 1 else ''}")
        if warnings:
            bits.append(f"{warnings} warning{'s' if warnings != 1 else ''}")
        self.summary.setText(" · ".join(bits))
        self.summary.setStyleSheet(
            f"color: {self.SEVERITY_COLORS['error' if errors else 'warning']}; "
            f"font-size: 11px; font-weight: bold;")

        for severity, device, message in found:
            row = QTreeWidgetItem([getattr(device, "label", "?"), message])
            row.setData(0, Qt.UserRole, device)
            row.setForeground(0, QColor(self.SEVERITY_COLORS.get(severity, "#e8e8f0")))
            row.setToolTip(1, message)
            self.tree.addTopLevelItem(row)
        self.tree.resizeColumnToContents(0)
