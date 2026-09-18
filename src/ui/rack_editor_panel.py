from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QLineEdit,
                             QSpinBox, QPushButton, QComboBox, QMessageBox, QGroupBox, QGraphicsView)
from PySide6.QtCore import Signal, QTimer
from PySide6.QtGui import QFont

from src.graphics.device_factory import make_device_item
from src.ui.rack_elevation_view import RackElevationView


class RackEditorPanel(QWidget):
    """Front-elevation editor for a single rack, opened as its own sheet tab (like
    Canvas/Network Diagram) rather than a modal dialog -- a modal window can't receive
    drags from the Equipment Catalog dock, which lives in the same main window, so
    mounting a device by dragging it in would never actually reach the rack. Living as
    a tab keeps the catalog reachable the whole time.

    Rename it, resize its RU height, drag switches/NVRs/patch panels in from the
    catalog (or reorder ones already mounted), and route the field cables that arrive
    here onto specific ports/devices -- one at a time or as a rubber-band-selected
    group -- or lay a short patch cable between two ports/devices to complete a
    connection through to a switch."""

    renamed = Signal(str)
    # Emitted when a mounted device is selected in the elevation, so the Properties
    # panel can show THAT device (ports, PoE, notes) instead of the rack it sits in.
    device_selected = Signal(object)

    def __init__(self, rack, canvas_view, parent=None):
        super().__init__(parent)
        self.rack = rack
        # Set while a rebuild is queued, so overlapping requests coalesce into one
        # and a rebuild deferred past an in-flight drag is never lost.
        self._refresh_pending = False
        self.canvas_view = canvas_view
        self.tool_mode = "select"  # "select" or "patch"
        self._patch_pending = None  # (device_id, port_or_None) -- first endpoint clicked, awaiting the second
        # Held-key "peek under the cables" -- see RackElevationView's H key handling.
        # Same visual/click-through treatment as actively drawing a new patch cable,
        # but under direct manual control instead of only kicking in mid-draw.
        self.ghost_mode = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)

        header_group = QGroupBox("Rack")
        header_form = QFormLayout(header_group)

        self.name_edit = QLineEdit(rack.label)
        self.name_edit.textChanged.connect(self._on_name_changed)
        header_form.addRow("Name:", self.name_edit)

        id_lbl = QLabel(rack.id)
        id_lbl.setStyleSheet("color: #555568; font-size: 10px;")
        id_lbl.setFont(QFont("monospace"))
        header_form.addRow("Internal ID:", id_lbl)

        self.height_spin = QSpinBox()
        self.height_spin.setRange(1, 48)
        self.height_spin.setSuffix(" RU")
        self.height_spin.setValue(rack.ru_height)
        self.height_spin.valueChanged.connect(self._on_height_changed)
        header_form.addRow("Rack Height:", self.height_spin)

        layout.addWidget(header_group)

        add_row = QHBoxLayout()
        self.existing_combo = QComboBox()
        add_row.addWidget(self.existing_combo, 1)
        add_existing_btn = QPushButton("Add Existing Device to Rack")
        add_existing_btn.clicked.connect(self._add_existing_device)
        add_row.addWidget(add_existing_btn)
        layout.addLayout(add_row)

        toolbar = QHBoxLayout()
        self.select_btn = QPushButton("Select / Move")
        self.select_btn.setCheckable(True)
        self.select_btn.setChecked(True)
        self.select_btn.clicked.connect(lambda: self._set_tool_mode("select"))
        toolbar.addWidget(self.select_btn)

        self.patch_btn = QPushButton("Draw Patch Cable")
        self.patch_btn.setCheckable(True)
        self.patch_btn.clicked.connect(lambda: self._set_tool_mode("patch"))
        toolbar.addWidget(self.patch_btn)

        self.hint_lbl = QLabel()
        self.hint_lbl.setStyleSheet("color: #8888a0; font-size: 11px;")
        toolbar.addWidget(self.hint_lbl, 1)
        layout.addLayout(toolbar)
        self._update_hint()

        # Legend for the port-grid tints -- the colours encode which bank a port
        # belongs to, which is what decides whether a downstream PoE-powered switch
        # gets its full budget or a fraction of it.
        legend = QHBoxLayout()
        legend.setSpacing(12)
        for colour, text in (("#22c55e", "in use"), ("#7c4a03", "PoE++"),
                             ("#1e3a5f", "PoE+"), ("#134e4a", "PoE"),
                             ("#2e2e3f", "data only")):
            swatch = QLabel("\u25a0 " + text)
            swatch.setStyleSheet(f"color: {colour}; font-size: 11px; font-weight: bold;")
            legend.addWidget(swatch)
        legend.addStretch()
        layout.addLayout(legend)

        self.elevation_view = RackElevationView(self)
        layout.addWidget(self.elevation_view, 1)

        self.refresh()

    # ── Header handlers ──
    def _on_name_changed(self, text):
        self.rack.label = text or "Rack"
        self.rack.update()
        self.renamed.emit(self.rack.label)

    def _on_height_changed(self, value):
        top_used = max((s.start_ru + s.ru_size - 1 for s in self.rack.slots), default=0)
        if value < top_used:
            QMessageBox.warning(self, "Rack Too Short",
                                 f"RU {top_used} is occupied -- remove or move that device before "
                                 f"shrinking the rack below {top_used} RU.")
            self.height_spin.blockSignals(True)
            self.height_spin.setValue(self.rack.ru_height)
            self.height_spin.blockSignals(False)
            return
        self.rack.ru_height = value
        self.rack.update()
        self.refresh()

    # ── Tool mode ──
    def _set_tool_mode(self, mode):
        self.tool_mode = mode
        self._patch_pending = None
        self.select_btn.setChecked(mode == "select")
        self.patch_btn.setChecked(mode == "patch")
        self._update_hint()
        self.elevation_view.setDragMode(
            QGraphicsView.RubberBandDrag if mode == "select" else QGraphicsView.NoDrag)
        # Existing patch cables' opacity/click-through depends on tool_mode -- force
        # an immediate repaint rather than waiting for some unrelated event to do it.
        self.elevation_view.scene_obj.update()

    def _update_hint(self):
        if self.tool_mode == "select":
            self.hint_lbl.setText("Drag equipment to reorder. Drag a cable stub (or a rubber-band-selected "
                                   "group) onto a port/device to terminate it there.")
        else:
            self.hint_lbl.setText("Click a port or device, then click a second one, to lay a patch cable between them.")

    # ── Mount / resize / unmount ──
    def mount_new_device(self, spec_id, preferred_start_ru=None):
        spec = self.canvas_view.catalog_data.get(spec_id)
        if not spec or spec.get("category") not in ("switch", "nvr", "patch-panel"):
            QMessageBox.warning(self, "Can't Mount", "Only switches, NVRs, and patch panels can be mounted in a rack.")
            return
        device = make_device_item(spec, self.rack.pos().x(), self.rack.pos().y())
        device.label = self.canvas_view.next_device_label(spec)
        ru_size = int(spec.get("ruSize", 1) or 1)
        self.canvas_view.push_undo_snapshot()
        ok = self.rack.mount(device, ru_size=ru_size, start_ru=preferred_start_ru
                              if preferred_start_ru is not None and self.rack.can_fit(ru_size, preferred_start_ru) else None)
        if not ok:
            QMessageBox.warning(self, "Rack Full", "Not enough contiguous RU space left in this rack.")
            return
        self.canvas_view.refresh_connectivity_badges()
        self.refresh()

    def resize_slot(self, slot, new_size):
        if new_size == slot.ru_size:
            return
        self.canvas_view.push_undo_snapshot()
        self.rack.slots.remove(slot)
        if self.rack.can_fit(new_size, slot.start_ru):
            slot.ru_size = new_size
            self.rack.slots.append(slot)
        else:
            new_start = self.rack.next_fit(new_size)
            if new_start is None:
                self.rack.slots.append(slot)
                QMessageBox.warning(self, "No Room", "Not enough contiguous RU space to grow this device.")
            else:
                slot.ru_size = new_size
                slot.start_ru = new_start
                self.rack.slots.append(slot)
        self.rack.update()
        self.refresh()

    def move_slot_to(self, slot, new_start_ru):
        """Drag-to-reorder: called by EquipmentBlockItem after a reorder drag."""
        if new_start_ru == slot.start_ru:
            self.refresh()
            return
        self.canvas_view.push_undo_snapshot()
        self.rack.slots.remove(slot)
        if self.rack.can_fit(slot.ru_size, new_start_ru):
            slot.start_ru = new_start_ru
            self.rack.slots.append(slot)
        else:
            self.rack.slots.append(slot)
            QMessageBox.warning(self, "No Room", "Another device already occupies that space.")
        self.rack.update()
        self.refresh()

    def unmount_device(self, slot):
        self.canvas_view.push_undo_snapshot()
        device = self.rack.unmount(slot.device.id)
        if device is None:
            return
        device.setPos(self.rack.pos())
        self.canvas_view.scene_obj.addItem(device)
        self.canvas_view.scene_obj.clearSelection()
        device.setSelected(True)
        self.rack.update()
        self.canvas_view.refresh_connectivity_badges()
        self.refresh()

    def delete_devices(self, slots, confirm=True):
        """Permanently deletes one or more mounted devices (right-click menu, or
        Backspace/Delete with them selected) -- NOT the same as unmount_device(), which
        pops a device back out onto the floor plan still intact. Confirmed by default
        since it's destructive and, unlike deleting a device from the main Canvas,
        there's no separate confirmation dialog upstream of this one to rely on. Takes
        a list (even for a single device) so a rubber-band multi-selection deletes as
        one undoable action instead of one entry per device."""
        if not slots:
            return
        if confirm:
            if len(slots) == 1:
                device = slots[0].device
                msg = f"Permanently delete {device.label} ({device.spec.get('model', '')}) from this rack?"
            else:
                names = ", ".join(s.device.label for s in slots)
                msg = f"Permanently delete {len(slots)} devices ({names}) from this rack?"
            res = QMessageBox.question(self, "Delete Device", msg + " This cannot be undone except with Ctrl+Z.",
                                        QMessageBox.Yes | QMessageBox.No)
            if res != QMessageBox.Yes:
                return
        self.canvas_view.push_undo_snapshot()

        from src.graphics.cable_item import recalculate_all_cable_offsets
        device_ids = {s.device.id for s in slots}

        # Field cables land here from OUTSIDE the rack -- deleting the device doesn't
        # make that physical run disappear, so return it to the rack's unrouted
        # pitchfork instead of deleting it too (same as backspacing a landed stub).
        for cable in self.canvas_view.get_cables():
            if cable.cable_type == "Patch":
                continue
            anchors = list(cable.vertex_anchors)
            changed = False
            if anchors and anchors[0] in device_ids:
                anchors[0] = self.rack.id
                cable.start_port = None
                changed = True
            if anchors and anchors[-1] in device_ids:
                anchors[-1] = self.rack.id
                cable.end_port = None
                changed = True
            if changed:
                cable.set_points(list(cable.points), anchors=anchors)

        # Patch cables are purely internal jumpers with no floor-plan fallback the way
        # field cables have -- one end about to not exist means the whole cable goes.
        for cable in list(self.canvas_view.get_cables()):
            if cable.cable_type == "Patch" and (cable.start_device_id in device_ids or cable.end_device_id in device_ids):
                if cable.scene() is not None:
                    self.canvas_view.scene_obj.removeItem(cable)

        for cam in self.canvas_view.get_cameras():
            if cam.connected_device_id in device_ids:
                cam.connected_device_id = None

        for slot in slots:
            device = slot.device
            self.rack.unmount(device.id)
            if device.scene() is not None:
                self.canvas_view.scene_obj.removeItem(device)

        recalculate_all_cable_offsets(self.canvas_view.scene_obj)
        self.canvas_view.refresh_connectivity_badges()
        self.refresh_soon()

    def return_stubs_to_pitchfork(self, stubs):
        """Un-terminates one or more landed field cable ends, sending them back to the
        unrouted pitchfork column so they can be dropped on a different port/device --
        the only way to MOVE a cable once it's landed somewhere, short of deleting and
        re-routing it from scratch. Takes a list so a multi-selection returns as one
        undoable action."""
        terminated = [s for s in stubs if s.terminated]
        if not terminated:
            return
        self.canvas_view.push_undo_snapshot()
        for stub in terminated:
            cable = stub.cable
            anchors = list(cable.vertex_anchors)
            anchors[stub.end_index] = self.rack.id
            cable.set_points(list(cable.points), anchors=anchors)
            if stub.end_index == 0:
                cable.start_port = None
            else:
                cable.end_port = None
        self.canvas_view.refresh_connectivity_badges()
        self.refresh_soon()

    def _add_existing_device(self):
        device_id = self.existing_combo.currentData()
        if not device_id:
            return
        device = self.canvas_view.find_device_or_camera_by_id(device_id)
        if device is None:
            return
        if not self.rack.can_fit(1):
            QMessageBox.warning(self, "Rack Full", "Not enough contiguous RU space left in this rack.")
            return
        self.canvas_view.push_undo_snapshot()
        self.canvas_view.scene_obj.removeItem(device)
        self.rack.mount(device, ru_size=1)
        self.rack.update()
        self.canvas_view.refresh_connectivity_badges()
        self.refresh()

    # ── Cable routing (field cable stubs) ──
    def cables_terminating_here(self):
        """(cable, end_index) for every non-patch cable with an end anchored either to
        this rack directly (unrouted) or to a device/patch-panel mounted inside it
        (routed) -- what the elevation draws as incoming stubs.

        A cable whose OTHER end is also a rack is excluded -- there's no supported way
        to represent an inter-rack trunk in this view yet, and one showing up here
        (labeled with the other rack's name) is almost always an accident: dragging a
        rack across the floor plan auto-adopts any dangling cable end it passes over
        (see CanvasView.reattach_free_vertices_near), which can vacuum up a free end
        that was never meant to attach to it. Filtered out here, not deleted -- it's
        still a real cable, findable/fixable from the floor plan."""
        mounted_ids = {s.device.id for s in self.rack.slots}
        racks = self.canvas_view.get_racks()
        rack_ids = {r.id for r in racks}
        result = []
        for cable in self.canvas_view.get_cables():
            if cable.cable_type == "Patch":
                continue
            anchors = cable.vertex_anchors
            if not anchors:
                continue
            if anchors[0] == self.rack.id or anchors[0] in mounted_ids:
                if anchors[-1] not in rack_ids:
                    result.append((cable, 0))
            if anchors[-1] == self.rack.id or anchors[-1] in mounted_ids:
                if anchors[0] not in rack_ids:
                    result.append((cable, -1))
        return result

    def stub_label(self, cable, end_index):
        other_index = -1 if end_index == 0 else 0
        other_id = cable.vertex_anchors[other_index] if cable.vertex_anchors else None
        other_item = self.canvas_view.find_device_or_camera_by_id(other_id) if other_id else None
        if other_item is not None:
            return getattr(other_item, "label", cable.label)
        return cable.label

    def patch_cables_here(self):
        mounted_ids = {s.device.id for s in self.rack.slots}
        result = []
        for cable in self.canvas_view.get_cables():
            if cable.cable_type != "Patch":
                continue
            anchors = cable.vertex_anchors
            if not anchors or len(anchors) < 2:
                continue
            a_id, b_id = anchors[0], anchors[-1]
            if a_id in mounted_ids or b_id in mounted_ids:
                result.append((cable, a_id, cable.start_port, b_id, cable.end_port))
        return result

    def delete_patch_cables(self, patch_cable_items):
        """Removes the underlying CableItem for each selected PatchCableItem. Patch
        cables are real cables (see handle_patch_click), so this is the same kind of
        deletion delete_selected() does on the main canvas -- just triggered here
        since patch cables aren't visible/selectable on the floor plan at all."""
        if not patch_cable_items:
            return
        self.canvas_view.push_undo_snapshot()
        for item in patch_cable_items:
            cable = item.cable
            if cable.scene() is not None:
                self.canvas_view.scene_obj.removeItem(cable)
        self.canvas_view.refresh_connectivity_badges()
        self.refresh_soon()

    def handle_stub_drop(self, stubs, target_block, drop_scene_pos):
        if target_block is None:
            self.refresh_soon()  # nothing under the cursor -- snap back to the unrouted fan
            return

        device = target_block.slot.device
        self.canvas_view.push_undo_snapshot()
        if target_block.has_ports():
            local_pt = target_block.mapFromScene(drop_scene_pos)
            precise_port = target_block.port_at(local_pt)
            any_rejected = False
            occupied_target = None
            for stub in stubs:
                port = precise_port if (len(stubs) == 1 and precise_port is not None) else None
                # A stub being re-seated must not collide with its OWN current
                # termination: without ignoring it, dropping a cable back on the port
                # it already occupies read as "occupied" and silently flung it to some
                # unrelated free port instead.
                if port is not None and not device.port_free_for_new_cable(
                        port, self.canvas_view, stub.cable.cable_type, ignore_cable=stub.cable):
                    # Deliberately aimed at a port that's genuinely taken by something
                    # else. Say so rather than quietly landing it somewhere the user
                    # didn't pick -- silently relocating is what made this feel random.
                    occupied_target = port
                    continue
                if port is None:
                    # Auto-assign (bulk drop, or an imprecise single one): a Fiber
                    # cable belongs on an SFP port, not squeezed onto an RJ45 one.
                    port = device.free_port_for_field_cable(
                        self.canvas_view, sfp=(stub.cable.cable_type == "Fiber"),
                        ignore_cable=stub.cable)
                if port is None:
                    any_rejected = True
                    continue
                self._terminate_stub(stub, device.id, port)
            if occupied_target is not None:
                existing = device.field_cable_at_port(occupied_target, self.canvas_view)
                taken_by = f" ({existing.label})" if existing is not None else ""
                QMessageBox.warning(
                    self, "Port In Use",
                    f"{device.port_display_name(occupied_target)} on {device.label} is already "
                    f"in use{taken_by}. Free it up, or drop onto an empty port.")
            if any_rejected:
                QMessageBox.warning(self, "Ports Full", f"{device.label} has no free ports left for all of these.")
        else:
            for stub in stubs:
                self._terminate_stub(stub, device.id, None)

        self.canvas_view.refresh_connectivity_badges()
        self.refresh_soon()

    def _terminate_stub(self, stub, target_id, port):
        cable = stub.cable
        anchors = list(cable.vertex_anchors)
        anchors[stub.end_index] = target_id
        cable.set_points(list(cable.points), anchors=anchors)
        if stub.end_index == 0:
            cable.start_port = port
        else:
            cable.end_port = port
        # Keep the in-memory stub consistent with the cable straight away. The rebuild
        # that would otherwise set this is deferred (refresh_soon), so until it runs the
        # stub would still claim to be unrouted -- and return_stubs_to_pitchfork ignores
        # anything not flagged terminated, which is one way a cable can refuse to go back.
        stub.terminated = True

    # ── Patch cable drawing ──
    def handle_patch_click(self, slot, port):
        device = slot.device
        if (device.port_count or device.sfp_port_count) and port is None:
            # This device has a port grid but the click missed every cell -- ignore,
            # but say so rather than silently doing nothing, which just looks broken.
            self.hint_lbl.setText(f"That didn't land on a port on {device.label} -- try again, "
                                   f"zoom in if the grid is small.")
            return
        if port is not None and not device.port_free_for_new_cable(port, self.canvas_view, "Patch"):
            QMessageBox.warning(self, "Port In Use",
                                 f"{device.port_display_name(port)} on {device.label} is already in use.")
            self._patch_pending = None  # don't leave a stale pending endpoint behind
            self._update_hint()
            self.elevation_view.scene_obj.update()  # un-dim existing cables -- the draw was cancelled
            return

        endpoint = (device.id, port)
        if self._patch_pending is None:
            self._patch_pending = endpoint
            self._update_hint()
            self.hint_lbl.setText(f"Patch cable started at {device.label}" +
                                   (f" {device.port_display_name(port)}" if port else "") + " -- click the other end.")
            # Dim every existing patch cable NOW that a draw is actually in progress,
            # rather than waiting for some unrelated repaint to notice.
            self.elevation_view.scene_obj.update()
            return

        start_id, start_port = self._patch_pending
        self._patch_pending = None
        self._update_hint()
        if start_id == endpoint[0]:
            # Same device on both ends -- whether the exact same port or a different
            # one on itself, patching a device to itself is never a real connection.
            QMessageBox.warning(self, "Can't Patch a Device to Itself",
                                 f"Both ends landed on {device.label}. Pick a port/device on the OTHER side.")
            self.elevation_view.scene_obj.update()  # un-dim existing cables -- the draw was cancelled
            return

        self.canvas_view.push_undo_snapshot()
        from src.graphics.cable_item import CableItem, recalculate_all_cable_offsets
        cable = CableItem()
        cable.cable_type = "Patch"
        cable.label = "Patch Cable"
        cable.set_points([self.rack.pos(), self.rack.pos()], anchors=[start_id, endpoint[0]])
        cable.start_port = start_port
        cable.end_port = endpoint[1]
        cable.setVisible(False)  # patch cables are internal to the rack, not drawn on the floor plan
        self.canvas_view.scene_obj.addItem(cable)
        recalculate_all_cable_offsets(self.canvas_view.scene_obj)
        self.canvas_view.refresh_connectivity_badges()
        self.refresh_soon()
        other_device = self.canvas_view.find_device_or_camera_by_id(start_id)
        other_label = other_device.label if other_device is not None else "?"
        self.hint_lbl.setText(f"Patch cable connected: {other_label} → {device.label}.")

    # ── Rendering ──
    REFRESH_RETRY_MS = 50

    def refresh_soon(self):
        """Rebuild on the next event-loop turn instead of right now.

        refresh() calls scene.clear(), which destroys every item -- including whichever
        one Qt currently has as its mouse grabber if we're being called from inside a
        mouse handler (dropping a stub, laying a patch cable, deleting a selection).
        Tearing the grabber out from under Qt mid-event leaves the scene unable to
        route later presses at all: the rack stops responding to clicks entirely until
        the tab is closed and reopened. Letting the current event finish first avoids
        the whole problem.
        """
        if self._refresh_pending:
            return  # one is already queued; it will pick up every change since
        self._refresh_pending = True
        QTimer.singleShot(0, self._refresh_now)

    def _refresh_now(self):
        if getattr(self.elevation_view, "_drag_candidates", None):
            # A drag started before this rebuild came due, and rebuilding would destroy
            # the items it is holding. Come back for it rather than dropping it: giving
            # up here is what made "return to pitchfork" intermittent -- the cable moved
            # in the model but nothing redrew it, so it stayed invisible until the tab
            # was closed and reopened.
            QTimer.singleShot(self.REFRESH_RETRY_MS, self._refresh_now)
            return
        self._refresh_pending = False
        self.refresh()

    def refresh(self):
        # Rebuilding destroys and recreates every stub/block item, so refusing while a
        # stub drag is in flight keeps the items the drag is holding onto alive -- but
        # the rebuild is re-queued rather than abandoned. The drop path clears
        # _drag_candidates before calling refresh(), so the legitimate post-drop
        # rebuild still goes straight through.
        if getattr(self.elevation_view, "_drag_candidates", None):
            self.refresh_soon()
            return

        self.existing_combo.clear()
        self.existing_combo.addItem("Select a loose device…", None)
        for dev in self.canvas_view.get_network_devices():
            if self.canvas_view.find_rack_containing_device(dev.id) is not None:
                continue
            self.existing_combo.addItem(f"{dev.label} ({dev.spec.get('model', '')})", dev.id)

        self.elevation_view.refresh()
