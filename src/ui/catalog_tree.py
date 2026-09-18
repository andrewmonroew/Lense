import json
import os
import sys
from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem, QAbstractItemView, QVBoxLayout, QWidget, QLineEdit
from PySide6.QtCore import Qt, QMimeData
from PySide6.QtGui import QDrag
from src.core.utils import CATALOG_SPEC_MIME_TYPE

CATEGORY_LABELS = {"camera": "Cameras", "switch": "Switches", "nvr": "NVRs", "patch-panel": "Patch Panels",
                    "access-point": "Access Points", "drop": "Wall Drops", "misc": "Misc Devices"}

class EquipmentTreeWidget(QTreeWidget):
    """QTreeWidget that hands off a spec_id (via a custom MIME type) when a leaf item is dragged out."""
    def mimeData(self, items):
        mime = QMimeData()
        if items:
            spec_id = items[0].data(0, Qt.UserRole)
            if spec_id:
                mime.setData(CATALOG_SPEC_MIME_TYPE, spec_id.encode("utf-8"))
        return mime

class CatalogTree(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(4)

        # Search box
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search equipment...")
        self.search_box.textChanged.connect(self.filter_tree)
        self.layout.addWidget(self.search_box)

        # Tree widget
        self.tree = EquipmentTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setDragEnabled(True)
        self.tree.setDragDropMode(QAbstractItemView.DragOnly)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.layout.addWidget(self.tree)

        self.tree.doubleClicked.connect(self.on_item_double_clicked)
        self.tree.itemClicked.connect(self.on_item_single_clicked)

        # Loaded database data: spec_id -> merged spec dict (built-ins with the
        # custom_equipment.json overlay applied on top)
        self.catalog_data = {}
        self.builtin_ids = set()  # ids that exist in the shipped (read-only) files
        self.nodes = {}

        self.load_databases()

    # ── Paths ──
    def get_data_dir(self):
        if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
            return os.path.join(sys._MEIPASS, "data")
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(base_dir, "data")

    def get_custom_file_path(self):
        return os.path.join(self.get_data_dir(), "custom_equipment.json")

    def get_images_dir(self):
        return os.path.join(self.get_data_dir(), "images")

    # ── Loading / merging ──
    def load_databases(self):
        """(Re)loads the built-in catalog files plus the custom_equipment.json overlay
        (user additions/edits/deletions), merges them by id, and rebuilds the tree."""
        self.catalog_data = {}
        self.builtin_ids = set()
        data_dir = self.get_data_dir()

        builtin_files = [
            (os.path.join(data_dir, "cameras-ubiquiti.json"), "Ubiquiti", "camera"),
            (os.path.join(data_dir, "cameras-reolink.json"), "Reolink", "camera"),
            (os.path.join(data_dir, "switches.json"), None, "switch"),
            (os.path.join(data_dir, "nvrs.json"), None, "nvr"),
            (os.path.join(data_dir, "patch-panels.json"), None, "patch-panel"),
            (os.path.join(data_dir, "access-points.json"), "Ubiquiti", "access-point"),
            (os.path.join(data_dir, "drops.json"), None, "drop"),
            (os.path.join(data_dir, "misc.json"), None, "misc"),
        ]
        for filepath, mfg_override, category in builtin_files:
            for entry in self._read_json_list(filepath):
                entry = dict(entry)
                entry.setdefault("category", category)
                if mfg_override:
                    entry.setdefault("manufacturer", mfg_override)
                spec_id = entry.get("id")
                if spec_id:
                    self.catalog_data[spec_id] = entry
                    self.builtin_ids.add(spec_id)

        # Overlay: additions/edits replace by id; {"deleted": true} tombstones a built-in id
        for entry in self._read_json_list(self.get_custom_file_path()):
            spec_id = entry.get("id")
            if not spec_id:
                continue
            if entry.get("deleted"):
                self.catalog_data.pop(spec_id, None)
            else:
                self.catalog_data[spec_id] = entry

        self._build_tree()

    def reload(self):
        self.load_databases()

    def _read_json_list(self, filepath):
        if not os.path.exists(filepath):
            return []
        try:
            with open(filepath, 'r') as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception as e:
            print(f"Error loading {filepath}: {e}")
            return []

    # ── Overlay persistence (used by the catalog manager) ──
    def save_overlay_entry(self, entry):
        """Adds or updates a custom_equipment.json entry (new item, or an edit/override of
        a built-in one) and refreshes the merged catalog + tree."""
        overlay = self._read_json_list(self.get_custom_file_path())
        overlay = [e for e in overlay if e.get("id") != entry.get("id")]
        overlay.append(entry)
        self._write_overlay(overlay)
        self.load_databases()

    def delete_entry(self, spec_id):
        """Removes a catalog entry. A purely custom entry is dropped from the overlay file
        outright; a built-in id gets a tombstone so the shipped files stay untouched."""
        overlay = self._read_json_list(self.get_custom_file_path())
        overlay = [e for e in overlay if e.get("id") != spec_id]
        if spec_id in self.builtin_ids:
            overlay.append({"id": spec_id, "deleted": True})
        self._write_overlay(overlay)
        self.load_databases()

    def _write_overlay(self, overlay):
        with open(self.get_custom_file_path(), 'w') as f:
            json.dump(overlay, f, indent=4)

    def is_builtin(self, spec_id):
        return spec_id in self.builtin_ids

    # ── Tree construction ──
    def _build_tree(self):
        self.tree.clear()
        self.nodes = {}  # mfg -> {"item": QTreeWidgetItem, "categories": {cat_label: QTreeWidgetItem}}

        def category_item(mfg, cat_label):
            if mfg not in self.nodes:
                mfg_item = QTreeWidgetItem(self.tree)
                mfg_item.setText(0, mfg)
                mfg_item.setFlags(mfg_item.flags() & ~Qt.ItemIsDragEnabled & ~Qt.ItemIsSelectable)
                self.nodes[mfg] = {"item": mfg_item, "categories": {}}
            mfg_node = self.nodes[mfg]
            if cat_label not in mfg_node["categories"]:
                cat_item = QTreeWidgetItem(mfg_node["item"])
                cat_item.setText(0, cat_label)
                cat_item.setFlags(cat_item.flags() & ~Qt.ItemIsDragEnabled & ~Qt.ItemIsSelectable)
                mfg_node["categories"][cat_label] = cat_item
            return mfg_node["categories"][cat_label]

        for spec_id, entry in sorted(self.catalog_data.items(),
                                      key=lambda kv: (kv[1].get("manufacturer", "Other"), kv[1].get("model", ""))):
            mfg = entry.get("manufacturer", "Other")
            cat_label = CATEGORY_LABELS.get(entry.get("category", "camera"), "Other")
            parent_node = category_item(mfg, cat_label)

            child = QTreeWidgetItem(parent_node)
            child.setText(0, entry.get("model", spec_id))
            child.setData(0, Qt.UserRole, spec_id)
            child.setToolTip(0, f"{entry.get('model', '')} - {entry.get('resolution', '') or entry.get('ports', '')}")
            if spec_id not in self.builtin_ids:
                font = child.font(0)
                font.setItalic(True)  # mark user-added entries
                child.setFont(0, font)

        self.tree.expandAll()

    def on_item_double_clicked(self, index):
        item = self.tree.currentItem()
        if not item:
            return
        spec_id = item.data(0, Qt.UserRole)
        if spec_id:
            # Broadcast double click event or notify parent
            parent_window = self.window()
            if hasattr(parent_window, "trigger_catalog_placement"):
                parent_window.trigger_catalog_placement(spec_id)

    def on_item_single_clicked(self, item, _column):
        spec_id = item.data(0, Qt.UserRole)
        if spec_id:
            parent_window = self.window()
            if hasattr(parent_window, "preview_catalog_item"):
                parent_window.preview_catalog_item(spec_id)

    def filter_tree(self, text):
        """Simple filter for searching models in the catalog tree."""
        text = text.lower()
        self.filter_item(self.tree.invisibleRootItem(), text)

    def filter_item(self, item, text):
        child_count = item.childCount()
        any_child_visible = False

        for i in range(child_count):
            child = item.child(i)
            spec_id = child.data(0, Qt.UserRole)
            if spec_id:  # It's a leaf item (product)
                match = text in child.text(0).lower()
                child.setHidden(not match)
                if match:
                    any_child_visible = True
            else:  # Category or Manufacturer node
                child_visible = self.filter_item(child, text)
                child.setHidden(not child_visible)
                if child_visible:
                    any_child_visible = True

        return any_child_visible
