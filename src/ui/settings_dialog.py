"""Application settings.

Everything here is a *preference* -- it belongs to the person using Lense, not to the
design they happen to have open -- so it lives in QSettings and follows them from one
project to the next, unlike anything saved into a .lense file.

The dialog applies live as you drag, so the icon scale can be judged against the actual
floor plan instead of guessed at from a number. Cancel restores whatever was in effect
when it opened.
"""

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFormLayout, QFrame, QGridLayout,
                               QGroupBox, QHBoxLayout, QLabel, QPushButton, QScrollArea,
                               QSlider, QSpinBox, QVBoxLayout, QWidget)

from src.core import cable_length, termination, zoom_input
from src.graphics.cable_item import recalculate_all_cable_offsets
from src.graphics import label_render
from src.graphics.icon_scale import (DEFAULT_ICON_SCALE, MAX_ICON_SCALE,
                                     MIN_ICON_SCALE, clamp_icon_scale)

# QSettings keys. Kept in one place so main_window and main.py read the same strings
# the dialog writes -- a typo'd key silently reads back the default forever.
KEY_ICON_SCALE = "canvas/icon_scale"
KEY_LABEL_FONT_SIZE = "canvas/label_font_size"
KEY_SHOW_ICONS = "canvas/show_icons"
KEY_SHOW_FOV = "canvas/show_fov"
KEY_SHOW_CABLES = "canvas/show_cables"
KEY_SHOW_GRID = "canvas/show_grid"
KEY_SNAP_TO_GRID = "canvas/snap_to_grid"
KEY_GRID_SIZE = "canvas/grid_size"
KEY_SPLASH_SOUND = "startup/splash_sound"
KEY_REOPEN_LAST = "startup/reopen_last_project"
KEY_CONFIRM_RACK_DELETE = "behavior/confirm_rack_delete"
KEY_FANOUT_MODE = "canvas/fanout_mode"
KEY_ZOOM_MOUSE = "navigation/zoom_sensitivity_mouse"
KEY_ZOOM_TOUCHPAD = "navigation/zoom_sensitivity_touchpad"
KEY_MULTIDROP_CABLES = "cabling/multidrop_auto_cables"

DEFAULTS = {
    KEY_ICON_SCALE: DEFAULT_ICON_SCALE,
    KEY_LABEL_FONT_SIZE: label_render.DEFAULT_LABEL_FONT_SIZE,
    KEY_SHOW_ICONS: True,
    KEY_SHOW_FOV: True,
    KEY_SHOW_CABLES: True,
    KEY_SHOW_GRID: False,
    KEY_SNAP_TO_GRID: False,
    KEY_GRID_SIZE: 0.0,  # 0 means "follow the calibration" (one real-world foot)
    KEY_SPLASH_SOUND: True,
    KEY_REOPEN_LAST: True,
    KEY_CONFIRM_RACK_DELETE: True,
    KEY_FANOUT_MODE: "latched",
    KEY_ZOOM_MOUSE: zoom_input.DEFAULT_SENSITIVITY,
    KEY_ZOOM_TOUCHPAD: zoom_input.DEFAULT_SENSITIVITY,
    KEY_MULTIDROP_CABLES: True,
}

# QSettings on some backends hands back "true"/"false" strings rather than bools, which
# are both truthy -- every read goes through these so a saved False stays False.
_TRUE_STRINGS = ("true", "1", "yes", "on")


def get_bool(key):
    value = QSettings().value(key, DEFAULTS[key])
    if isinstance(value, str):
        return value.strip().lower() in _TRUE_STRINGS
    return bool(value)


def get_float(key):
    try:
        return float(QSettings().value(key, DEFAULTS[key]))
    except (TypeError, ValueError):
        return float(DEFAULTS[key])


def get_str(key):
    value = QSettings().value(key, DEFAULTS[key])
    return value if isinstance(value, str) and value else DEFAULTS[key]


def get_int(key):
    try:
        return int(float(QSettings().value(key, DEFAULTS[key])))
    except (TypeError, ValueError):
        return int(DEFAULTS[key])


def set_value(key, value):
    QSettings().setValue(key, value)


# Slider works in whole percent; the scale itself is a multiplier.
def _to_percent(scale):
    return int(round(scale * 100))


def _from_percent(percent):
    return clamp_icon_scale(percent / 100.0)


class SettingsDialog(QDialog):
    def __init__(self, main_window):
        super().__init__(main_window)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(460)
        self.main_window = main_window
        self.canvas_view = main_window.canvas_view
        self.scene = self.canvas_view.scene_obj

        # Snapshot for Cancel. Only the live-applied settings need restoring; the rest
        # aren't touched until OK.
        self._original_icon_scale = self.canvas_view.icon_scale()
        self._original_label_size = label_render.label_font_size()
        self._original_fanout = getattr(self.scene, "fanout_mode", "latched")

        # The groups have outgrown a fixed dialog -- on a laptop screen the whole thing
        # ran off the bottom, taking OK and Cancel with it. Everything scrolls, and the
        # buttons sit outside the scroll area so they are always reachable.
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(12, 12, 12, 12)
        content_layout.addWidget(self._build_canvas_group())
        content_layout.addWidget(self._build_navigation_group())
        content_layout.addWidget(self._build_cabling_group())
        content_layout.addWidget(self._build_visibility_group())
        content_layout.addWidget(self._build_startup_group())
        content_layout.addWidget(self._build_behavior_group())
        content_layout.addStretch()

        scroll = QScrollArea()
        scroll.setWidget(content)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        # Vertical only: the groups are laid out to fit the width, and a horizontal bar
        # would just be a sign something is mis-sized.
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel
                                   | QDialogButtonBox.RestoreDefaults)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.RestoreDefaults).clicked.connect(self.restore_defaults)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(scroll, 1)

        footer = QWidget()
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(12, 8, 12, 12)
        footer_layout.addWidget(buttons)
        layout.addWidget(footer)

        self._size_to_content(content, footer)

    def _size_to_content(self, content, footer):
        """Open at the natural height, but never taller than the screen it opens on.

        Sized rather than left to Qt so the dialog is only as tall as it needs to be --
        it scrolls when it must, and doesn't when it doesn't.
        """
        wanted = content.sizeHint().height() + footer.sizeHint().height() + 24
        screen = self.screen()
        available = screen.availableGeometry().height() if screen else 900
        self.resize(max(self.minimumWidth(), 540), min(wanted, int(available * 0.85)))

    # ── Groups ──
    def _build_canvas_group(self):
        group = QGroupBox("Canvas")
        form = QFormLayout(group)

        self.icon_slider = QSlider(Qt.Horizontal)
        self.icon_slider.setRange(_to_percent(MIN_ICON_SCALE), _to_percent(MAX_ICON_SCALE))
        self.icon_slider.setValue(_to_percent(self._original_icon_scale))
        self.icon_slider.setTickPosition(QSlider.TicksBelow)
        self.icon_slider.setTickInterval(100)
        self.icon_slider.valueChanged.connect(self._on_icon_slider)

        self.icon_readout = QLabel()
        self.icon_readout.setMinimumWidth(48)
        self.icon_readout.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        fit_btn = QPushButton("Fit to Floor Plan")
        fit_btn.setToolTip("Pick a scale that sizes equipment icons like real objects "
                           "on the plan (about 2.5 ft across) once it's calibrated, or "
                           "off the image's resolution if it isn't.")
        fit_btn.clicked.connect(self._on_fit_to_floorplan)

        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.addWidget(self.icon_slider, 1)
        row_layout.addWidget(self.icon_readout)
        row_layout.addWidget(fit_btn)
        form.addRow("Icon size:", row)

        hint = QLabel("Equipment icons are a fixed size on the canvas, so they look "
                      "tiny over a high-resolution floor plan. This scales them "
                      "without touching camera coverage, which stays in real feet.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #8888a0; font-size: 11px;")
        form.addRow("", hint)

        self.fanout_combo = QComboBox()
        self.fanout_combo.addItem("Latched \u2014 hold one bundle open", "latched")
        self.fanout_combo.addItem("Dynamic \u2014 spread with cursor distance", "dynamic")
        self.fanout_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        current = get_str(KEY_FANOUT_MODE)
        self.fanout_combo.setCurrentIndex(max(0, self.fanout_combo.findData(current)))
        self.fanout_combo.setToolTip(
            "How a bundle of cables opens when you hover it.\n"
            "Latched holds it open so you can read it without chasing it; "
            "dynamic is the original proportional feel.")
        self.fanout_combo.currentIndexChanged.connect(self._on_fanout_mode)
        form.addRow("Bundle fan-out:", self.fanout_combo)

        self.label_size_spin = QSpinBox()
        self.label_size_spin.setRange(6, 20)
        self.label_size_spin.setSuffix(" pt")
        self.label_size_spin.setValue(self._original_label_size)
        self.label_size_spin.valueChanged.connect(self._on_label_size)
        form.addRow("Label text size:", self.label_size_spin)

        self._update_icon_readout()
        return group

    def _build_navigation_group(self):
        group = QGroupBox("Navigation")
        form = QFormLayout(group)

        hint = QLabel("Zoom speed for each kind of device. They're separate so a touchpad "
                      "can be slowed down without changing a mouse wheel that already "
                      "feels right.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #8888a0; font-size: 11px;")
        form.addRow("", hint)

        row, self.zoom_mouse_slider, _ = self._sensitivity_row(get_float(KEY_ZOOM_MOUSE))
        form.addRow("Mouse wheel zoom:", row)
        row, self.zoom_touchpad_slider, _ = self._sensitivity_row(get_float(KEY_ZOOM_TOUCHPAD))
        form.addRow("Touchpad zoom:", row)
        return group

    def _sensitivity_row(self, value):
        """A percentage slider with a live readout, for a zoom sensitivity multiplier."""
        slider = QSlider(Qt.Horizontal)
        slider.setRange(int(zoom_input.MIN_SENSITIVITY * 100),
                        int(zoom_input.MAX_SENSITIVITY * 100))
        slider.setTickPosition(QSlider.TicksBelow)
        slider.setTickInterval(50)
        slider.setValue(int(round(zoom_input.clamp_sensitivity(value) * 100)))

        readout = QLabel(f"{slider.value()}%")
        readout.setMinimumWidth(48)
        readout.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        slider.valueChanged.connect(lambda v, r=readout: r.setText(f"{v}%"))

        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(slider, 1)
        layout.addWidget(readout)
        return row, slider, readout

    def _build_cabling_group(self):
        group = QGroupBox("Cable allowances")
        outer = QVBoxLayout(group)

        hint = QLabel("A floor plan only measures the horizontal run. These add the "
                      "cable spent getting up to the pathway and back down at each end, "
                      "plus slack coiled there \u2014 so a takeoff reflects what actually "
                      "gets pulled. Termination decides the hardware each connection "
                      "needs: a crimped plug, or a jack plus a patch cable. Objects "
                      "follow these unless you set one directly in its properties.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #8888a0; font-size: 11px;")
        outer.addWidget(hint)

        grid = QGridLayout()
        grid.addWidget(QLabel(""), 0, 0)
        for column, heading in ((1, "Vertical rise"), (2, "Service loop"),
                                (3, "Termination")):
            head = QLabel(heading)
            head.setStyleSheet("color: #8888a0; font-weight: bold; font-size: 11px;")
            grid.addWidget(head, 0, column)

        self.cabling_spins = {}
        self.termination_combos = {}
        for row, (category, (label, _rise, _loop)) in enumerate(
                cable_length.CATEGORY_DEFAULTS.items(), start=1):
            grid.addWidget(QLabel(label + ":"), row, 0)
            spins = []
            for column, getter in ((1, cable_length.default_rise),
                                   (2, cable_length.default_loop)):
                spin = QDoubleSpinBox()
                spin.setRange(0.0, 500.0)
                spin.setDecimals(1)
                spin.setSingleStep(1.0)
                spin.setSuffix(" ft")
                spin.setValue(getter(category))
                grid.addWidget(spin, row, column)
                spins.append(spin)
            self.cabling_spins[category] = tuple(spins)

            combo = QComboBox()
            for method in termination.METHODS:
                combo.addItem(termination.METHOD_LABELS[method], method)
            combo.setCurrentIndex(
                max(0, combo.findData(termination.default_method(category))))
            grid.addWidget(combo, row, 3)
            self.termination_combos[category] = combo
        grid.setColumnStretch(4, 1)
        outer.addLayout(grid)
        return group

    def _build_visibility_group(self):
        group = QGroupBox("Defaults for new sessions")
        form = QFormLayout(group)

        self.show_icons_chk = QCheckBox("Show equipment icons")
        self.show_icons_chk.setChecked(get_bool(KEY_SHOW_ICONS))
        self.show_fov_chk = QCheckBox("Show camera coverage (FOV)")
        self.show_fov_chk.setChecked(get_bool(KEY_SHOW_FOV))
        self.show_cables_chk = QCheckBox("Show cables")
        self.show_cables_chk.setChecked(get_bool(KEY_SHOW_CABLES))
        self.show_grid_chk = QCheckBox("Show grid")
        self.show_grid_chk.setChecked(get_bool(KEY_SHOW_GRID))
        self.snap_grid_chk = QCheckBox("Snap to grid")
        self.snap_grid_chk.setChecked(get_bool(KEY_SNAP_TO_GRID))
        for widget in (self.show_icons_chk, self.show_fov_chk, self.show_cables_chk,
                       self.show_grid_chk, self.snap_grid_chk):
            form.addRow("", widget)

        self.grid_size_spin = QDoubleSpinBox()
        self.grid_size_spin.setRange(0.0, 2000.0)
        self.grid_size_spin.setSingleStep(5.0)
        self.grid_size_spin.setDecimals(1)
        self.grid_size_spin.setSpecialValueText("Follow calibration (1 ft)")
        self.grid_size_spin.setValue(get_float(KEY_GRID_SIZE))
        self.grid_size_spin.setToolTip("Grid spacing in scene units. Zero tracks the "
                                       "project's calibration instead, so one square "
                                       "is one real-world foot.")
        form.addRow("Grid spacing:", self.grid_size_spin)
        return group

    def _build_startup_group(self):
        group = QGroupBox("Startup")
        form = QFormLayout(group)
        self.splash_sound_chk = QCheckBox("Play the splash chime")
        self.splash_sound_chk.setChecked(get_bool(KEY_SPLASH_SOUND))
        form.addRow("", self.splash_sound_chk)

        self.reopen_last_chk = QCheckBox("Reopen the last project on launch")
        self.reopen_last_chk.setChecked(get_bool(KEY_REOPEN_LAST))
        form.addRow("", self.reopen_last_chk)
        return group

    def _build_behavior_group(self):
        group = QGroupBox("Behavior")
        form = QFormLayout(group)
        self.confirm_rack_chk = QCheckBox("Confirm before deleting a rack with equipment in it")
        self.confirm_rack_chk.setChecked(get_bool(KEY_CONFIRM_RACK_DELETE))
        form.addRow("", self.confirm_rack_chk)

        self.multidrop_chk = QCheckBox("Pull one cable per outlet on multi-gang wall drops")
        self.multidrop_chk.setChecked(get_bool(KEY_MULTIDROP_CABLES))
        self.multidrop_chk.setToolTip(
            "Drawing a run from the MDF to a 6-port plate creates six cables at once, "
            "one per outlet.\nTurn this off to draw them one at a time.")
        form.addRow("", self.multidrop_chk)
        return group

    # ── Live preview ──
    def _update_icon_readout(self):
        self.icon_readout.setText(f"{self.icon_slider.value()}%")

    def _on_icon_slider(self, percent):
        self._update_icon_readout()
        self.canvas_view.set_icon_scale(_from_percent(percent))

    def _on_fanout_mode(self, _index):
        self._apply_fanout(self.fanout_combo.currentData())

    def _apply_fanout(self, mode):
        self.scene.fanout_mode = mode
        self.scene.open_bundle = None
        recalculate_all_cable_offsets(self.scene)

    def _on_label_size(self, size):
        # Through the canvas, so the scene index is told before the label extents (and
        # therefore every item's bounding rect) change underneath it.
        self.canvas_view.set_label_font_size(size)

    def _on_fit_to_floorplan(self):
        suggested = self.canvas_view.suggested_icon_scale()
        self.icon_slider.setValue(_to_percent(suggested))

    def restore_defaults(self):
        self.icon_slider.setValue(_to_percent(DEFAULTS[KEY_ICON_SCALE]))
        self.label_size_spin.setValue(DEFAULTS[KEY_LABEL_FONT_SIZE])
        self.show_icons_chk.setChecked(DEFAULTS[KEY_SHOW_ICONS])
        self.show_fov_chk.setChecked(DEFAULTS[KEY_SHOW_FOV])
        self.show_cables_chk.setChecked(DEFAULTS[KEY_SHOW_CABLES])
        self.show_grid_chk.setChecked(DEFAULTS[KEY_SHOW_GRID])
        self.snap_grid_chk.setChecked(DEFAULTS[KEY_SNAP_TO_GRID])
        self.grid_size_spin.setValue(DEFAULTS[KEY_GRID_SIZE])
        self.splash_sound_chk.setChecked(DEFAULTS[KEY_SPLASH_SOUND])
        self.reopen_last_chk.setChecked(DEFAULTS[KEY_REOPEN_LAST])
        self.confirm_rack_chk.setChecked(DEFAULTS[KEY_CONFIRM_RACK_DELETE])
        self.multidrop_chk.setChecked(DEFAULTS[KEY_MULTIDROP_CABLES])
        self.fanout_combo.setCurrentIndex(
            max(0, self.fanout_combo.findData(DEFAULTS[KEY_FANOUT_MODE])))
        self.zoom_mouse_slider.setValue(int(DEFAULTS[KEY_ZOOM_MOUSE] * 100))
        self.zoom_touchpad_slider.setValue(int(DEFAULTS[KEY_ZOOM_TOUCHPAD] * 100))
        cable_length.clear_defaults()
        termination.clear_defaults()
        for category, (rise_spin, loop_spin) in self.cabling_spins.items():
            rise_spin.setValue(cable_length.default_rise(category))
            loop_spin.setValue(cable_length.default_loop(category))
        for category, combo in self.termination_combos.items():
            combo.setCurrentIndex(
                max(0, combo.findData(termination.default_method(category))))

    # ── Commit / revert ──
    def accept(self):
        set_value(KEY_ICON_SCALE, _from_percent(self.icon_slider.value()))
        set_value(KEY_LABEL_FONT_SIZE, self.label_size_spin.value())
        set_value(KEY_SHOW_ICONS, self.show_icons_chk.isChecked())
        set_value(KEY_SHOW_FOV, self.show_fov_chk.isChecked())
        set_value(KEY_SHOW_CABLES, self.show_cables_chk.isChecked())
        set_value(KEY_SHOW_GRID, self.show_grid_chk.isChecked())
        set_value(KEY_SNAP_TO_GRID, self.snap_grid_chk.isChecked())
        set_value(KEY_GRID_SIZE, self.grid_size_spin.value())
        set_value(KEY_SPLASH_SOUND, self.splash_sound_chk.isChecked())
        set_value(KEY_REOPEN_LAST, self.reopen_last_chk.isChecked())
        set_value(KEY_CONFIRM_RACK_DELETE, self.confirm_rack_chk.isChecked())
        set_value(KEY_MULTIDROP_CABLES, self.multidrop_chk.isChecked())
        self.canvas_view.multidrop_auto_cables = self.multidrop_chk.isChecked()
        set_value(KEY_FANOUT_MODE, self.fanout_combo.currentData())
        mouse_zoom = self.zoom_mouse_slider.value() / 100.0
        touchpad_zoom = self.zoom_touchpad_slider.value() / 100.0
        set_value(KEY_ZOOM_MOUSE, mouse_zoom)
        set_value(KEY_ZOOM_TOUCHPAD, touchpad_zoom)
        self.canvas_view.zoom_sensitivity_mouse = mouse_zoom
        self.canvas_view.zoom_sensitivity_touchpad = touchpad_zoom
        for category, (rise_spin, loop_spin) in self.cabling_spins.items():
            cable_length.set_defaults(category, rise_spin.value(), loop_spin.value())
        for category, combo in self.termination_combos.items():
            termination.set_default(category, combo.currentData())
        # Lengths shift for every object still following a default, so anything showing
        # footage has to be recomputed.
        self.canvas_view.refresh_connectivity_badges()
        self.main_window.inventory_panel.refresh()
        # The visibility/grid checkboxes are stored as the defaults for a new session
        # rather than forced onto the open one -- the Visibility bar is right there,
        # and yanking the current view out from under someone who came here to resize
        # icons would be its own kind of rude.
        super().accept()

    def reject(self):
        self.canvas_view.set_icon_scale(self._original_icon_scale)
        self.canvas_view.set_label_font_size(self._original_label_size)
        self._apply_fanout(self._original_fanout)
        super().reject()


def apply_saved_settings(main_window):
    """Applies the stored preferences to a freshly-built MainWindow."""
    canvas_view = main_window.canvas_view
    scene = canvas_view.scene_obj

    canvas_view.set_icon_scale(get_float(KEY_ICON_SCALE))
    canvas_view.set_label_font_size(get_int(KEY_LABEL_FONT_SIZE))

    scene.global_show_icons = get_bool(KEY_SHOW_ICONS)
    scene.global_show_fov = get_bool(KEY_SHOW_FOV)
    scene.global_show_cables = get_bool(KEY_SHOW_CABLES)
    scene.show_grid = get_bool(KEY_SHOW_GRID)
    scene.snap_to_grid = get_bool(KEY_SNAP_TO_GRID)
    scene.fanout_mode = get_str(KEY_FANOUT_MODE)
    canvas_view.zoom_sensitivity_mouse = zoom_input.clamp_sensitivity(get_float(KEY_ZOOM_MOUSE))
    canvas_view.zoom_sensitivity_touchpad = zoom_input.clamp_sensitivity(get_float(KEY_ZOOM_TOUCHPAD))
    canvas_view.multidrop_auto_cables = get_bool(KEY_MULTIDROP_CABLES)

    grid_size = get_float(KEY_GRID_SIZE)
    if grid_size > 0:
        scene.grid_size = grid_size
        scene.grid_size_is_default = False

    scene.update()
