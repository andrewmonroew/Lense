import os
import sys
import json
import base64
from PySide6.QtWidgets import (QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QToolBar,
                             QStatusBar, QFileDialog, QMessageBox, QSplitter, QLabel,
                             QMessageBox, QDockWidget, QMenuBar, QMenu, QGraphicsItem, QTabWidget,
                             QApplication, QDoubleSpinBox)
from PySide6.QtCore import Qt, QPointF, Slot, QSettings, QBuffer, QIODevice
from PySide6.QtGui import QAction, QIcon, QKeySequence, QPixmap, QTransform

from src.ui.catalog_tree import CatalogTree
from src.ui.catalog_manager import CatalogManagerDialog
from src.ui.properties_sidebar import PropertiesSidebar
from src.ui.canvas_view import CanvasView
from src.ui.inventory_panel import InventoryPanel
from src.ui.object_explorer import ObjectExplorerPanel
from src.ui.validation_panel import ValidationPanel
from src.ui.network_diagram_view import NetworkDiagramView
from src.ui.rack_editor_panel import RackEditorPanel
from src.ui.new_project_dialog import NewProjectDialog

from src.graphics.camera_item import CameraItem
from src.core import perf
from src.core.version import title as app_title
from src.graphics.device_factory import make_device_item
from src.ui.settings_dialog import (KEY_REOPEN_LAST, SettingsDialog,
                                    apply_saved_settings, get_bool)
from src.graphics.cable_item import CableItem, recalculate_all_cable_offsets
from src.graphics.custom_item import CustomItem
from src.graphics.label_item import LabelItem
from src.graphics.zone_item import ZoneItem
from src.graphics.rack_item import RackItem, RackSlot
from src.core.undo_manager import UndoManager

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(app_title("CCTV CAD Design Tool"))
        self.resize(1200, 800)

        # Apply dark stylesheet
        self.apply_dark_theme()

        # Canvas View (Center) -- lives inside a bottom-tabbed sheet switcher, Excel-style,
        # alongside the auto-generated Network Diagram and any open Rack Editor tabs
        self.canvas_view = CanvasView(self)

        self.sheet_tabs = QTabWidget(self)
        self.sheet_tabs.setTabPosition(QTabWidget.South)
        self.sheet_tabs.setTabsClosable(True)
        self.sheet_tabs.tabCloseRequested.connect(self._on_sheet_tab_close_requested)

        self.sheet_tabs.addTab(self.canvas_view, "Canvas")
        self.network_diagram = NetworkDiagramView(self.canvas_view)
        self.sheet_tabs.addTab(self.network_diagram, "Network Diagram")
        self.network_diagram.jump_to_object.connect(self._jump_to_object_on_canvas)
        # Canvas and Network Diagram are permanent -- only dynamically-opened Rack
        # Editor tabs get a close button.
        for i in (0, 1):
            self.sheet_tabs.tabBar().setTabButton(i, self.sheet_tabs.tabBar().ButtonPosition.RightSide, None)

        self.canvas_view.rack_open_requested.connect(self.open_rack_tab)
        self.sheet_tabs.currentChanged.connect(self._on_sheet_tab_changed)

        self.setCentralWidget(self.sheet_tabs)

        # Left catalog Panel
        self.catalog_dock = QDockWidget("Equipment Catalog", self)
        self.catalog_dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        self.catalog_tree = CatalogTree(self)
        self.catalog_dock.setWidget(self.catalog_tree)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.catalog_dock)
        self.canvas_view.set_catalog_data(self.catalog_tree.catalog_data)

        # Right properties Sidebar
        self.sidebar_dock = QDockWidget("Properties Panel", self)
        self.sidebar_dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        self.sidebar = PropertiesSidebar(self)
        self.sidebar.set_canvas_view(self.canvas_view)
        self.sidebar_dock.setWidget(self.sidebar)
        self.addDockWidget(Qt.RightDockWidgetArea, self.sidebar_dock)

        # Inventory & Cost Analysis panel — tabbed with Properties on the same side,
        # closable/reopenable independently (View menu)
        self.inventory_dock = QDockWidget("Inventory & Cost Analysis", self)
        self.inventory_dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        self.inventory_panel = InventoryPanel(self.canvas_view)
        self.inventory_dock.setWidget(self.inventory_panel)
        self.addDockWidget(Qt.RightDockWidgetArea, self.inventory_dock)
        self.tabifyDockWidget(self.sidebar_dock, self.inventory_dock)

        # Object Explorer -- itemized listing of every placed object (cabling excluded
        # for now, undecided how it should be represented here), tabbed alongside
        # Properties/Inventory so it's a click away without eating screen space by default.
        self.explorer_dock = QDockWidget("Object Explorer", self)
        self.explorer_dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        self.object_explorer = ObjectExplorerPanel(self.canvas_view)
        self.explorer_dock.setWidget(self.object_explorer)
        self.addDockWidget(Qt.RightDockWidgetArea, self.explorer_dock)
        self.tabifyDockWidget(self.sidebar_dock, self.explorer_dock)

        # Design Validation -- every PoE power-chain problem in one list, since a
        # per-device badge on a dense canvas is easy to miss.
        self.validation_dock = QDockWidget("Design Validation", self)
        self.validation_dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        self.validation_panel = ValidationPanel(self.canvas_view)
        self.validation_dock.setWidget(self.validation_panel)
        self.addDockWidget(Qt.RightDockWidgetArea, self.validation_dock)
        self.tabifyDockWidget(self.sidebar_dock, self.validation_dock)

        self.sidebar_dock.raise_()  # Properties visible by default

        # Wire UI interactions
        self.canvas_view.item_selected.connect(self.sidebar.select_item)
        self.canvas_view.item_property_changed.connect(lambda item: self.sidebar.select_item(item))
        self.sidebar.property_changed.connect(self.canvas_view.scene_obj.update)

        # Navigation status bar
        self.create_status_bar()

        # Action commands
        self.create_actions()

        # Menu bar
        self.create_menu_bar()

        # Visibility bar (sits between the menu bar and the main toolbar)
        self.create_visibility_bar()
        self.addToolBarBreak(Qt.TopToolBarArea)

        # Main Toolbar
        self.create_toolbar()

        # Defined default state before any project is opened/loaded -- gets replaced
        # by whatever project_type a loaded/restored project actually carries.
        self._apply_project_mode("cctv")

        # Keyboard shortcuts helper
        self.setup_shortcuts()

        # Undo/redo -- snapshot-based (see UndoManager), wired onto CanvasView (and
        # reachable from RackEditorPanel via canvas_view) through plain callables so
        # neither has to import MainWindow directly.
        self.undo_manager = UndoManager(self)
        self.canvas_view.push_undo_snapshot = self.undo_manager.snapshot
        self.canvas_view.capture_undo_state = self._serialize_state
        self.canvas_view.commit_undo_state = self.undo_manager.commit_raw_snapshot
        self.canvas_view.on_calibration_changed = self._on_calibration_changed
        undo_action = QAction("Undo", self)
        undo_action.setShortcut(QKeySequence("Ctrl+Z"))
        undo_action.setShortcutContext(Qt.ApplicationShortcut)
        undo_action.triggered.connect(self.undo_manager.undo)
        self.addAction(undo_action)
        redo_action = QAction("Redo", self)
        redo_action.setShortcut(QKeySequence("Ctrl+X"))
        redo_action.setShortcutContext(Qt.ApplicationShortcut)
        redo_action.triggered.connect(self.undo_manager.redo)
        self.addAction(redo_action)

        # Project filepath tracker
        self.current_project_path = None

        # Reopen whatever project was open last time, so closing and relaunching the
        # app picks up right where you left off instead of an empty canvas. Falls back
        # to the dev test image if there's no remembered project (first run, or the
        # remembered file has since been moved/deleted).
        last_path = self._get_last_project_path() if get_bool(KEY_REOPEN_LAST) else None
        if last_path and os.path.exists(last_path):
            self.open_project_filepath(last_path)
        else:
            self.load_default_test_image()

        # Saved preferences apply after the project loads, so the icon scale lands on
        # the items that were just reconstructed rather than on an empty scene.
        apply_saved_settings(self)
        self._sync_visibility_actions()
        self.grid_size_spin.blockSignals(True)
        self.grid_size_spin.setValue(self.canvas_view.scene_obj.effective_grid_size())
        self.grid_size_spin.blockSignals(False)

    def _jump_to_object_on_canvas(self, obj):
        """Clicking a camera/device node in the Network Diagram tab should feel like a
        real link back to the design, not just a picture -- switch to Canvas and
        select/center on the actual object."""
        self.sheet_tabs.setCurrentWidget(self.canvas_view)
        self.canvas_view.select_and_focus_item(obj)

    def open_rack_tab(self, rack):
        """Opens (or focuses, if already open) a sheet tab for editing this rack's
        contents. A tab rather than a dialog specifically so the Equipment Catalog
        dock stays reachable for drag-and-drop while it's open."""
        # The rack (and possibly other items) is still selected on the Canvas at this
        # point -- it was just double-clicked to get here. If keyboard focus ever ends
        # up back on the (now hidden) Canvas while working in this tab, a stray
        # Delete/Backspace would silently wipe whatever's still selected there. Clear
        # it now so there's nothing dangerous sitting selected in the background.
        self.canvas_view.scene_obj.clearSelection()

        for i in range(self.sheet_tabs.count()):
            widget = self.sheet_tabs.widget(i)
            if getattr(widget, "rack", None) is rack:
                self.sheet_tabs.setCurrentIndex(i)
                return

        panel = RackEditorPanel(rack, self.canvas_view, self)
        index = self.sheet_tabs.addTab(panel, rack.label)
        panel.renamed.connect(lambda new_name, p=panel: self._sync_rack_tab_title(p, new_name))
        panel.device_selected.connect(self.sidebar.select_item)
        self.sheet_tabs.setCurrentIndex(index)

    def _sync_rack_tab_title(self, panel, new_name):
        # The rack's internal id is what actually identifies the tab (see
        # open_rack_tab's lookup by `widget.rack is rack`) -- renaming just needs to
        # keep the visible tab text in sync with it, found fresh each time in case
        # tabs have been reordered or others closed since.
        index = self.sheet_tabs.indexOf(panel)
        if index >= 0:
            self.sheet_tabs.setTabText(index, new_name or "Rack")

    def _on_sheet_tab_changed(self, index):
        # Keeps CanvasView.is_active_tab in sync so it can refuse to act on a stray
        # Delete/Backspace whenever Qt's keyboard focus drifts back there while some
        # other tab (Rack Editor, Network Diagram) is what's actually on screen --
        # see CanvasView.delete_selected(). Checked by widget identity, not tab title,
        # since titles can be renamed.
        self.canvas_view.is_active_tab = (self.sheet_tabs.widget(index) is self.canvas_view)

        # A Rack Editor only rebuilds itself in response to actions taken inside it, so
        # a cable drawn on the Canvas and anchored to that rack wouldn't show up as a
        # pitchfork stub until something in there happened to trigger a rebuild. Re-read
        # the scene when its tab is actually selected -- an explicit once-per-switch
        # signal, rather than hanging it off widget visibility, which fires at moments
        # we don't control (and could rebuild mid-interaction).
        widget = self.sheet_tabs.widget(index)
        if getattr(widget, "rack", None) is not None:
            widget.refresh()

    def _on_sheet_tab_close_requested(self, index):
        widget = self.sheet_tabs.widget(index)
        if widget is None or getattr(widget, "rack", None) is None:
            return  # Canvas / Network Diagram are permanent, no close button anyway
        self.sheet_tabs.removeTab(index)
        widget.deleteLater()

    def _close_all_rack_tabs(self):
        """Called from new_project()/project load -- any open Rack Editor tab refers
        to a rack that's about to stop existing, so it can't stay open."""
        for i in range(self.sheet_tabs.count() - 1, -1, -1):
            widget = self.sheet_tabs.widget(i)
            if getattr(widget, "rack", None) is not None:
                self.sheet_tabs.removeTab(i)
                widget.deleteLater()

    def apply_dark_theme(self):
        # Premium dark style sheet matching modern CAD interfaces
        dark_stylesheet = """
        QMainWindow {
            background-color: #0a0a0f;
        }
        QMenuBar, QMenu {
            background-color: #111118;
            color: #e8e8f0;
            border-bottom: 1px solid #1c1c28;
        }
        QMenuBar::item:selected, QMenu::item:selected {
            background-color: #3b82f6;
            color: white;
        }
        QToolBar {
            background-color: #111118;
            border-bottom: 1px solid #1c1c28;
            spacing: 6px;
            padding: 4px;
        }
        QDockWidget {
            color: #a0a0b8;
            font-weight: bold;
            titlebar-close-icon: none;
            titlebar-normal-icon: none;
        }
        QDockWidget::title {
            background-color: #111118;
            padding-top: 6px;
            padding-bottom: 6px;
            border-bottom: 1px solid #1c1c28;
        }
        QTreeWidget, QScrollArea, QWidget#container {
            background-color: #111118;
            color: #e8e8f0;
            border: none;
        }
        QLineEdit, QTextEdit, QComboBox, QDoubleSpinBox, QSpinBox {
            background-color: #1a1a25;
            color: #e8e8f0;
            border: 1px solid #2e2e3f;
            border-radius: 4px;
            padding: 4px 6px;
            font-family: 'JetBrains Mono', monospace;
            font-size: 11px;
        }
        QLineEdit:focus, QComboBox:focus, QDoubleSpinBox:focus, QSpinBox:focus {
            border-color: #3b82f6;
        }
        QComboBox::drop-down {
            border: none;
        }
        QComboBox QAbstractItemView {
            background-color: #1a1a25;
            color: #e8e8f0;
            selection-background-color: #3b82f6;
            border: 1px solid #2e2e3f;
        }
        QGroupBox {
            border: 1px solid #2e2e3f;
            border-radius: 6px;
            margin-top: 12px;
            font-weight: bold;
            color: #60a5fa;
            font-size: 12px;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            left: 10px;
            padding: 0 4px;
        }
        QLabel {
            color: #a0a0b8;
            font-size: 11px;
        }
        QStatusBar {
            background-color: #111118;
            color: #8888a0;
            border-top: 1px solid #1c1c28;
        }
        QDialog {
            background-color: #0a0a0f;
        }
        QPushButton, QToolButton {
            background-color: #1a1a25;
            color: #e8e8f0;
            border: 1px solid #2e2e3f;
            border-radius: 4px;
            padding: 6px 14px;
            font-size: 11px;
        }
        QToolButton {
            padding: 4px 8px;
        }
        QPushButton:hover, QToolButton:hover {
            background-color: #22222e;
            border-color: #3b82f6;
        }
        QPushButton:pressed, QToolButton:pressed {
            background-color: #1c1c28;
        }
        QPushButton:disabled, QToolButton:disabled {
            color: #555568;
            border-color: #1c1c28;
        }
        QToolButton:checked {
            background-color: #1e3a5f;
            border-color: #3b82f6;
            color: #e8e8f0;
        }
        QToolBar QToolButton {
            background-color: transparent;
            border: 1px solid transparent;
        }
        QToolBar QToolButton:hover {
            background-color: #22222e;
            border-color: #2e2e3f;
        }
        QToolBar QToolButton:checked, QToolBar QToolButton:pressed {
            background-color: #1e3a5f;
            border-color: #3b82f6;
        }
        QCheckBox {
            color: #e8e8f0;
            font-size: 11px;
            spacing: 6px;
        }
        QCheckBox::indicator {
            width: 14px;
            height: 14px;
            border: 1px solid #2e2e3f;
            border-radius: 3px;
            background-color: #1a1a25;
        }
        QCheckBox::indicator:checked {
            background-color: #3b82f6;
            border-color: #3b82f6;
        }
        QTabWidget::pane {
            background-color: #0a0a0f;
            border-top: 1px solid #1c1c28;
        }
        QTabBar::tab {
            background-color: #111118;
            color: #8888a0;
            padding: 6px 16px;
            border: 1px solid #1c1c28;
            border-bottom: none;
        }
        QTabBar::tab:selected {
            background-color: #0a0a0f;
            color: #e8e8f0;
            border-bottom: 2px solid #3b82f6;
        }
        QTabBar::tab:hover:!selected {
            background-color: #1a1a25;
        }
        QSplitter::handle {
            background-color: #1c1c28;
        }
        QSplitter::handle:hover {
            background-color: #3b82f6;
        }
        QScrollBar:vertical {
            background-color: #111118;
            width: 12px;
            margin: 0;
        }
        QScrollBar::handle:vertical {
            background-color: #2e2e3f;
            border-radius: 5px;
            min-height: 24px;
        }
        QScrollBar::handle:vertical:hover {
            background-color: #3b82f6;
        }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
            height: 0;
        }
        QScrollBar:horizontal {
            background-color: #111118;
            height: 12px;
            margin: 0;
        }
        QScrollBar::handle:horizontal {
            background-color: #2e2e3f;
            border-radius: 5px;
            min-width: 24px;
        }
        QScrollBar::handle:horizontal:hover {
            background-color: #3b82f6;
        }
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
            width: 0;
        }
        QSlider::groove:horizontal {
            background-color: #1a1a25;
            height: 4px;
            border-radius: 2px;
        }
        QSlider::handle:horizontal {
            background-color: #3b82f6;
            width: 12px;
            margin: -4px 0;
            border-radius: 6px;
        }
        """
        # Applied at the QApplication level, not just on self -- a widget-level
        # setStyleSheet() only cascades to children WITHIN this same top-level
        # window. CatalogManagerDialog and CalibrationDialog are separate top-level
        # windows (QDialog, not a child widget of MainWindow), so they'd render with
        # zero styling -- plain white Qt default -- if this only lived on self.
        QApplication.instance().setStyleSheet(dark_stylesheet)

    def create_actions(self):
        # File operations
        self.act_new = QAction("New Project", self, shortcut=QKeySequence.New, triggered=self.prompt_new_project)
        self.act_open = QAction("Open Project...", self, shortcut=QKeySequence.Open, triggered=self.open_project)
        self.act_save = QAction("Save Project", self, shortcut=QKeySequence.Save, triggered=self.save_project)
        self.act_save_as = QAction("Save Project As...", self, shortcut=QKeySequence.SaveAs, triggered=self.save_project_as)
        self.act_import_fp = QAction("Import Floor Plan...", self, shortcut="Ctrl+I", triggered=self.import_floorplan)
        self.act_export_img = QAction("Export Layout to Image...", self, shortcut="Ctrl+E", triggered=self.export_to_image)
        self.act_exit = QAction("Exit", self, triggered=self.close)

        # Drawing Tool modes
        self.act_tool_select = QAction("Select/Move (V)", self, checkable=True, triggered=lambda: self.set_active_tool("select"))
        self.act_tool_pan = QAction("Pan Canvas (H)", self, checkable=True, triggered=lambda: self.set_active_tool("pan"))
        self.act_tool_calibrate = QAction("Calibrate scale (C)", self, checkable=True, triggered=lambda: self.set_active_tool("calibrate"))
        self.act_tool_place = QAction("Place Equipment (P)", self, checkable=True, triggered=lambda: self.set_active_tool("place"))
        self.act_tool_camera = QAction("Place Camera (K)", self, checkable=True, triggered=lambda: self.set_active_tool("camera"))
        self.act_tool_cable = QAction("Draw Cable (L)", self, checkable=True, triggered=lambda: self.set_active_tool("cable"))
        self.act_tool_fiber = QAction("Draw Fiber Link (B)", self, checkable=True, triggered=lambda: self.set_active_tool("fiber"))
        self.act_tool_infra = QAction("Place Switch/NVR (N)", self, checkable=True, triggered=lambda: self.set_active_tool("infra"))
        self.act_tool_measure = QAction("Measure Distance (M)", self, checkable=True, triggered=lambda: self.set_active_tool("measure"))
        self.act_tool_labelzone = QAction("Label/Zone (T)", self, checkable=True, triggered=lambda: self.set_active_tool("labelzone"))
        self.act_tool_lasso = QAction("Lasso Cables (G)", self, checkable=True, triggered=lambda: self.set_active_tool("lasso"))
        self.act_tool_lasso.setToolTip("Drag a line across a fan of cables to gather them "
                                        "into one bundled crossing point")

        # Group tool modes so only one is active at a time
        self.tool_actions = [
            self.act_tool_select, self.act_tool_pan, self.act_tool_calibrate, self.act_tool_place,
            self.act_tool_camera, self.act_tool_cable, self.act_tool_fiber, self.act_tool_infra,
            self.act_tool_measure, self.act_tool_labelzone, self.act_tool_lasso
        ]
        self.act_tool_select.setChecked(True)

        # Calibrate/Place Camera/Place Switch-NVR are redundant with the catalog drag-drop
        # and the unified Place tool, so they (and Import Floor Plan) live in the Edit menu
        # instead of cluttering the toolbar. They stay fully functional via their shortcuts.
        self.toolbar_tool_actions = [
            self.act_tool_select, self.act_tool_pan, self.act_tool_place,
            self.act_tool_cable, self.act_tool_fiber, self.act_tool_measure, self.act_tool_labelzone,
            self.act_tool_lasso
        ]

        # Visibility toggles (the Visibility bar) -- declutter the canvas when cameras
        # are placed densely by hiding whole categories of rendering at once.
        self.act_toggle_cables = QAction("Cables", self, checkable=True, checked=True, triggered=self.toggle_show_cables)
        self.act_toggle_icons = QAction("Icons", self, checkable=True, checked=True, triggered=self.toggle_show_icons)
        self.act_toggle_fov = QAction("FOV", self, checkable=True, checked=True, triggered=self.toggle_show_fov)
        self.act_toggle_grid = QAction("Grid", self, checkable=True, checked=False, triggered=self.toggle_show_grid)
        self.act_toggle_snap = QAction("Snap to Grid", self, checkable=True, checked=False, triggered=self.toggle_snap_to_grid)

        # View actions
        self.act_zoom_in = QAction("Zoom In", self, shortcut=QKeySequence.ZoomIn, triggered=self.canvas_view.zoom_in)
        self.act_zoom_out = QAction("Zoom Out", self, shortcut=QKeySequence.ZoomOut, triggered=self.canvas_view.zoom_out)
        self.act_zoom_fit = QAction("Zoom to Fit", self, shortcut="F", triggered=self.canvas_view.zoom_fit)
        
        # Edit actions
        self.act_duplicate = QAction("Duplicate Selected", self, shortcut=QKeySequence("Ctrl+D"), triggered=self.canvas_view.duplicate_selected)
        self.act_delete = QAction("Delete Selected", self, shortcut=QKeySequence.Delete, triggered=self.canvas_view.delete_selected)
        self.act_manage_catalog = QAction("Manage Equipment Catalog...", self, triggered=self.open_catalog_manager)
        self.act_settings = QAction("Settings...", self, shortcut=QKeySequence("Ctrl+,"),
                                    triggered=self.open_settings)

    def create_menu_bar(self):
        menu = self.menuBar()

        # File Menu
        file_menu = menu.addMenu("&File")
        file_menu.addAction(self.act_new)
        file_menu.addAction(self.act_open)
        file_menu.addAction(self.act_save)
        file_menu.addAction(self.act_save_as)
        file_menu.addSeparator()
        file_menu.addAction(self.act_export_img)
        file_menu.addSeparator()
        file_menu.addAction(self.act_exit)

        # Edit Menu
        edit_menu = menu.addMenu("&Edit")
        edit_menu.addAction(self.act_duplicate)
        edit_menu.addAction(self.act_delete)
        edit_menu.addSeparator()
        # Redundant with catalog drag-drop / the Place tool, kept here rather than the toolbar
        edit_menu.addAction(self.act_import_fp)
        edit_menu.addAction(self.act_tool_calibrate)
        edit_menu.addAction(self.act_tool_camera)
        edit_menu.addAction(self.act_tool_infra)
        edit_menu.addSeparator()
        edit_menu.addAction(self.act_manage_catalog)
        edit_menu.addAction(self.act_settings)

        # View Menu
        view_menu = menu.addMenu("&View")
        view_menu.addAction(self.act_zoom_in)
        view_menu.addAction(self.act_zoom_out)
        view_menu.addAction(self.act_zoom_fit)
        view_menu.addSeparator()
        view_menu.addAction(self.sidebar_dock.toggleViewAction())
        view_menu.addAction(self.inventory_dock.toggleViewAction())
        view_menu.addAction(self.explorer_dock.toggleViewAction())
        view_menu.addAction(self.validation_dock.toggleViewAction())

    def create_visibility_bar(self):
        visibility_bar = QToolBar("Visibility", self)
        visibility_bar.setMovable(False)
        self.addToolBar(Qt.TopToolBarArea, visibility_bar)

        label = QLabel(" Show: ")
        label.setStyleSheet("color: #8888a0; font-weight: bold;")
        visibility_bar.addWidget(label)
        visibility_bar.addAction(self.act_toggle_cables)
        visibility_bar.addAction(self.act_toggle_icons)
        visibility_bar.addAction(self.act_toggle_fov)
        visibility_bar.addSeparator()
        visibility_bar.addAction(self.act_toggle_grid)
        visibility_bar.addAction(self.act_toggle_snap)

        self.grid_size_spin = QDoubleSpinBox()
        self.grid_size_spin.setRange(5.0, 2000.0)
        self.grid_size_spin.setSingleStep(5.0)
        self.grid_size_spin.setDecimals(1)
        self.grid_size_spin.setToolTip("Grid spacing (scene units -- 1 real-world foot once the "
                                        "floor plan is calibrated, until you change this)")
        self.grid_size_spin.setValue(self.canvas_view.scene_obj.effective_grid_size())
        self.grid_size_spin.valueChanged.connect(self.on_grid_size_changed)
        visibility_bar.addWidget(self.grid_size_spin)

    def create_toolbar(self):
        toolbar = QToolBar("Main Controls", self)
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        # Tool buttons (Calibrate/Place Camera/Place Switch-NVR/Import Floor Plan live in the
        # Edit menu instead — redundant here now that the catalog supports drag-drop and the
        # unified Place tool covers ad-hoc placement)
        for act in self.toolbar_tool_actions:
            toolbar.addAction(act)

        toolbar.addSeparator()
        toolbar.addAction(self.act_zoom_in)
        toolbar.addAction(self.act_zoom_out)
        toolbar.addAction(self.act_zoom_fit)
        toolbar.addSeparator()
        toolbar.addAction(self.act_duplicate)
        toolbar.addAction(self.act_delete)

    def create_status_bar(self):
        status = QStatusBar(self)
        self.setStatusBar(status)

        # Coords
        self.lbl_coords = QLabel("X: 0 Y: 0")
        self.lbl_coords.setStyleSheet("font-family: 'JetBrains Mono', monospace; font-size: 11px;")
        
        # Scale
        self.lbl_scale = QLabel("⚠ Scale: Not Calibrated")
        self.lbl_scale.setStyleSheet("color: #f59e0b; font-weight: bold;")
        
        # Info
        self.lbl_zoom = QLabel("Zoom: 100%")
        self.lbl_zoom.setStyleSheet("font-family: 'JetBrains Mono', monospace;")

        status.addPermanentWidget(self.lbl_coords, 1)
        status.addPermanentWidget(self.lbl_scale, 2)
        status.addPermanentWidget(self.lbl_zoom, 1)

        # Connect signals
        self.canvas_view.coords_changed.connect(self.update_coords)
        self.canvas_view.scale_changed.connect(self.update_scale_label)
        self.canvas_view.zoom_changed.connect(self.update_zoom_label)

    def setup_shortcuts(self):
        # Quick hotkeys to switch tools
        self.act_tool_select.setShortcut("V")
        self.act_tool_pan.setShortcut("H")
        self.act_tool_calibrate.setShortcut("C")
        self.act_tool_place.setShortcut("P")
        self.act_tool_camera.setShortcut("K")
        self.act_tool_cable.setShortcut("L")
        self.act_tool_fiber.setShortcut("B")
        self.act_tool_infra.setShortcut("N")
        self.act_tool_measure.setShortcut("M")
        self.act_tool_labelzone.setShortcut("T")
        self.act_tool_lasso.setShortcut("G")

    # ── Status Bar Slots ──
    @Slot(QPointF)
    def update_coords(self, pt):
        ratio = self.canvas_view.scale_ratio
        if ratio:
            # Show calibrated coordinate units
            fx = pt.x() / ratio
            fy = pt.y() / ratio
            m_factor = 0.3048
            self.lbl_coords.setText(f"X: {fx:.1f} ft ({fx*m_factor:.1f} m)  Y: {fy:.1f} ft ({fy*m_factor:.1f} m)")
        else:
            self.lbl_coords.setText(f"X: {pt.x():.0f} px  Y: {pt.y():.0f} px")

    @Slot(str)
    def update_scale_label(self, scale_str):
        self.lbl_scale.setText(scale_str)
        if "Not Calibrated" in scale_str:
            self.lbl_scale.setStyleSheet("color: #f59e0b; font-weight: bold;")
        else:
            self.lbl_scale.setStyleSheet("color: #22c55e; font-weight: bold;")

    @Slot(float)
    def update_zoom_label(self, zoom_pct):
        self.lbl_zoom.setText(f"Zoom: {zoom_pct:.0f}%")

    # ── Active Tool Toggle ──
    def set_active_tool(self, tool_name):
        # Set checked state on toolbar buttons
        for act in self.tool_actions:
            act.blockSignals(True)
            act.setChecked(False)
            act.blockSignals(False)

        if tool_name == "select":
            self.act_tool_select.setChecked(True)
        elif tool_name == "pan":
            self.act_tool_pan.setChecked(True)
        elif tool_name == "calibrate":
            self.act_tool_calibrate.setChecked(True)
        elif tool_name == "place":
            self.act_tool_place.setChecked(True)
        elif tool_name == "camera":
            self.act_tool_camera.setChecked(True)
        elif tool_name == "cable":
            self.act_tool_cable.setChecked(True)
        elif tool_name == "fiber":
            self.act_tool_fiber.setChecked(True)
        elif tool_name == "infra":
            self.act_tool_infra.setChecked(True)
        elif tool_name == "measure":
            self.act_tool_measure.setChecked(True)
        elif tool_name == "labelzone":
            self.act_tool_labelzone.setChecked(True)
        elif tool_name == "lasso":
            self.act_tool_lasso.setChecked(True)

        # Notify canvas
        self.canvas_view.set_tool(tool_name)

    # ── Trigger catalog item placement ──
    def trigger_catalog_placement(self, spec_id):
        spec = self.catalog_tree.catalog_data.get(spec_id)
        if not spec:
            return
            
        category = spec.get("category", "camera")
        if category == "camera":
            self.set_active_tool("camera")
            self.canvas_view.set_tool("camera", spec)
        else: # switch or nvr
            self.set_active_tool("infra")
            self.canvas_view.set_tool("infra", spec)

    def preview_catalog_item(self, spec_id):
        spec = self.catalog_tree.catalog_data.get(spec_id)
        if not spec:
            return
        self.canvas_view.scene_obj.clearSelection()
        self.sidebar.preview_catalog_item(spec)

    def open_settings(self):
        dialog = SettingsDialog(self)
        dialog.exec()
        # The dialog applies the canvas-affecting settings live (and undoes them on
        # Cancel), so there is nothing to re-apply here -- but the Visibility bar's
        # checkboxes are the source of truth for the current session and must not drift
        # out of step with the scene if a future setting starts touching it.
        self._sync_visibility_actions()

    def _sync_visibility_actions(self):
        scene = self.canvas_view.scene_obj
        for action, value in ((self.act_toggle_cables, scene.global_show_cables),
                              (self.act_toggle_icons, scene.global_show_icons),
                              (self.act_toggle_fov, scene.global_show_fov),
                              (self.act_toggle_grid, scene.show_grid),
                              (self.act_toggle_snap, scene.snap_to_grid)):
            action.blockSignals(True)
            action.setChecked(bool(value))
            action.blockSignals(False)

    def open_catalog_manager(self):
        dlg = CatalogManagerDialog(self.catalog_tree, self)
        dlg.exec()
        # The dialog reloads catalog_tree.catalog_data as a fresh dict on every
        # save/delete, so re-point the canvas at the current one.
        self.canvas_view.set_catalog_data(self.catalog_tree.catalog_data)

    # ── Visibility Bar ──
    def toggle_show_cables(self, checked):
        self.canvas_view.scene_obj.global_show_cables = checked
        for cable in self.canvas_view.get_cables():
            cable.setVisible(checked)

    def toggle_show_icons(self, checked):
        self.canvas_view.scene_obj.global_show_icons = checked
        self.canvas_view.scene_obj.update()

    def toggle_show_fov(self, checked):
        self.canvas_view.scene_obj.global_show_fov = checked
        self.canvas_view.scene_obj.update()

    def toggle_show_grid(self, checked):
        self.canvas_view.scene_obj.show_grid = checked
        self.canvas_view.scene_obj.update()

    def toggle_snap_to_grid(self, checked):
        self.canvas_view.scene_obj.snap_to_grid = checked

    def on_grid_size_changed(self, value):
        scene = self.canvas_view.scene_obj
        scene.grid_size = value
        scene.grid_size_is_default = False  # user has an explicit opinion now -- stop
                                             # auto-following calibration
        scene.update()

    def _on_calibration_changed(self):
        # Only refresh the displayed number while the grid is still auto-following
        # calibration (grid_size_is_default) -- once the user has set their own
        # spacing via the spinbox, a later re-calibration shouldn't silently override it.
        scene = self.canvas_view.scene_obj
        if scene.grid_size_is_default:
            self.grid_size_spin.blockSignals(True)
            self.grid_size_spin.setValue(scene.effective_grid_size())
            self.grid_size_spin.blockSignals(False)

    def load_default_test_image(self):
        """Dev convenience: auto-loads testimage.jpg from the project root on startup, if present."""
        if getattr(sys, 'frozen', False):
            return
        root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        test_image_path = os.path.join(root_dir, "testimage.jpg")
        if os.path.exists(test_image_path):
            self.canvas_view.load_floorplan(test_image_path)

    # ── Menu / Toolbar File actions ──
    def import_floorplan(self):
        filepath, _ = QFileDialog.getOpenFileName(
            self, "Import Floor Plan Image", "",
            "Images (*.png *.jpg *.jpeg *.bmp *.gif);;All Files (*)"
        )
        if filepath:
            self.canvas_view.load_floorplan(filepath)

    def export_to_image(self):
        if not self.canvas_view.floorplan_item:
            QMessageBox.warning(self, "Export Failed", "There is no layout floorplan active to export.")
            return

        filepath, _ = QFileDialog.getSaveFileName(
            self, "Export Layout to Image", "",
            "PNG Images (*.png);;JPEG Images (*.jpg *.jpeg)"
        )
        if filepath:
            # Render layout scene to image
            rect = self.canvas_view.scene_obj.itemsBoundingRect()
            image = QPixmap(rect.size().toSize())
            image.fill(QColor("#141420"))
            
            painter = QPainter(image)
            # Offset center to align with bounding rect
            self.canvas_view.scene_obj.render(painter, QRectF(image.rect()), rect)
            painter.end()
            
            if image.save(filepath):
                QMessageBox.information(self, "Success", "Layout exported successfully!")
            else:
                QMessageBox.critical(self, "Failed", "Could not save the image file.")

    # ── Save / Load Project ──
    def _remember_last_project_path(self, filepath):
        QSettings().setValue("last_project_path", filepath)

    def _get_last_project_path(self):
        return QSettings().value("last_project_path", None)

    def _make_device_item(self, spec, x, y):
        """Constructs the right DeviceItem subclass for spec's category -- used
        everywhere a device is reconstructed from a saved spec_id, so a loaded patch
        panel/wall drop/access point keeps its correct rendering/behavior instead of
        silently degrading into a generic device."""
        return make_device_item(spec, x, y)

    def prompt_new_project(self):
        project_type = NewProjectDialog.get_project_type(self)
        if project_type is None:
            return  # cancelled
        self.new_project(project_type)

    def new_project(self, project_type="cctv"):
        if len(self.canvas_view.scene_obj.items()) > 0:
            res = QMessageBox.question(self, "New Project", "Discard current layout and create a new project?",
                                       QMessageBox.Yes | QMessageBox.No)
            if res != QMessageBox.Yes:
                return

        self._close_all_rack_tabs()
        self.canvas_view.scene_obj.clear()
        self.canvas_view.floorplan_item = None
        self.canvas_view.scale_ratio = None
        self.canvas_view.scene_obj.scale_ratio = None
        self.current_project_path = None

        self.sidebar.show_empty()
        self.update_scale_label("⚠ Scale: Not Calibrated")
        self.canvas_view.cleanup_temp_shapes()
        self._apply_project_mode(project_type)

        # A new/different project has nothing to do with whatever was undoable in the
        # last one -- undoing "past" a project switch would restore the WRONG project.
        self.undo_manager.undo_stack.clear()
        self.undo_manager.redo_stack.clear()

    def _apply_project_mode(self, project_type):
        """Toggles the handful of CCTV-only UI elements that don't apply to a Network
        Topology project (a lighter mode of the SAME MainWindow/scene/save-format/
        undo-system/catalog/Rack Editor/Network Diagram -- not a separate codepath).
        Called on startup, from new_project(), and from _restore_state() (so it's kept
        in sync across open/save AND undo/redo, since both go through _restore_state)."""
        self.project_type = project_type if project_type in ("cctv", "network_topology") else "cctv"
        is_network = self.project_type == "network_topology"
        self.canvas_view.project_type = self.project_type

        self.act_tool_camera.setVisible(not is_network)
        self.act_tool_camera.setEnabled(not is_network)
        self.act_toggle_fov.setVisible(not is_network)

        if is_network and self.canvas_view.active_tool == "camera":
            self.set_active_tool("select")

        # A camera already on the canvas renders totally differently between modes
        # (see CameraItem.paint) -- force a repaint so switching modes (or loading a
        # project of the other type) is reflected immediately, not just for cameras
        # placed after the switch.
        for cam in self.canvas_view.get_cameras():
            cam.update()

    def save_project(self):
        if not self.current_project_path:
            self.save_project_as()
        else:
            self.write_project_file(self.current_project_path)

    def save_project_as(self):
        filepath, _ = QFileDialog.getSaveFileName(
            self, "Save CCTV Project", "", "Lense Project Files (*.lense)"
        )
        if filepath:
            if not filepath.endswith(".lense"):
                filepath += ".lense"
            self.current_project_path = filepath
            self.write_project_file(filepath)

    def write_project_file(self, filepath):
        try:
            project_data = self._serialize_state()
            with open(filepath, 'w') as f:
                json.dump(project_data, f, indent=4)

            self._remember_last_project_path(filepath)
            self.statusBar().showMessage(f"Project saved to {filepath}", 3000)
        except Exception as e:
            QMessageBox.critical(self, "Save Project Failed", f"Could not save project file: {e}")

    def _seed_floorplan_encoding(self, encoded):
        """Records `encoded` as the current floor plan's PNG, skipping a re-encode."""
        item = self.canvas_view.floorplan_item
        if item is None or not encoded:
            return
        pixmap = item.pixmap()
        if not pixmap.isNull():
            self._floorplan_encoded = (pixmap.cacheKey(), encoded)

    def _encoded_floorplan(self):
        """The floor plan as base64 PNG, encoded once and reused.

        This used to run on every _serialize_state() call -- and a snapshot of that
        state is captured on every mouse press, so a drag could begin only after the
        whole floor plan had been PNG-compressed. On a 54-megapixel architectural scan
        that is over half a second per click, which is exactly what "it takes a second
        to follow the mouse" was.

        Keyed on the pixmap's cacheKey, so importing a different plan re-encodes and
        nothing can go stale. Every undo snapshot then shares one string rather than
        holding its own copy, which also takes the undo stack's memory from fifty
        copies of the image down to one.
        """
        item = self.canvas_view.floorplan_item
        if item is None:
            self._floorplan_encoded = None
            return None
        pixmap = item.pixmap()
        if pixmap.isNull():
            self._floorplan_encoded = None
            return None

        key = pixmap.cacheKey()
        cached = getattr(self, "_floorplan_encoded", None)
        if cached is not None and cached[0] == key:
            return cached[1]

        buffer = QBuffer()
        buffer.open(QIODevice.WriteOnly)
        pixmap.save(buffer, "PNG")
        encoded = base64.b64encode(bytes(buffer.data())).decode("ascii")
        self._floorplan_encoded = (key, encoded)
        return encoded

    def _serialize_state(self):
        """Builds the full project-state dict -- everything write_project_file writes
        to disk, minus the file I/O. Also the snapshot format the undo/redo stack
        uses (see UndoManager) -- same data, just kept in memory instead of on disk."""
        # Serialize project items
        project_data = {
            "version": "1.0",
            "project_type": getattr(self, "project_type", "cctv"),
            "floorplan_path": getattr(self.canvas_view.floorplan_item, "filepath", "") if self.canvas_view.floorplan_item else "",
            # Custom tag inside floorplan item
            "scale_ratio": self.canvas_view.scale_ratio,
            "calibration_unit": self.canvas_view.scene_obj.calibration_unit,
            "calibration_dist": self.canvas_view.scene_obj.calibration_dist,
            "calibration_p1": self.canvas_view.scene_obj.calibration_p1,
            "calibration_p2": self.canvas_view.scene_obj.calibration_p2,
            "cameras": [],
            "devices": [],
            "cables": [],
            "customs": [],
            "labels": [],
            "zones": [],
            "racks": []
        }
            
        # Store filepath on floorplan item if loaded
        if self.canvas_view.floorplan_item and hasattr(self.canvas_view.floorplan_item, "filepath"):
            project_data["floorplan_path"] = self.canvas_view.floorplan_item.filepath

        # Embed the floorplan image itself (not just its local path) so the project
        # file is fully self-contained -- a path like "C:\Users\andrew\..." or
        # "/home/andrew/..." means nothing on whoever's machine opens this file next,
        # whether that's a different OS or just a different person. floorplan_path is
        # still written above too, purely as a human-readable hint of the original
        # filename; it's never relied on for restoring the image when this key exists.
        encoded = self._encoded_floorplan()
        if encoded is not None:
            project_data["floorplan_image_base64"] = encoded

        # Serialize cameras
        for cam in self.canvas_view.get_cameras():
            project_data["cameras"].append({
                "id": cam.id,
                "spec_id": cam.spec["id"],
                "x": cam.x(),
                "y": cam.y(),
                "rotation": cam.rotation_deg,
                "downtilt": cam.downtilt_deg,
                "elevation": cam.elevation,
                "vertical_rise": cam.vertical_rise,
                "service_loop": cam.service_loop,
                "termination": cam.termination,
                "label": cam.label,
                "notes": cam.notes,
                "connected_device_id": cam.connected_device_id,
                "connected_cable_id": cam.connected_cable_id
            })

        # Serialize devices. Loose (top-level scene) devices only -- a device mounted
        # inside a rack is serialized as part of that rack's own entry below instead,
        # since get_network_devices() (which merges both) would otherwise double it up.
        loose_devices = [item for item in self.canvas_view.scene_obj.items()
                          if getattr(item, "object_type", None) == "device"]
        for dev in loose_devices:
            project_data["devices"].append({
                "id": dev.id,
                "spec_id": dev.spec["id"],
                "x": dev.x(),
                "y": dev.y(),
                "label": dev.label,
                "notes": dev.notes,
                "elevation": dev.elevation,
                "vertical_rise": dev.vertical_rise,
                "service_loop": dev.service_loop,
                "termination": dev.termination
            })

        # Serialize racks (and the devices mounted inside each one)
        for rack in self.canvas_view.get_racks():
            project_data["racks"].append({
                "id": rack.id,
                "x": rack.x(),
                "y": rack.y(),
                "label": rack.label,
                "notes": rack.notes,
                "ru_height": rack.ru_height,
                "vertical_rise": rack.vertical_rise,
                "service_loop": rack.service_loop,
                "termination": rack.termination,
                "slots": [{
                    "start_ru": slot.start_ru,
                    "ru_size": slot.ru_size,
                    "device": {
                        "id": slot.device.id,
                        "spec_id": slot.device.spec["id"],
                        "label": slot.device.label,
                        "notes": slot.device.notes,
                        "elevation": slot.device.elevation,
                        "vertical_rise": slot.device.vertical_rise,
                        "service_loop": slot.device.service_loop,
                        "termination": slot.device.termination
                    }
                } for slot in rack.slots]
            })

        # Serialize cables
        for cab in self.canvas_view.get_cables():
            project_data["cables"].append({
                "id": cab.id,
                "label": cab.label,
                "cable_type": cab.cable_type,
                "notes": cab.notes,
                "start_device_id": cab.start_device_id,
                "end_device_id": cab.end_device_id,
                "start_port": cab.start_port,
                "end_port": cab.end_port,
                "vertex_anchors": list(cab.vertex_anchors),
                "points": [(pt.x(), pt.y()) for pt in cab.points]
            })

        # Serialize custom objects
        for custom in self.canvas_view.get_custom_objects():
            project_data["customs"].append({
                "id": custom.id,
                "x": custom.x(),
                "y": custom.y(),
                "label": custom.label,
                "notes": custom.notes,
                "icon_type": custom.icon_type,
                "vertical_rise": custom.vertical_rise,
                "service_loop": custom.service_loop,
                "termination": custom.termination
            })

        # Serialize text labels
        for lbl in self.canvas_view.get_labels():
            project_data["labels"].append({
                "id": lbl.id,
                "x": lbl.x(),
                "y": lbl.y(),
                "text": lbl.text,
                "font_size": lbl.font_size,
                "text_color": lbl.text_color,
                "outline_color": lbl.outline_color,
                "notes": lbl.notes
            })

        # Serialize zone polygons
        for zone in self.canvas_view.get_zones():
            project_data["zones"].append({
                "id": zone.id,
                "label": zone.label,
                "notes": zone.notes,
                "fill_color": zone.fill_color,
                "fill_opacity": zone.fill_opacity,
                "border_color": zone.border_color,
                "points": [(pt.x(), pt.y()) for pt in zone.points],
                "is_box": zone.is_box,
                "label_position": zone.label_position
            })

        return project_data

    def open_project(self):
        filepath, _ = QFileDialog.getOpenFileName(
            self, "Open CCTV Project", "", "Lense Project Files (*.lense)"
        )
        if not filepath:
            return

        try:
            with open(filepath, 'r') as f:
                project_data = json.load(f)
            self.new_project()
            self.current_project_path = filepath
            self._restore_state(project_data)
            self._remember_last_project_path(filepath)
            self.statusBar().showMessage(f"Project loaded from {filepath}", 3000)
        except Exception as e:
            QMessageBox.critical(self, "Load Project Failed", f"Could not load project file: {e}")

    def _restore_state(self, project_data, preserve_view=False, reuse_floorplan=None):
        """Rebuilds the scene from a project-state dict -- shared by opening a .lense
        file (open_project/open_project_filepath) and by the undo/redo stack restoring
        an in-memory snapshot (see UndoManager). Assumes the scene is already empty
        (callers clear it first, e.g. via new_project() or UndoManager itself) and
        that current_project_path/last-project bookkeeping is handled by the caller --
        this only touches the scene and calibration state.

        `preserve_view` keeps the current zoom and scroll position instead of letting
        the floor plan reload re-fit the view -- undo/redo restore the design, not where
        you were looking at it. `reuse_floorplan` is a (base64, pixmap, path) triple
        salvaged before the scene was cleared: when the snapshot carries the very same
        image it is re-hung as-is rather than decoded again, which on a 54-megapixel
        plan is the difference between undo feeling instant and taking half a second.
        """
        view = self.canvas_view
        kept_view = None
        if preserve_view:
            # Scroll-bar values rather than a scene point: centerOn() snaps to whole
            # scroll steps, so round-tripping through it drifts the frame slightly.
            kept_view = (QTransform(view.transform()),
                         view.horizontalScrollBar().value(),
                         view.verticalScrollBar().value(),
                         view.mapToScene(view.viewport().rect().center()))

        self._apply_project_mode(project_data.get("project_type", "cctv"))

        fp_b64 = project_data.get("floorplan_image_base64")
        fp_path = project_data.get("floorplan_path")
        if (fp_b64 and reuse_floorplan is not None and reuse_floorplan[0] == fp_b64
                and not reuse_floorplan[1].isNull()):
            self.canvas_view._apply_floorplan_pixmap(reuse_floorplan[1], reuse_floorplan[2])
            self._seed_floorplan_encoding(fp_b64)
        elif fp_b64:
            # Embedded image data -- always works regardless of what machine/OS/user
            # opens this file, unlike a local path from wherever it was last saved.
            self.canvas_view.load_floorplan_from_bytes(base64.b64decode(fp_b64), fp_path or "")
            # Seed the encode cache with the very string we just decoded, so the first
            # click after opening a project doesn't have to re-compress the image to
            # produce a byte-identical result. See _encoded_floorplan.
            self._seed_floorplan_encoding(fp_b64)
        elif fp_path and os.path.exists(fp_path):
            # Older project file saved before floorplan embedding existed -- fall
            # back to the local-path behavior, best effort.
            self.canvas_view.load_floorplan(fp_path)
            if self.canvas_view.floorplan_item:
                self.canvas_view.floorplan_item.filepath = fp_path

        if kept_view is not None:
            # Back to exactly where the user was looking. Through _zoom_changed so the
            # scene's view_lod (and the bounding rects that depend on it) stay in step.
            view.setTransform(kept_view[0])
            view.horizontalScrollBar().setValue(kept_view[1])
            view.verticalScrollBar().setValue(kept_view[2])
            if view.mapToScene(view.viewport().rect().center()) != kept_view[3]:
                # Scene rect changed under us (the restored state has a different plan),
                # so the old scroll offsets mean something else -- fall back to the point.
                view.centerOn(kept_view[3])
            view._zoom_changed()

        self.canvas_view.scale_ratio = project_data.get("scale_ratio")
        self.canvas_view.scene_obj.scale_ratio = self.canvas_view.scale_ratio
        self.canvas_view.scene_obj.calibration_unit = project_data.get("calibration_unit", "feet")
        self.canvas_view.scene_obj.calibration_dist = project_data.get("calibration_dist", 0.0)
        self.canvas_view.scene_obj.calibration_p1 = project_data.get("calibration_p1")
        self.canvas_view.scene_obj.calibration_p2 = project_data.get("calibration_p2")
        if self.canvas_view.scale_ratio:
            self.update_scale_label(f"Scale: 1 ft = {self.canvas_view.scale_ratio:.1f} px")
        else:
            self.update_scale_label("⚠ Scale: Not Calibrated")

        # Devices (Switches / NVRs)
        for dev_data in project_data.get("devices", []):
            spec_id = dev_data.get("spec_id")
            spec = self.catalog_tree.catalog_data.get(spec_id)
            if spec:
                dev = self._make_device_item(spec, dev_data.get("x"), dev_data.get("y"))
                dev.id = dev_data.get("id")
                dev.label = dev_data.get("label")
                dev.notes = dev_data.get("notes", "")
                dev.elevation = dev_data.get("elevation", 0.0)
                dev.vertical_rise = dev_data.get("vertical_rise")
                dev.service_loop = dev_data.get("service_loop")
                dev.termination = dev_data.get("termination")
                self.canvas_view.scene_obj.addItem(dev)

        # Racks (and the devices mounted inside each one -- those never go through
        # scene_obj.addItem; they're only reachable via rack.slots, same as a
        # freshly-mounted device, see RackItem/CanvasView.get_network_devices)
        for rack_data in project_data.get("racks", []):
            rack = RackItem(rack_data.get("x"), rack_data.get("y"),
                             label=rack_data.get("label", "Rack"),
                             ru_height=rack_data.get("ru_height", 12))
            rack.id = rack_data.get("id")
            rack.notes = rack_data.get("notes", "")
            rack.vertical_rise = rack_data.get("vertical_rise")
            rack.service_loop = rack_data.get("service_loop")
            rack.termination = rack_data.get("termination")
            self.canvas_view.scene_obj.addItem(rack)
            for slot_data in rack_data.get("slots", []):
                dev_data = slot_data.get("device", {})
                spec = self.catalog_tree.catalog_data.get(dev_data.get("spec_id"))
                if not spec:
                    continue
                dev = self._make_device_item(spec, rack.x(), rack.y())
                dev.id = dev_data.get("id")
                dev.label = dev_data.get("label")
                dev.notes = dev_data.get("notes", "")
                dev.elevation = dev_data.get("elevation", 0.0)
                dev.vertical_rise = dev_data.get("vertical_rise")
                dev.service_loop = dev_data.get("service_loop")
                dev.termination = dev_data.get("termination")
                rack.slots.append(RackSlot(dev, slot_data.get("start_ru", 1), slot_data.get("ru_size", 1)))

        # Cameras
        for cam_data in project_data.get("cameras", []):
            spec_id = cam_data.get("spec_id")
            spec = self.catalog_tree.catalog_data.get(spec_id)
            if spec:
                cam = CameraItem(spec, cam_data.get("x"), cam_data.get("y"))
                cam.id = cam_data.get("id")
                cam.label = cam_data.get("label")
                cam.rotation_deg = cam_data.get("rotation", 0.0)
                cam.downtilt_deg = cam_data.get("downtilt", 20.0)
                cam.elevation = cam_data.get("elevation", 10.0)
                cam.notes = cam_data.get("notes", "")
                cam.vertical_rise = cam_data.get("vertical_rise")
                cam.service_loop = cam_data.get("service_loop")
                cam.termination = cam_data.get("termination")
                cam.connected_device_id = cam_data.get("connected_device_id")
                cam.connected_cable_id = cam_data.get("connected_cable_id")
                self.canvas_view.scene_obj.addItem(cam)

        # Cables
        for cab_data in project_data.get("cables", []):
            pts = [QPointF(x, y) for x, y in cab_data.get("points", [])]
            if pts:
                cable = CableItem()
                cable.id = cab_data.get("id")
                cable.label = cab_data.get("label")
                cable.cable_type = cab_data.get("cable_type", "CAT6")
                cable.notes = cab_data.get("notes", "")
                anchors = cab_data.get("vertex_anchors")
                if anchors is None:
                    # Older project files only recorded the two endpoints
                    anchors = [None] * len(pts)
                    anchors[0] = cab_data.get("start_device_id")
                    anchors[-1] = cab_data.get("end_device_id")
                cable.set_points(pts, anchors=anchors)
                # Must be set AFTER set_points -- it clears both ports whenever an end's
                # anchor changes from what it was before, which for a freshly
                # constructed CableItem (anchors start out None) is always true.
                cable.start_port = cab_data.get("start_port")
                cable.end_port = cab_data.get("end_port")
                cable.setVisible(self.canvas_view.scene_obj.global_show_cables)
                self.canvas_view.scene_obj.addItem(cable)

        # Custom Objects
        for custom_data in project_data.get("customs", []):
            custom = CustomItem(custom_data.get("x"), custom_data.get("y"), label=custom_data.get("label", "Custom Object"),
                                 icon_type=custom_data.get("icon_type", "generic"))
            custom.id = custom_data.get("id")
            custom.notes = custom_data.get("notes", "")
            custom.vertical_rise = custom_data.get("vertical_rise")
            custom.service_loop = custom_data.get("service_loop")
            custom.termination = custom_data.get("termination")
            self.canvas_view.scene_obj.addItem(custom)

        # Labels
        for label_data in project_data.get("labels", []):
            lbl = LabelItem(label_data.get("x"), label_data.get("y"), text=label_data.get("text", "Label"))
            lbl.id = label_data.get("id")
            lbl.font_size = label_data.get("font_size", 14)
            lbl.text_color = label_data.get("text_color", "#ffffff")
            lbl.outline_color = label_data.get("outline_color", "#0a0a0f")
            lbl.notes = label_data.get("notes", "")
            self.canvas_view.scene_obj.addItem(lbl)

        # Zones
        for zone_data in project_data.get("zones", []):
            pts = [QPointF(x, y) for x, y in zone_data.get("points", [])]
            if pts:
                zone = ZoneItem(pts, label=zone_data.get("label", "Zone"))
                zone.id = zone_data.get("id")
                zone.notes = zone_data.get("notes", "")
                zone.fill_color = zone_data.get("fill_color", "#3b82f6")
                zone.fill_opacity = zone_data.get("fill_opacity", 0.22)
                zone.border_color = zone_data.get("border_color", "#3b82f6")
                zone.is_box = zone_data.get("is_box", False)
                zone.label_position = zone_data.get("label_position", "center")
                self.canvas_view.scene_obj.addItem(zone)

        recalculate_all_cable_offsets(self.canvas_view.scene_obj)

    def _replace_scene_state(self, state):
        """Used by UndoManager for undo/redo -- clears the scene and rebuilds it from
        an in-memory snapshot. No 'discard changes?' confirmation (this IS the
        mechanism for reverting a change, confirming would be backwards) and no
        current_project_path/QSettings changes (undo/redo doesn't change which file
        you're working on). Closes any open Rack Editor tabs first: restoring
        recreates every object with a fresh identity, so a tab holding onto the OLD
        rack object would otherwise go stale."""
        # Salvage the decoded plan before scene.clear() destroys it, so an undo that
        # doesn't change the image doesn't pay to decode it all over again.
        reuse = None
        item = self.canvas_view.floorplan_item
        cached = getattr(self, "_floorplan_encoded", None)
        if item is not None and cached is not None:
            reuse = (cached[1], item.pixmap(), getattr(item, "filepath", ""))

        self._close_all_rack_tabs()
        self.canvas_view.scene_obj.clear()
        self.canvas_view.floorplan_item = None
        self.canvas_view.scale_ratio = None
        self.canvas_view.scene_obj.scale_ratio = None
        self.canvas_view.cleanup_temp_shapes()
        self.sidebar.show_empty()

        self._restore_state(state, preserve_view=True, reuse_floorplan=reuse)

        self.network_diagram.refresh()
        self.object_explorer.refresh()
        if self.inventory_panel.isVisible():
            self.inventory_panel.refresh()

    # Override floorplan loading via drag and drop in main window too
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            
    def dropEvent(self, event):
        for url in event.mimeData().urls():
            filepath = url.toLocalFile()
            if os.path.exists(filepath):
                # If project file drop
                if filepath.endswith(".lense"):
                    # Open project
                    # Simulate open_project logic with manual path
                    self.open_project_filepath(filepath)
                else: # Otherwise load image as floorplan
                    self.canvas_view.load_floorplan(filepath)
                break
        event.acceptProposedAction()

    def open_project_filepath(self, filepath):
        """Same as open_project() but with a pre-selected path (drag-and-drop, or
        reopening the last project on startup) -- skips the file-picker dialog."""
        try:
            with open(filepath, 'r') as f:
                project_data = json.load(f)
            self.new_project()
            self.current_project_path = filepath
            self._restore_state(project_data)
            self._remember_last_project_path(filepath)
            self.statusBar().showMessage(f"Project loaded from {filepath}", 3000)
        except Exception as e:
            QMessageBox.critical(self, "Load Project Failed", f"Could not load project file: {e}")


if perf.enabled:
    # The properties panel rebuild is the other thing a drag can trigger repeatedly.
    from src.ui.properties_sidebar import PropertiesSidebar as _PS
    _PS.select_item = perf.wrap("properties_rebuild", _PS.select_item)
