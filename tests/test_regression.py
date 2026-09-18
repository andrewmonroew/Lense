"""Headless regression checks. Run: python3 tests/test_regression.py"""
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication, QGraphicsPathItem
from PySide6.QtCore import QPoint, QPointF, QSettings, Qt
from PySide6.QtGui import QPixmap, QTransform
from PySide6.QtGui import QKeyEvent as _QKE
from PySide6.QtCore import QEvent as _QEv
app = QApplication([])
# Preferences are read during MainWindow construction and written by the settings
# dialog, so point QSettings at a scope of our own -- these tests must neither be
# steered by the developer's real settings nor overwrite them.
app.setOrganizationName("LenseRegressionTests")
app.setApplicationName("LenseRegressionTests")

# A QMessageBox raised by production code (e.g. handle_stub_drop's "Port In Use") is
# modal and has nobody to click it here, so it wedges the whole suite until the outer
# timeout kills it -- which reads as "the tests hang" rather than "this check tripped a
# dialog". Poll for one and close it, recording what it said so a check can assert on
# it instead of guessing.
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QDialog, QMessageBox
dialogs_seen = []
auto_answer = None  # set to a QMessageBox button to answer an EXPECTED confirmation
def _dismiss_modals():
    w = app.activeModalWidget()
    if not isinstance(w, QDialog):
        return
    if auto_answer is not None and isinstance(w, QMessageBox):
        btn = w.button(auto_answer)
        if btn is not None:
            btn.click()
            return
    dialogs_seen.append(w.windowTitle())
    w.close()
_modal_watchdog = QTimer()
_modal_watchdog.timeout.connect(_dismiss_modals)
_modal_watchdog.start(50)

from src.ui.main_window import MainWindow
from src.ui.rack_editor_panel import RackEditorPanel
from src.graphics.cable_item import (CableItem, anchor_target_index,
                                     recalculate_all_cable_offsets,
                                     sync_anchored_vertices)
from src.graphics.zone_item import ZoneItem
from src.graphics.appliance_item import ApplianceItem
from src.core import poe_chain
from src.graphics import label_render
from src.core.utils import point_hits_item

win = MainWindow(); cv = win.canvas_view
cat = win.catalog_tree.catalog_data
def fresh():
    cv.scene_obj.clear(); cv.floorplan_item = None; cv.setTransform(QTransform())
    # Resync the zoom the scene believes it is at. setTransform() alone leaves view_lod
    # -- and every bounding rect sized from it -- stale at whatever the previous check
    # zoomed to. A zoom test leaving it at 2.3x made the repaint guard read 110ms for a
    # scene that renders in 20ms in a fresh process.
    cv._zoom_changed()
def link(a, b, ap=None, bp=None):
    c = CableItem(); c.cable_type = "CAT6"
    c.set_points([a.pos(), b.pos()], anchors=[a.id, b.id]); c.start_port, c.end_port = ap, bp
    cv.scene_obj.addItem(c); return c
checks = []
def ok(name, cond):
    checks.append((name, bool(cond)))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")

print("\n== Catalog integrity ==")
aps = [s for s in cat.values() if s.get("category") == "access-point"]
sws = [s for s in cat.values() if s.get("category") == "switch"]
ok("all APs have maxPower", all(s.get("maxPower") for s in aps))
ok("all APs have poeInput", all(s.get("poeInput") for s in aps))
ok("13 switches have poePortGroups", sum(1 for s in sws if s.get("poePortGroups")) == 13)
ok("Flex has conditional output", cat["ubiquiti-usw-flex"]["poeOutputByInput"] == {"802.3af":8,"802.3at":20,"802.3bt":46})
ok("U7 Pro is 21W", cat["ubiquiti-u7-pro"]["maxPower"] == 21)

print("\n== PoE chain ==")
fresh()
pro24 = cv.place_device(cat["ubiquiti-usw-24-poe"], QPointF(0,0)); pro24.label="Core"
flex  = cv.place_device(cat["ubiquiti-usw-flex"], QPointF(300,0)); flex.label="Tiny"
ap    = cv.place_device(cat["ubiquiti-u7-pro"], QPointF(600,0));   ap.label="AP"
link(pro24, flex, 20, 1); link(flex, ap, 2)
ok("PoE++ port -> chain is clean", not poe_chain.evaluate_device(cv, ap))
fresh()
pro24 = cv.place_device(cat["ubiquiti-usw-24-poe"], QPointF(0,0)); pro24.label="Core"
flex  = cv.place_device(cat["ubiquiti-usw-flex"], QPointF(300,0)); flex.label="Tiny"
ap    = cv.place_device(cat["ubiquiti-u7-pro"], QPointF(600,0));   ap.label="AP"
link(pro24, flex, 3, 1); link(flex, ap, 2)
msgs = [m for _s, m in poe_chain.evaluate_device(cv, ap)]
ok("PoE+ port -> 20W vs 21W error", any("20W" in m and "21W" in m for m in msgs))
fresh()
pro24 = cv.place_device(cat["ubiquiti-usw-24-poe"], QPointF(0,0))
mini  = cv.place_device(cat["ubiquiti-usw-flex-mini"], QPointF(300,0)); mini.label="Mini"
ap    = cv.place_device(cat["ubiquiti-u7-pro"], QPointF(600,0)); ap.label="AP"
link(pro24, mini, 20, 1); link(mini, ap)
ok("Flex Mini breaks the power chain", any(s=="error" for s,_ in poe_chain.evaluate_device(cv, mini)))

print("\n== Rack cable anchoring (this fix) ==")
fresh()
rack = cv.place_rack(QPointF(0,0), label="Rack", ru_height=12)
ok("snaps at rack bottom edge", cv.snap_to_devices(QPointF(0,21)) == QPointF(0,0))
ok("snaps at rack corner", cv.snap_to_devices(QPointF(16,21)) == QPointF(0,0))
ok("no snap well outside", cv.snap_to_devices(QPointF(0,60)) != QPointF(0,0))
dev = cv.place_device(cat["ubiquiti-u7-pro"], QPointF(400,0))
cv.set_tool("cable")
for raw in (QPointF(400,0), QPointF(0,20)):
    cv.cable_draw_points.append(cv.snap_to_devices(raw))
cv.temp_draw_line = QGraphicsPathItem(); cv.scene_obj.addItem(cv.temp_draw_line)
cv.complete_cable_drawing()
cable = cv.get_cables()[0]
ok("cable anchors to rack body", rack.id in cable.vertex_anchors)
ok("shows as pitchfork stub", len(RackEditorPanel(rack, cv, win).cables_terminating_here()) == 1)
win.sidebar.select_item(cable)
ok("rack-anchored cable sidebar builds", True)

print("\n== Rack editor refresh + stub drag ==")
from PySide6.QtGui import QMouseEvent
from src.graphics.rack_elevation_items import CableStubItem, EquipmentBlockItem
fresh()
rack2 = cv.place_rack(QPointF(0,0), label="Rack", ru_height=12)
ap2 = cv.place_device(cat["ubiquiti-u7-pro"], QPointF(400,0)); ap2.label="AP"
win.open_rack_tab(rack2)
panel = next(win.sheet_tabs.widget(i) for i in range(win.sheet_tabs.count())
             if getattr(win.sheet_tabs.widget(i), "rack", None) is rack2)
panel.resize(900,700); panel.elevation_view.resize(880,600); win.show(); app.processEvents()
panel.mount_new_device("ubiquiti-usw-24-poe")
evw = panel.elevation_view
def _stubs(): return [i for i in evw.scene_obj.items() if isinstance(i, CableStubItem)]
win.sheet_tabs.setCurrentIndex(0); app.processEvents()
cbl = CableItem(); cbl.cable_type="CAT6"
cbl.set_points([ap2.pos(), rack2.pos()], anchors=[ap2.id, rack2.id]); cv.scene_obj.addItem(cbl)
win.sheet_tabs.setCurrentWidget(panel); app.processEvents()
ok("canvas cable appears on tab switch", len(_stubs()) == 1)
stub = _stubs()[0]
blk = next(i for i in evw.scene_obj.items() if isinstance(i, EquipmentBlockItem))
def _mk(k,p,b,bs): return QMouseEvent(k, QPointF(p), QPointF(evw.mapToGlobal(p)), b, bs, Qt.NoModifier)
s_pt, e_pt = evw.mapFromScene(stub.pos()), evw.mapFromScene(blk.sceneBoundingRect().center())
evw.mousePressEvent(_mk(QMouseEvent.MouseButtonPress, s_pt, Qt.LeftButton, Qt.LeftButton))
ok("press selects the stub", len(evw._drag_candidates) == 1)
panel.refresh()
ok("mid-drag refresh is ignored", _stubs() and _stubs()[0] is stub)
evw.mouseMoveEvent(_mk(QMouseEvent.MouseMove, e_pt, Qt.NoButton, Qt.LeftButton))
evw.mouseReleaseEvent(_mk(QMouseEvent.MouseButtonRelease, e_pt, Qt.LeftButton, Qt.NoButton))
ok("stub drop terminates the cable", cbl.vertex_anchors[-1] != rack2.id)
# The post-drop rebuild is deliberately deferred to the next event-loop turn (see
# RackEditorPanel.refresh_soon -- tearing the scene down inside the mouse handler
# left the view unable to route clicks), so let that turn happen before checking.
app.processEvents()
ok("post-drop refresh runs", _stubs() and _stubs()[0].terminated)
win.sheet_tabs.setCurrentIndex(0)

print("\n== Other subsystems ==")
fresh()
ok("misc catalog has 9 appliances", len([s for s in cat.values() if s.get("category")=="misc"]) == 9)
tv = cv.place_device(cat["generic-misc-tv"], QPointF(0,0))
ok("TV renders as ApplianceItem", type(tv) is ApplianceItem and tv.label.startswith("TV"))
ok("select tool uses rubber band", (cv.set_tool("select"), cv.dragMode())[1].name == "RubberBandDrag")
cv.scene_obj.snap_to_grid = True; cv.scene_obj.grid_size = 50.0
cv.scene_obj.grid_size_is_default = False
ok("grid snap rounds", cv.snap_point(QPointF(112,138)) == QPointF(100,150))
cv.scene_obj.snap_to_grid = False
z = ZoneItem([QPointF(0,0),QPointF(100,0),QPointF(100,40),QPointF(0,40)], label="Box")
z.is_box = True; z.label_position = "above"
pt, valign = z.label_anchor_point(scale=1.0)
ok("box label sits 3px above", (pt.y(), valign) == (-3.0, "bottom"))
cv.scene_obj.addItem(z); z.setSelected(True); cv.duplicate_selected()
ok("duplicate keeps is_box", any(getattr(q,"is_box",False) for q in cv.get_zones() if q is not z))
state = win._serialize_state()
ok("project_type serialized", state.get("project_type") in ("cctv","network_topology"))
ok("zone label_position serialized", all("label_position" in zz for zz in state["zones"]))

print("\n== NVR sees cameras across a switch uplink ==")
fresh()
_nvr_id = next(k for k, v in cat.items() if v.get("category") == "nvr")
nvr = cv.place_device(cat[_nvr_id], QPointF(0,0)); nvr.label = "UNVR"
sw  = cv.place_device(cat["ubiquiti-usw-24-poe"], QPointF(300,0)); sw.label = "Core Switch"
link(sw, nvr, 24, 1)
for _i in range(4):
    _c = cv.place_camera(cat["ubiquiti-g5-bullet"], QPointF(600, _i*80)); _c.label = f"Cam{_i}"
    link(sw, _c, 1+_i)
ok("NVR has no directly-powered cameras", cv.get_cameras_connected_to_device(nvr.id) == [])
ok("NVR reaches the switch on its network",
   [d.label for d in cv.get_switched_network_peers(nvr.id)] == ["Core Switch"])
ok("NVR sees all 4 cameras over the network", len(cv.get_cameras_on_network_of(nvr.id)) == 4)
ok("PoE still attributes cameras to the switch, not the NVR",
   len(cv.get_cameras_connected_to_device(sw.id)) == 4)
ok("switch does not double-count them as networked",
   cv.get_cameras_on_network_of(sw.id) == [])

print("\n== Rack editor survives repeated stub drops ==")
from PySide6.QtTest import QTest
from PySide6.QtGui import QMouseEvent as _QME
from src.graphics.rack_elevation_items import CableStubItem as _CSI, EquipmentBlockItem as _EBI2
fresh()
_rack = cv.place_rack(QPointF(0,0), label="Rack", ru_height=12)
for _i in range(4):
    _a = cv.place_device(cat["ubiquiti-u7-lite"], QPointF(400, _i*60)); _a.label=f"AP{_i}"
    _cb = CableItem(); _cb.cable_type="CAT6"
    _cb.set_points([_a.pos(), _rack.pos()], anchors=[_a.id, _rack.id]); cv.scene_obj.addItem(_cb)
win.open_rack_tab(_rack)
_panel = next(win.sheet_tabs.widget(i) for i in range(win.sheet_tabs.count())
              if getattr(win.sheet_tabs.widget(i), "rack", None) is _rack)
win.resize(1200,900); win.show(); win.sheet_tabs.setCurrentWidget(_panel)
app.processEvents(); QTest.qWait(20)
_panel.mount_new_device("ubiquiti-usw-24-poe")
app.processEvents(); QTest.qWait(20)
_ev = _panel.elevation_view
_all_detected = True
for _n in range(3):
    app.processEvents(); QTest.qWait(20)
    _un = [s for s in _ev.scene_obj.items() if isinstance(s, _CSI) and not s.terminated]
    if not _un: break
    _blk = next(i for i in _ev.scene_obj.items() if isinstance(i, _EBI2))
    # Each run gets its OWN port -- dropping them all on the same one is a legitimate
    # "Port In Use" refusal, not the routing failure this check is about.
    _target = _blk.port_scene_pos(_n + 1)
    _s = _ev.mapFromScene(_un[0].pos()); _e = _ev.mapFromScene(_target)
    QTest.mousePress(_ev.viewport(), Qt.LeftButton, Qt.NoModifier, _s); app.processEvents()
    if len(_ev._drag_candidates) != 1:
        _all_detected = False
    QTest.mouseMove(_ev.viewport(), _e); app.processEvents()
    QTest.mouseRelease(_ev.viewport(), Qt.LeftButton, Qt.NoModifier, _e)
    app.processEvents(); QTest.qWait(20)
ok("stub clicks keep registering across repeated drops", _all_detected)
ok("stubs actually route", any(s.terminated for s in _ev.scene_obj.items() if isinstance(s, _CSI)))
win.sheet_tabs.setCurrentIndex(0)

print("\n== Moving cables between ports ==")
from src.graphics.rack_elevation_items import CableStubItem as _CS3, EquipmentBlockItem as _EB3
fresh()
_rk = cv.place_rack(QPointF(0,0), label="Rack", ru_height=12)
_runs = []
for _i in range(3):
    _a = cv.place_device(cat["ubiquiti-u7-lite"], QPointF(400, _i*60)); _a.label=f"AP{_i}"
    _c = CableItem(); _c.cable_type="CAT6"; _c.label=f"Run{_i}"
    _c.set_points([_a.pos(), _rk.pos()], anchors=[_a.id, _rk.id]); cv.scene_obj.addItem(_c)
    _runs.append(_c)
win.open_rack_tab(_rk)
_pn = next(win.sheet_tabs.widget(i) for i in range(win.sheet_tabs.count())
           if getattr(win.sheet_tabs.widget(i), "rack", None) is _rk)
win.resize(1200,900); win.show(); win.sheet_tabs.setCurrentWidget(_pn)
app.processEvents()
_pn.mount_new_device("ubiquiti-usw-24-poe")
app.processEvents()
_evw = _pn.elevation_view
_blk3 = next(i for i in _evw.scene_obj.items() if isinstance(i, _EB3))
_dev3 = _blk3.slot.device
_st = [s for s in _evw.scene_obj.items() if isinstance(s, _CS3)]
ok("stub hit area is tighter than its label bounding box",
   _st[0].shape().boundingRect().width() < _st[0].boundingRect().width())

_pn._terminate_stub(_st[0], _dev3.id, 5)
app.processEvents()
_c0 = _st[0].cable
ok("a stub can be re-dropped on the port it already holds",
   _dev3.port_free_for_new_cable(5, cv, "CAT6", ignore_cable=_c0))
ok("that port is still blocked for a different cable",
   not _dev3.port_free_for_new_cable(5, cv, "CAT6", ignore_cable=_runs[1]))
ok("terminating flags the stub immediately", _st[0].terminated)

_other3 = next(s for s in _st if s.cable is not _c0)
_seen3 = {}
import src.ui.rack_editor_panel as _rep3
_realwarn = _rep3.QMessageBox.warning
_rep3.QMessageBox.warning = staticmethod(lambda *a, **k: _seen3.update(title=a[1]))
_before3 = [(c.label, c.start_port, c.end_port) for c in _runs]
_pn.handle_stub_drop([_other3], _blk3, _blk3.mapToScene(_blk3.port_rect(5).center()))
app.processEvents()
_after3 = [(c.label, c.start_port, c.end_port) for c in _runs]
_rep3.QMessageBox.warning = _realwarn
ok("dropping on an occupied port warns", _seen3.get("title") == "Port In Use")
ok("and relocates nothing behind your back", _before3 == _after3)

_pn.return_stubs_to_pitchfork([_st[0]])
app.processEvents()
ok("a terminated stub returns to the pitchfork", _rk.id in _c0.vertex_anchors)
win.sheet_tabs.setCurrentIndex(0)

print("\n== Port map visibility ==")
fresh()
pro = cv.place_device(cat["ubiquiti-usw-24-poe"], QPointF(0,0))
pm = win.sidebar.build_port_map_group(pro)
from PySide6.QtWidgets import QFormLayout as _QFL
rows = []
if pm:
    _f = pm.layout()
    for r in range(_f.rowCount()):
        _l = _f.itemAt(r, _QFL.LabelRole); _v = _f.itemAt(r, _QFL.FieldRole)
        rows.append(((_l.widget().text() if _l and _l.widget() else ""),
                     (_v.widget().text() if _v and _v.widget() else "")))
ok("Pro 24 exposes a port map", bool(rows))
ok("names the PoE+ bank 1-16 @32W", any("1\u201316" in a and "32W" in b for a, b in rows))
ok("names the PoE++ bank 17-24 @64W", any("17\u201324" in a and "64W" in b for a, b in rows))
ok("flags SFP as data only", any("SFP" in a and "data only" in b.lower() for a, b in rows))
flexdev = cv.place_device(cat["ubiquiti-usw-flex"], QPointF(200,0))
frows = []
_pm = win.sidebar.build_port_map_group(flexdev)
if _pm:
    _f = _pm.layout()
    for r in range(_f.rowCount()):
        _l = _f.itemAt(r, _QFL.LabelRole); _v = _f.itemAt(r, _QFL.FieldRole)
        frows.append(((_l.widget().text() if _l and _l.widget() else ""),
                      (_v.widget().text() if _v and _v.widget() else "")))
ok("Flex labels port 1 as PoE input", any("Port 1" in a and "input" in b.lower() for a, b in frows))
ok("Flex does not claim a phantom data-only port",
   not any("data only" in b.lower() for a, b in frows))

from src.graphics.rack_elevation_items import EquipmentBlockItem as _EBI
from src.graphics.rack_item import RackSlot as _RS
class _P: pass
_p = _P(); _p.canvas_view = cv
_blk = _EBI(_RS(pro, 1, 1), 12, _p)
ok("PoE+ and PoE++ banks are tinted differently",
   _blk.port_fill(1, False).name() != _blk.port_fill(17, False).name())
ok("occupied still overrides the tint", _blk.port_fill(1, True).name() == "#22c55e")

from src.ui.catalog_manager import CatalogManagerDialog as _CMD
_dlg = _CMD(win.catalog_tree, win)
_dlg.load_entry("ubiquiti-usw-24-poe")
_txt = _dlg.poe_groups_edit.toPlainText()
ok("catalog editor shows the port map", "1-16: 802.3at 32W" in _txt)
ok("port map round-trips through the editor",
   _dlg._parse_poe_groups(_txt) == [{"firstPort":1,"count":16,"standard":"802.3at","maxWatts":32.0},
                                    {"firstPort":17,"count":8,"standard":"802.3bt","maxWatts":64.0}])
ok("malformed port-map lines are skipped", _dlg._parse_poe_groups("junk\n1-4: 802.3at 30W") ==
   [{"firstPort":1,"count":4,"standard":"802.3at","maxWatts":30.0}])

print("\n== Network diagram trunks correctly ==")
fresh()
_sw17 = cv.place_device(cat["ubiquiti-usw-24-poe"], QPointF(0,0)); _sw17.label="SWITCH 17"
def _patch(a,b,ap,bp):
    _c=CableItem(); _c.cable_type="Patch"; _c.set_points([a.pos(),b.pos()],anchors=[a.id,b.id])
    _c.start_port,_c.end_port=ap,bp; _c.setVisible(False); cv.scene_obj.addItem(_c)
for _i in range(4):
    _dp = cv.place_device(cat["generic-wall-drop-1port"], QPointF(300,_i*100)); _dp.label=f"Drop{_i}"
    _fx = cv.place_device(cat["ubiquiti-usw-flex"], QPointF(600,_i*100)); _fx.label=f"Flex{_i}"
    link(_sw17, _dp, 17+_i, 1); _patch(_dp, _fx, 1, 1)
    _ap = cv.place_device(cat["ubiquiti-u7-lite"], QPointF(900,_i*100)); _ap.label=f"AP{_i}"
    link(_fx, _ap, 2)
_roots = cv.build_network_topology()
ok("one root under WAN, not one per switch", len(_roots) == 1)
ok("the trunk switch is the root", _roots and _roots[0]["device"].label == "SWITCH 17")
ok("flexes trunk beneath it (link survives the wall drop)",
   _roots and sorted(c["device"].label for c in _roots[0]["children"]) == ["Flex0","Flex1","Flex2","Flex3"])

print("\n== Properties fields ignore the scroll wheel unless focused ==")
from PySide6.QtWidgets import QAbstractSpinBox as _ASB, QComboBox as _CB, QSlider as _SL, QApplication as _QA
from PySide6.QtGui import QWheelEvent as _QWE
from PySide6.QtCore import QPoint as _QP
def _wheel():
    return _QWE(QPointF(5,5), QPointF(5,5), _QP(0,0), _QP(0,-120),
                Qt.NoButton, Qt.NoModifier, Qt.ScrollUpdate, False)
fresh()
_probe = [("camera", cv.place_camera(next(v for v in cat.values() if v.get("category")=="camera"), QPointF(0,0))),
          ("switch", cv.place_device(cat["ubiquiti-usw-24-poe"], QPointF(200,0))),
          ("rack",   cv.place_rack(QPointF(400,0), label="R", ru_height=12))]
_hijacks, _count = 0, 0
for _lbl, _it in _probe:
    win.sidebar.select_item(_it)
    _ws = []
    for _t in (_ASB, _CB, _SL):
        _ws += win.sidebar.container.findChildren(_t)
    _count += len(_ws)
    for _w in _ws:
        _get = (lambda x=_w: x.value()) if hasattr(_w, "value") else (lambda x=_w: x.currentIndex())
        _b = _get(); _w.clearFocus(); _QA.sendEvent(_w, _wheel())
        if _get() != _b:
            _hijacks += 1
ok(f"no value widget changes on hover-scroll ({_count} checked)", _hijacks == 0)

print("\n== Lasso cable bundling ==")
fresh()
_rk2 = cv.place_rack(QPointF(500,0), label="Rack", ru_height=12)
_drops2 = []
for _i in range(8):
    _d2 = cv.place_device(cat["generic-wall-drop-1port"], QPointF(150+_i*110, 600))
    _d2.label = f"Drop{_i}"
    _c2 = CableItem(); _c2.cable_type="CAT6"
    _c2.set_points([_rk2.pos(), _d2.pos()], anchors=[_rk2.id, _d2.id])
    cv.scene_obj.addItem(_c2); _drops2.append(_d2)
_runs2 = list(cv.get_cables())
_p1, _p2 = QPointF(100, 320), QPointF(1000, 320)
ok("lasso finds every crossed cable", len(cv._lasso_crossings(_p1, _p2)) == 8)
_n2 = cv.apply_lasso(_p1, _p2)
_ctr = QPointF((_p1.x()+_p2.x())/2, (_p1.y()+_p2.y())/2)
ok("lasso bundles all crossed cables", _n2 == 8)
ok("each run gains exactly one vertex", all(len(c.points) == 3 for c in _runs2))
ok("they share one common point", all(any(pt == _ctr for pt in c.points) for c in _runs2))
ok("endpoints/anchors survive untouched",
   all(c.vertex_anchors[0] == _rk2.id and c.vertex_anchors[-1] in {d.id for d in _drops2}
       for c in _runs2))
from src.graphics.cable_item import recalculate_all_cable_offsets as _rc
_rc(cv.scene_obj)
ok("shared segment renders as a merged bundle",
   any((max(c.segment_widths) if c.segment_widths else 1.0) > 1.0 for c in _runs2))
_before2 = [len(c.points) for c in _runs2]
ok("a lasso that crosses nothing is a no-op",
   cv.apply_lasso(QPointF(0,9000), QPointF(10,9000)) == 0
   and [len(c.points) for c in _runs2] == _before2)

def _mount(label_map, ru=12):
    """A rack with equipment in it, plus its editor panel."""
    rack = cv.place_rack(QPointF(500, 0), label="Rack 1", ru_height=ru)
    win.open_rack_tab(rack)
    panel = next(win.sheet_tabs.widget(i) for i in range(win.sheet_tabs.count())
                 if getattr(win.sheet_tabs.widget(i), "rack", None) is rack)
    made = {}
    for name, spec_id in label_map:
        before = {s.device.id for s in rack.slots}
        panel.mount_new_device(spec_id)
        dev = next(s.device for s in rack.slots if s.device.id not in before)
        dev.label = name
        made[name] = dev
    return rack, panel, made

print("\n== Network diagram: rack-anchored trunks ==")
# Cables drawn on the floor plan anchor to the RACK, not to the switch inside it, so
# the topology graph used to hit a dead end there and show every downstream switch as
# its own island hanging straight off the WAN.
fresh()
rack, panel, made = _mount([("SWITCH 17", "ubiquiti-usw-24-poe")])
sw17 = made["SWITCH 17"]
nvr19 = cv.place_device(cat["ubiquiti-unvr"], QPointF(700, 0)); nvr19.label = "NVR 19"
link(nvr19, rack)
flexes = []
for _i in range(3):
    f = cv.place_device(cat["ubiquiti-usw-flex"], QPointF(200 + _i*200, 400))
    f.label = f"Flex{_i}"; flexes.append(f); link(f, rack)
roots = cv.build_network_topology()
ok("one root, not five islands", len(roots) == 1)
ok("the aggregation switch is the root", roots and roots[0]["device"] is sw17)
_kids = {c["device"].label for c in roots[0]["children"]} if roots else set()
ok("all three Flexes trunk under it", {"Flex0", "Flex1", "Flex2"} <= _kids)
ok("the NVR hangs off it too", "NVR 19" in _kids)
ok("a rack resolves to its best-connected switch", cv.rack_uplink_device(rack) is sw17)

# Degree decides the head, but category still breaks a tie: the classic small system
# (one NVR, one switch, both with a single link) must still root at the NVR.
fresh()
_nvr = cv.place_device(cat["ubiquiti-unvr"], QPointF(0, 0)); _nvr.label = "NVR"
_sw = cv.place_device(cat["ubiquiti-usw-24-poe"], QPointF(200, 0)); _sw.label = "Switch"
link(_nvr, _sw)
_roots = cv.build_network_topology()
ok("NVR still heads a one-switch system", len(_roots) == 1 and _roots[0]["device"] is _nvr)

fresh()
cv.place_device(cat["ubiquiti-usw-24-poe"], QPointF(0, 0))
cv.place_device(cat["ubiquiti-usw-flex"], QPointF(300, 0))
ok("unwired devices each still appear", len(cv.build_network_topology()) == 2)

# The residential shape that was actually broken: the trunk from the aggregation switch
# reaches each downstream switch through a WALL DROP, and nothing on the floor plan ever
# assigns a port number, so the jack could not be crossed and every branch floated off on
# its own. The NVR meanwhile is patched straight into the switch inside the rack.
fresh()
rack, panel, made = _mount([("SWITCH 17", "ubiquiti-usw-24-poe"),
                            ("PP", "generic-patch-panel-24"),
                            ("NVR 19", "ubiquiti-unvr")])
sw17, pp, nvr = made["SWITCH 17"], made["PP"], made["NVR 19"]
_jumper = CableItem(); _jumper.cable_type = "Patch"
_jumper.set_points([rack.pos(), rack.pos()], anchors=[nvr.id, sw17.id])
_jumper.start_port, _jumper.end_port = 1, 1
cv.scene_obj.addItem(_jumper)
for _i in range(3):
    _drop = cv.place_device(cat["generic-wall-drop-1port"], QPointF(300, _i*150))
    _drop.label = f"Wall Drop {_i}"
    _flex = cv.place_device(cat["ubiquiti-usw-flex"], QPointF(600, _i*150))
    _flex.label = f"Flex{_i}"
    _ap = cv.place_device(cat["ubiquiti-u6-plus"], QPointF(900, _i*150))
    _ap.label = f"AP{_i}"
    _pj = CableItem(); _pj.cable_type = "Patch"
    _pj.set_points([rack.pos(), rack.pos()], anchors=[pp.id, sw17.id])
    _pj.start_port, _pj.end_port = _i + 1, _i + 2
    cv.scene_obj.addItem(_pj)
    link(_drop, pp, None, _i + 1)   # drop -> panel: port known only on the panel side
    link(_drop, _flex)              # drop -> flex: no ports at all
    link(_flex, _ap)
_roots = cv.build_network_topology()
ok("wall-drop trunks resolve to one root", len(_roots) == 1)
ok("the aggregation switch heads it", _roots and _roots[0]["device"] is sw17)
_kids = {c["device"].label for c in _roots[0]["children"]} if _roots else set()
ok("every Flex crosses its wall drop", {"Flex0", "Flex1", "Flex2"} <= _kids)
ok("a patch-cabled NVR is not stranded", "NVR 19" in _kids)
_flex0 = next(c for c in _roots[0]["children"] if c["device"].label == "Flex0")
ok("APs stay under their own Flex", [c["device"].label for c in _flex0["children"]] == ["AP0"])

print("\n== Connected Clients lists more than cameras ==")
_clients = {d.label for d in cv.get_client_devices_of(sw17.id)}
ok("switch clients include the Flexes", {"Flex0", "Flex1", "Flex2"} <= _clients)
ok("switch clients include the NVR", "NVR 19" in _clients)
ok("a Flex lists its access point", [d.label for d in cv.get_client_devices_of(_flex0["device"].id)] == ["AP0"])
ok("the uplink is never listed as a client", "SWITCH 17" not in _clients)
ok("wall drops are never listed as clients", not any(l.startswith("Wall Drop") for l in _clients))
_ap0 = next(d for d in cv.get_network_devices() if d.label == "AP0")
ok("an AP counts as a PoE load", poe_chain.is_powered_device(_ap0))
ok("a UNVR does not", not poe_chain.is_powered_device(nvr))

print("\n== Deleting a cable frees the ports it used ==")
# A patch cable only exists to carry the field run punched down on the back of a
# passive jack. Delete that run and the jumper is orphaned -- but cables_at_port still
# counted it, so the patch panel port stayed green and the switch port stayed taken.
fresh()
rack, panel, made = _mount([("PP", "generic-patch-panel-24"), ("SW", "ubiquiti-usw-24-poe")])
pp, sw = made["PP"], made["SW"]
_cam = cv.place_camera(cat["ubiquiti-g5-bullet"], QPointF(100, 100))
_field = link(_cam, pp, None, 3)
_patch = CableItem(); _patch.cable_type = "Patch"; _patch.label = "Patch Cable"
_patch.set_points([rack.pos(), rack.pos()], anchors=[pp.id, sw.id])
_patch.start_port, _patch.end_port = 3, 5
cv.scene_obj.addItem(_patch)
ok("port reads occupied while cabled", pp.cables_at_port(3, cv) and sw.cables_at_port(5, cv))
cv.is_active_tab = True
cv.scene_obj.clearSelection(); _field.setSelected(True)
cv.delete_selected()
ok("patch panel port frees on delete", not pp.cables_at_port(3, cv))
ok("the switch port it fed frees too", not sw.cables_at_port(5, cv))
ok("the orphaned jumper is gone from the scene", not cv.get_cables())

# Deleting the rack takes its equipment's jumpers with it, the same rule the Rack
# Editor already applied when unmounting. (Mounted devices are deliberately never added
# to the canvas scene -- see get_network_devices -- so deleting the RACK is the only way
# to remove them from the floor plan.)
fresh()
rack, panel, made = _mount([("PP", "generic-patch-panel-24"), ("SW", "ubiquiti-usw-24-poe")])
pp, sw = made["PP"], made["SW"]
_patch = CableItem(); _patch.cable_type = "Patch"
_patch.set_points([rack.pos(), rack.pos()], anchors=[pp.id, sw.id])
_patch.start_port, _patch.end_port = 1, 1
cv.scene_obj.addItem(_patch)
_ap = cv.place_device(cat["ubiquiti-u7-lite"], QPointF(900, 0))
_run = link(_ap, rack)
cv.is_active_tab = True
cv.scene_obj.clearSelection(); rack.setSelected(True)
auto_answer = QMessageBox.Yes  # the "Delete Populated Rack" confirmation
cv.delete_selected()
auto_answer = None
ok("deleting a rack removes its equipment's jumpers", _patch.scene() is None)
ok("but the field run into it survives as a free end", _run.scene() is not None)

# A field cable landing directly on a real switch port must NOT drag unrelated patch
# cables down with it -- only jumpers on the far side of a passive jack are orphaned.
fresh()
rack, panel, made = _mount([("SW", "ubiquiti-usw-24-poe"), ("SW2", "ubiquiti-usw-24-poe")])
sw, sw2 = made["SW"], made["SW2"]
_cam = cv.place_camera(cat["ubiquiti-g5-bullet"], QPointF(100, 100))
_field = link(_cam, sw, None, 3)
_trunk = CableItem(); _trunk.cable_type = "Patch"
_trunk.set_points([rack.pos(), rack.pos()], anchors=[sw.id, sw2.id])
_trunk.start_port, _trunk.end_port = 3, 1
cv.scene_obj.addItem(_trunk)
cv.is_active_tab = True
cv.scene_obj.clearSelection(); _field.setSelected(True)
cv.delete_selected()
ok("the field cable is really gone", _field.scene() is None)
ok("an unrelated patch cable survives", _trunk.scene() is not None)

print("\n== Scene-wide geometry changes must notify Qt first ==")
# Qt's contract is prepareGeometryChange() BEFORE the geometry changes: it drops the
# item from the scene's spatial index so the new rect is re-inserted on next access.
# Changing a scene-wide value first and notifying afterwards leaves every item indexed
# under a rect it no longer has; the BSP tree goes inconsistent and segfaults mid-paint.
fresh()
_probe_dev = cv.place_device(cat["ubiquiti-usw-24-poe"], QPointF(0, 0))
_order = []
_real_prepare = type(_probe_dev).prepareGeometryChange
def _record(self):
    _order.append("notify")
    return _real_prepare(self)
type(_probe_dev).prepareGeometryChange = _record
try:
    cv.apply_scene_geometry_change(lambda: _order.append("change"))
finally:
    type(_probe_dev).prepareGeometryChange = _real_prepare
ok("items are notified before the change lands",
   "notify" in _order and "change" in _order
   and _order.index("notify") < _order.index("change"))
ok("every affected item is notified", _order.count("notify") >= 1)

# The three scene-wide values that resize every item must all go through it.
_src_cv = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "src", "ui", "canvas_view.py")).read()
for _name in ("global_icon_scale = value", "view_lod = lod"):
    _at = _src_cv.index(_name)
    _window = _src_cv[max(0, _at - 400):_at]
    ok(f"'{_name.split(' =')[0]}' is assigned inside a notified block",
       "apply_scene_geometry_change" in _src_cv[_at:_at + 400] or "def assign" in _window)

print("\n== Icon scaling ==")
from src.graphics.icon_scale import MAX_ICON_SCALE, MIN_ICON_SCALE
fresh()
cv.scene_obj.scale_ratio = 10.0
_cam = cv.place_camera(cat["ubiquiti-g5-bullet"], QPointF(0, 0))
_dev = cv.place_device(cat["ubiquiti-usw-24-poe"], QPointF(200, 0))
_ap = cv.place_device(cat["ubiquiti-u6-plus"], QPointF(400, 0))
_rack = cv.place_rack(QPointF(600, 0), label="R", ru_height=12)

cv.set_icon_scale(1.0)
_base = (_cam.icon_radius, _dev.width, _dev.height, _ap.width, _rack.width,
         _dev.shape().boundingRect().width())
_fov_1x = _cam.boundingRect().width()
cv.set_icon_scale(3.0)
_big = (_cam.icon_radius, _dev.width, _dev.height, _ap.width, _rack.width,
        _dev.shape().boundingRect().width())
ok("every icon scales linearly", all(abs(b * 3.0 - g) < 1e-9 for b, g in zip(_base, _big)))
# The FOV wedge and IR ring are distances in feet off the calibration ratio. If they
# ever start following the icon scale, the coverage diagram silently starts lying.
ok("camera coverage is NOT scaled", _cam.boundingRect().width() == _fov_1x)
ok("hit-testing follows the icon",
   point_hits_item(_dev, QPointF(_dev.pos().x() + 40, _dev.pos().y()), radius=0.0))
cv.set_icon_scale(1.0)
ok("and shrinks back with it",
   not point_hits_item(_dev, QPointF(_dev.pos().x() + 40, _dev.pos().y()), radius=0.0))

ok("scale is clamped low", cv.set_icon_scale(0.001) == MIN_ICON_SCALE)
ok("scale is clamped high", cv.set_icon_scale(1000) == MAX_ICON_SCALE)
ok("garbage falls back to 1x", cv.set_icon_scale("banana") == 1.0)

# A device built while a scale is already active must store its NATURAL size, or every
# item placed after the slider moved would come out permanently over- or under-sized.
cv.set_icon_scale(4.0)
_late = cv.place_device(cat["ubiquiti-usw-24-poe"], QPointF(800, 0))
ok("items placed later match ones placed earlier", _late.width == _dev.width)
cv.set_icon_scale(1.0)
ok("and are natural size at 1x", _late.width == 32.0)

# The artwork INSIDE an icon -- a Wi-Fi fan, port squares, bolt holes -- is drawn at
# literal coordinates picked against the natural size, so it only follows the canvas
# scale because paint() scales the painter. The design_* naturals are what those
# literals are measured against and must never move.
cv.set_icon_scale(1.0)
_pp = cv.place_device(cat["generic-patch-panel-24"], QPointF(0, 200))
_drop = cv.place_device(cat["generic-wall-drop-2port"], QPointF(200, 200))
_nat = (_dev.design_width, _dev.design_height, _dev.design_radius,
        _ap.design_width, _pp.design_width, _drop.design_width, _rack.design_width)
cv.set_icon_scale(5.0)
_nat_after = (_dev.design_width, _dev.design_height, _dev.design_radius,
              _ap.design_width, _pp.design_width, _drop.design_width, _rack.design_width)
ok("natural sizes are unaffected by the scale", _nat == _nat_after)
ok("scaled sizes are the naturals times the scale", _dev.width == _dev.design_width * 5.0)
# Sizing the rack's bolt run off the SCALED height put fourteen bolts down a 4x rack.
ok("rack detail counts come off the natural height", _rack._design_icon_height() == 44.0)
ok("but its drawn height still scales", _rack._icon_height() == 44.0 * 5.0)

# Nothing may throw while painting at a large scale -- these paths only run on repaint,
# so a bad reference here would surface as a broken canvas rather than an exception.
from PySide6.QtGui import QImage as _QImg, QPainter as _QP
_img = _QImg(400, 400, _QImg.Format_ARGB32)
_p = _QP(_img)
try:
    cv.scene_obj.render(_p)
    _painted = True
except Exception:
    _painted = False
_p.end()
ok("every item paints cleanly at 5x", _painted)

# Inside a scaled painter the transform multiplies the PEN as well as the geometry, so
# a 2px outline became 7px at 350% -- a bloated sausage around an otherwise correctly
# sized icon. Stroke weights divide the icon scale back out (`screen`); drawn features
# like port squares and bolt holes keep `scale` so they stay proportional.
import re as _re
_gfx = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "graphics")
_offenders = []
_checked = 0
for _fn in sorted(os.listdir(_gfx)):
    if not _fn.endswith(".py"):
        continue
    if _fn == "icon_scale.py":
        continue  # defines the helper; its docstring is not a paint block
    _text = open(os.path.join(_gfx, _fn)).read()
    if "paint_at_icon_scale(painter" not in _text or "painter.restore()" not in _text:
        continue
    _checked += 1
    _blk = _text[_text.index("paint_at_icon_scale(painter"):]
    _blk = _blk[:_blk.index("painter.restore()")]
    if "screen = scale / paint_at_icon_scale" not in _text:
        _offenders.append(f"{_fn}: no screen scale")
    for _line in _blk.splitlines():
        _code = _line.split("#")[0]
        # Inside a scaled painter NOTHING may use `scale` (= 1/lod, "constant pixels on
        # screen"). Stroke weights use `screen`, which divides the icon scale back out;
        # drawn features use plain natural units so they shrink with the icon. A
        # constant-on-screen size here grows in scene units as you zoom out while the
        # icon shrinks around it, which is how the rack's bolt holes ended up bulging
        # through its own rails below about 32% zoom.
        if "* scale" in _code:
            _offenders.append(f"{_fn}: {_line.strip()}")
ok("every icon that scales its painter defines a screen scale", _checked >= 5)
ok("nothing inside a scaled painter is sized in screen pixels", not _offenders)
if _offenders:
    print("   offenders:", *_offenders, sep="\n     ")

print("\n== Cables follow the icon scale ==")
fresh()
_a = cv.place_device(cat["ubiquiti-usw-24-poe"], QPointF(0, 0))
_b = cv.place_device(cat["ubiquiti-u6-plus"], QPointF(400, 0))
_c = link(_a, _b)
cv.set_icon_scale(1.0)
_w1 = _c.shape().boundingRect().height()
cv.set_icon_scale(4.0)
_w4 = _c.shape().boundingRect().height()
# The click target is raw scene units, so it does need the scale.
ok("a cable's click target grows with the scale", _w4 > _w1 * 3.5)

# Drawn line WEIGHT must not. Every cable width is multiplied by 1/lod, which already
# cancels the view transform and renders a constant number of pixels at any zoom --
# that is exactly why cables stay readable on a huge plan while the icons shrink to
# specks. Multiplying an already zoom-invariant width by the icon scale drew sausages.
_src_cable = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "src", "graphics", "cable_item.py")).read()
_paint_body = _src_cable[_src_cable.index("    def paint(self"):]
_paint_body = _paint_body[:_paint_body.index("\ndef ")] if "\ndef " in _paint_body else _paint_body
ok("cable line weights are not multiplied by the icon scale",
   "icon_scale" not in _paint_body)
ok("cable geometry is still anchored to its endpoints",
   _c.points[0] == _a.pos() and _c.points[-1] == _b.pos())

# Fan-out spacing is a distance BETWEEN runs -- the same kind of quantity as an icon's
# width -- so it stays on the full linear scale, or bundles stop separating visibly.
from src.graphics.cable_item import recalculate_all_cable_offsets as _recalc
_hub = cv.place_device(cat["ubiquiti-usw-24-poe"], QPointF(0, 400))
_way = QPointF(0, 600)          # every run shares the waypoint -> hub segment
_fan = []
for _i in range(4):
    _d3 = cv.place_device(cat["generic-wall-drop-1port"], QPointF(200 + _i * 40, 800))
    _fc = CableItem(); _fc.cable_type = "CAT6"
    _fc.set_points([_d3.pos(), _way, _hub.pos()], anchors=[_d3.id, None, _hub.id])
    cv.scene_obj.addItem(_fc); _fan.append(_fc)

def _fan_span():
    # Hover the shared segment's midpoint -- a bundle only fans out under the cursor.
    _recalc(cv.scene_obj, hover_pt=QPointF(0, 500))
    xs = [(c.draw_points or c.points)[1].x() for c in _fan]
    return max(xs) - min(xs)

cv.set_icon_scale(1.0); _span1 = _fan_span()
cv.set_icon_scale(4.0); _span4 = _fan_span()
ok("the bundle actually fans out", _span1 > 0)
ok("fan-out separation scales linearly with the icons", _span4 > _span1 * 2.0)
cv.set_icon_scale(1.0)

# A PAIR has to separate about as visibly as a big bundle does. Deriving the gap from a
# small fixed preference gave a 2-cable bundle a sixth of a 10-cable one's spread, so a
# branch off to two wall drops looked like it was ignoring the hover completely.
def _pair_vs_many(count):
    fresh()
    hub = cv.place_device(cat["ubiquiti-usw-24-poe"], QPointF(0, 0))
    way = QPointF(0, 600)
    runs = []
    for i in range(count):
        d = cv.place_device(cat["generic-wall-drop-1port"], QPointF(200 + i * 40, 800))
        fc = CableItem(); fc.cable_type = "CAT6"
        fc.set_points([d.pos(), way, hub.pos()], anchors=[d.id, None, hub.id])
        cv.scene_obj.addItem(fc); runs.append(fc)
    _recalc(cv.scene_obj, hover_pt=QPointF(0, 300))
    xs = [(c.draw_points or c.points)[1].x() for c in runs]
    return max(xs) - min(xs)

# Aim tolerance is in SCREEN pixels, not scene units: a scene-unit radius shrinks to
# nothing on screen as you zoom out, so a bundle you can plainly see becomes impossible
# to land on -- which reads as "it doesn't fan out".
def _reach(lod):
    fresh()
    cv.scene_obj.view_lod = lod
    hub = cv.place_device(cat["ubiquiti-usw-24-poe"], QPointF(0, 0))
    way = QPointF(0, 600)
    runs = []
    for i in range(2):
        d = cv.place_device(cat["generic-wall-drop-1port"], QPointF(200 + i * 40, 800))
        fc = CableItem(); fc.cable_type = "CAT6"
        fc.set_points([d.pos(), way, hub.pos()], anchors=[d.id, None, hub.id])
        cv.scene_obj.addItem(fc); runs.append(fc)
    # Furthest scene distance from the run that still OPENS it, in screen pixels. The
    # latch is cleared each step: once open a bundle deliberately stays open much
    # further out, and that hysteresis is not what this is measuring.
    for off in range(0, 400, 5):
        cv.scene_obj.open_bundle = None
        _recalc(cv.scene_obj, hover_pt=QPointF(off, 300))
        xs = [(c.draw_points or c.points)[1].x() for c in runs]
        if max(xs) - min(xs) < 0.5:
            return (off - 5) * lod
    return 400 * lod

_near, _far = _reach(1.0), _reach(0.25)
ok("aim tolerance holds up when zoomed out", abs(_near - _far) < _near * 0.35)
cv.scene_obj.view_lod = 1.0

_pair = _pair_vs_many(2)
_many = _pair_vs_many(8)
ok("a pair of cables fans out too", _pair > 0)
ok("and not far less than a big bundle does", _pair >= _many * 0.4)
ok("a big bundle still fans within its span", _many > _pair)

print("\n== Duplicating numbers instead of appending Copy ==")
fresh()
cv.is_active_tab = True
_ap1 = cv.place_device(cat["ubiquiti-u6-plus"], QPointF(0, 0)); _ap1.label = "Access Point 1"
cv.scene_obj.clearSelection(); _ap1.setSelected(True)
cv.duplicate_selected()
_labels = sorted(d.label for d in cv.get_network_devices())
ok("Access Point 1 duplicates to Access Point 2", _labels == ["Access Point 1", "Access Point 2"])

# Duplicating several at once must hand out distinct numbers, not all fight over 2.
cv.scene_obj.clearSelection()
for _d2 in cv.get_network_devices():
    _d2.setSelected(True)
cv.duplicate_selected()
_labels = sorted(d.label for d in cv.get_network_devices())
ok("a multi-selection gets consecutive numbers",
   _labels == ["Access Point 1", "Access Point 2", "Access Point 3", "Access Point 4"])

# A number already taken is skipped rather than reused.
fresh()
_x1 = cv.place_device(cat["ubiquiti-u6-plus"], QPointF(0, 0)); _x1.label = "AP 1"
_x2 = cv.place_device(cat["ubiquiti-u6-plus"], QPointF(50, 0)); _x2.label = "AP 2"
cv.scene_obj.clearSelection(); _x1.setSelected(True)
cv.duplicate_selected()
ok("an already-used number is skipped",
   sorted(d.label for d in cv.get_network_devices()) == ["AP 1", "AP 2", "AP 3"])

# Nothing to increment still falls back to Copy.
fresh()
_nm = cv.place_device(cat["ubiquiti-u6-plus"], QPointF(0, 0)); _nm.label = "Garage AP"
cv.scene_obj.clearSelection(); _nm.setSelected(True)
cv.duplicate_selected()
ok("an unnumbered name still gets Copy",
   sorted(d.label for d in cv.get_network_devices()) == ["Garage AP", "Garage AP Copy"])

# Equipment carried along inside a duplicated rack gets renumbered too, or the copy
# comes up with a second "SWITCH 17" and the diagram shows two of them.
fresh()
_rk, _pn, _md = _mount([("SWITCH 17", "ubiquiti-usw-24-poe")])
cv.is_active_tab = True
cv.scene_obj.clearSelection(); _rk.setSelected(True)
cv.duplicate_selected()
ok("racked equipment is renumbered with the rack",
   sorted(d.label for d in cv.get_network_devices()) == ["SWITCH 17", "SWITCH 18"])

print("\n== Dragging does not rebuild the Properties panel ==")
from PySide6.QtTest import QTest as _QT
fresh()
# Earlier checks leave other tools active; only the select tool drags items.
cv.set_tool("select")
# Away from the origin: the scene rect starts at 0,0, so an item placed there sits
# in the viewport corner where centerOn cannot centre it and the press misses.
_drag_target = cv.place_rack(QPointF(600, 400), label="Drag Rack", ru_height=12)
win.resize(1000, 700); win.show(); win.sheet_tabs.setCurrentWidget(cv)
app.processEvents(); _QT.qWait(20)
_rebuilds = {"n": 0}
_real_select = win.sidebar.select_item
def _counting(item):
    _rebuilds["n"] += 1
    return _real_select(item)
win.sidebar.select_item = _counting
cv.item_property_changed.disconnect()
cv.item_property_changed.connect(_counting)
cv.centerOn(_drag_target); app.processEvents()
_start = cv.mapFromScene(_drag_target.scenePos())
_before_x = _drag_target.pos().x()
_QT.mousePress(cv.viewport(), Qt.LeftButton, Qt.NoModifier, _start)
app.processEvents()
_rebuilds["n"] = 0
for _i in range(1, 21):
    _QT.mouseMove(cv.viewport(), _start + QPoint(_i, 0))
    app.processEvents()
_during = _rebuilds["n"]
_QT.mouseRelease(cv.viewport(), Qt.LeftButton, Qt.NoModifier, _start + QPoint(20, 0))
app.processEvents()
ok("the drag actually moved something", _drag_target.pos().x() != _before_x)
# Every pixel of movement used to rebuild the whole panel -- for a switch that meant
# re-walking the cable graph and re-running PoE validation, dozens of times a drag.
ok("no panel rebuilds mid-drag", _during == 0)
ok("exactly one rebuild on release", _rebuilds["n"] - _during == 1)
win.sidebar.select_item = _real_select
cv.item_property_changed.disconnect()
cv.item_property_changed.connect(lambda i: win.sidebar.select_item(i))
win.sheet_tabs.setCurrentIndex(0)

print("\n== Graph walks are memoized ==")
fresh()
_hub = cv.place_device(cat["ubiquiti-usw-24-poe"], QPointF(0, 0))
for _i in range(6):
    _leaf = cv.place_device(cat["ubiquiti-usw-flex"], QPointF(200, _i * 60))
    link(_hub, _leaf)
_sig = cv._topology_signature()
cv.build_network_topology()
ok("the topology cache is populated", cv.scene_obj._topology_cache[0] == _sig)
ok("a repeat call is served from it", cv.build_network_topology() is cv.scene_obj._topology_cache[1])
# Position is deliberately not part of the signature: moving a switch cannot change
# what it is plugged into, and re-keying on it would defeat the cache during a drag.
_hub.setPos(_hub.x() + 500, _hub.y() + 500)
ok("moving a device does not invalidate it", cv._topology_signature() == _sig)
link(_hub, cv.place_device(cat["ubiquiti-usw-flex"], QPointF(600, 0)))
ok("but a new cable does", cv._topology_signature() != _sig)

print("\n== Settings ==")
from src.ui import settings_dialog as _sd
from src.graphics.floorplan_item import FloorPlanItem as _FPI
fresh()
cv.scene_obj.scale_ratio = None
ok("with no plan and no calibration the scale is left alone",
   cv.suggested_icon_scale() == cv.icon_scale())

# Uncalibrated: fall back to the image's pixel dimensions.
cv.floorplan_item = _FPI(QPixmap(4000, 2000))
cv.scene_obj.addItem(cv.floorplan_item)
ok("an uncalibrated big plan suggests a bigger scale", cv.suggested_icon_scale() > 1.0)

# Calibrated: size the icon as a real object instead. Sizing off pixels only picks the
# right answer for a whole-plan view, so it reads as bloated the moment you zoom in --
# a real-world size holds its proportions against the rooms at every zoom.
cv.scene_obj.scale_ratio = 20.0   # px per foot
_expect = round((cv.ICON_TARGET_FEET * 20.0 / cv.NATURAL_DEVICE_WIDTH) * 4) / 4
ok("calibration wins over image size", cv.suggested_icon_scale() == _expect)
_with_small_plan = cv.suggested_icon_scale()
cv.scene_obj.removeItem(cv.floorplan_item)
cv.floorplan_item = _FPI(QPixmap(12000, 9000))
cv.scene_obj.addItem(cv.floorplan_item)
ok("and does not change with the image's resolution",
   cv.suggested_icon_scale() == _with_small_plan)
ok("a finer calibration asks for bigger icons",
   (cv.scene_obj.__setattr__("scale_ratio", 60.0), cv.suggested_icon_scale())[1] > _with_small_plan)
ok("the suggestion lands on a round quarter step",
   abs(cv.suggested_icon_scale() * 4 - round(cv.suggested_icon_scale() * 4)) < 1e-9)

cv.set_icon_scale(1.0)
_dlg = _sd.SettingsDialog(win)
_dlg.icon_slider.setValue(250)
ok("the slider previews live on the canvas", abs(cv.icon_scale() - 2.5) < 1e-9)
_dlg.reject()
ok("cancel puts the canvas back", abs(cv.icon_scale() - 1.0) < 1e-9)

_dlg = _sd.SettingsDialog(win)
_dlg.icon_slider.setValue(175)
_dlg.label_size_spin.setValue(13)
_dlg.splash_sound_chk.setChecked(False)
_dlg.accept()
ok("OK persists the icon scale", abs(_sd.get_float(_sd.KEY_ICON_SCALE) - 1.75) < 1e-9)
ok("OK persists the label size", _sd.get_int(_sd.KEY_LABEL_FONT_SIZE) == 13)
# QSettings hands some booleans back as the strings "true"/"false", and both are
# truthy -- a plain bool() read would turn every disabled setting back on.
ok("a False setting survives the round trip", _sd.get_bool(_sd.KEY_SPLASH_SOUND) is False)
ok("label size actually reached the renderer", label_render.label_font_size() == 13)

_dlg = _sd.SettingsDialog(win)
_dlg.restore_defaults()
_dlg.accept()
ok("Restore Defaults resets the scale", _sd.get_float(_sd.KEY_ICON_SCALE) == 1.0)
ok("Restore Defaults re-enables the chime", _sd.get_bool(_sd.KEY_SPLASH_SOUND) is True)
QSettings().clear()

print("\n== Undo snapshots must not re-encode the floor plan ==")
# Every mouse press captures an undo snapshot, and a snapshot is a full project
# serialization -- which embedded the floor plan by PNG-compressing it every time. On a
# 54-megapixel scan that was over half a second per click: the drag could not start
# until it finished, which is what "it takes a second to follow the mouse" was.
import time as _t2
from src.graphics.floorplan_item import FloorPlanItem as _FPI2
fresh()
_plan = QPixmap(2600, 2000)
_plan.fill(Qt.darkGray)
cv.floorplan_item = _FPI2(_plan)
cv.floorplan_item.filepath = "probe.png"
cv.scene_obj.addItem(cv.floorplan_item)

_first_t = _t2.perf_counter(); _first = win._encoded_floorplan(); _first_ms = (_t2.perf_counter() - _first_t) * 1000
_again_t = _t2.perf_counter(); _again = win._encoded_floorplan(); _again_ms = (_t2.perf_counter() - _again_t) * 1000
ok("the floor plan still gets embedded", bool(_first))
ok("a repeat call returns the same data", _again == _first)
ok("and does no work the second time", _again_ms < max(1.0, _first_ms / 10.0))

# A snapshot is what a mouse press takes; it must be cheap once the plan is encoded.
_snap_t = _t2.perf_counter(); _state = win._serialize_state(); _snap_ms = (_t2.perf_counter() - _snap_t) * 1000
ok("serializing carries the image", bool(_state.get("floorplan_image_base64")))
ok("and reuses the cached copy rather than re-encoding", _snap_ms < max(2.0, _first_ms / 5.0))
# Fifty snapshots sharing one string instead of fifty copies of the image.
ok("snapshots share the encoded image", win._serialize_state()["floorplan_image_base64"]
   is _state["floorplan_image_base64"])

# A different plan must not serve the old one out of the cache.
_plan2 = QPixmap(900, 700); _plan2.fill(Qt.white)
cv.scene_obj.removeItem(cv.floorplan_item)
cv.floorplan_item = _FPI2(_plan2)
cv.scene_obj.addItem(cv.floorplan_item)
ok("a newly loaded plan re-encodes", win._encoded_floorplan() != _first)
ok("no plan at all encodes nothing",
   (cv.scene_obj.removeItem(cv.floorplan_item), setattr(cv, "floorplan_item", None),
    win._encoded_floorplan())[2] is None)

# Seeding from a project's own embedded data skips the encode entirely on open.
cv.floorplan_item = _FPI2(_plan)
cv.scene_obj.addItem(cv.floorplan_item)
win._seed_floorplan_encoding(_first)
_seed_t = _t2.perf_counter(); _seeded = win._encoded_floorplan(); _seed_ms = (_t2.perf_counter() - _seed_t) * 1000
ok("a seeded cache is used as-is", _seeded == _first)
ok("so opening a project costs no encode", _seed_ms < max(1.0, _first_ms / 10.0))

print("\n== Returning a cable to the pitchfork ==")
from src.graphics.rack_elevation_items import CableStubItem as _Stub
fresh()
_rk4, _pn4, _md4 = _mount([("SW", "ubiquiti-usw-24-poe")])
_sw4 = _md4["SW"]
_cam4 = cv.place_camera(cat["ubiquiti-g5-bullet"], QPointF(300, 300))
_run4 = CableItem(); _run4.cable_type = "CAT6"
_run4.set_points([_cam4.pos(), _rk4.pos()], anchors=[_cam4.id, _rk4.id])
cv.scene_obj.addItem(_run4)
_pn4.refresh(); app.processEvents()
_stubs4 = lambda: [i for i in _pn4.elevation_view.scene_obj.items() if isinstance(i, _Stub)]
_pn4._terminate_stub(_stubs4()[0], _sw4.id, 4)
_pn4.refresh(); app.processEvents()
ok("the run renders as routed to a port", all(x.terminated for x in _stubs4()))

# Returning it while a drag is in flight is the case that broke: refresh() refused to
# rebuild (it would destroy the items the drag holds) and simply dropped the request,
# so the cable moved in the model but nothing redrew it until the tab was reopened.
_pn4.elevation_view._drag_candidates = [object()]
_pn4.return_stubs_to_pitchfork(_stubs4())
app.processEvents()
ok("the model returns it to the rack immediately",
   _run4.vertex_anchors[-1] == _rk4.id and _run4.end_port is None)
ok("the rebuild is queued, not dropped", _pn4._refresh_pending)
_pn4.elevation_view._drag_candidates = []
QTest.qWait(200); app.processEvents()
ok("and lands once the drag ends", not any(x.terminated for x in _stubs4()))
ok("the pending flag clears", not _pn4._refresh_pending)

# Overlapping requests must coalesce rather than queue a rebuild each.
_pn4.refresh_soon(); _pn4.refresh_soon(); _pn4.refresh_soon()
ok("repeat requests coalesce into one", _pn4._refresh_pending)
QTest.qWait(120); app.processEvents()
ok("and it still runs", not _pn4._refresh_pending)
win.sheet_tabs.setCurrentIndex(0)

print("\n== Follow-the-bundle cable routing ==")
def _trunked_floor():
    """MDF with a corridor trunk that dog-legs, plus the drop that trunk serves."""
    fresh()
    cv.set_icon_scale(1.0)
    cv.set_tool("cable")
    rack = cv.place_rack(QPointF(0, 0), label="MDF", ru_height=12)
    win.open_rack_tab(rack)
    panel = next(win.sheet_tabs.widget(i) for i in range(win.sheet_tabs.count())
                 if getattr(win.sheet_tabs.widget(i), "rack", None) is rack)
    panel.mount_new_device("ubiquiti-usw-24-poe")
    win.sheet_tabs.setCurrentIndex(0)
    end = cv.place_device(cat["generic-wall-drop-1port"], QPointF(600, 400))
    trunk = CableItem(); trunk.cable_type = "CAT6"
    trunk.set_points([end.pos(), QPointF(600, 200), QPointF(0, 200), rack.pos()],
                     anchors=[end.id, None, None, rack.id])
    cv.scene_obj.addItem(trunk)
    return rack, trunk

_rack_f, _trunk_f = _trunked_floor()
_office = cv.place_device(cat["generic-wall-drop-2port"], QPointF(750, 300))
cv.cable_draw_points = [_office.pos()]
_followed = cv.follow_bundle_path(QPointF(600, 260))
ok("a click on the trunk returns a route", bool(_followed))
ok("it starts at the junction, not the click", _followed[0] == QPointF(600, 260))
ok("it ends on the rack", _followed[-1] == _rack_f.pos())
ok("it reproduces the corridor dog-leg",
   [(p.x(), p.y()) for p in _followed] == [(600, 260), (600, 200), (0, 200), (0, 0)])
cv.cable_draw_points.extend(_followed)
cv.complete_cable_drawing()
_new = [c for c in cv.get_cables() if c is not _trunk_f][0]
ok("the finished run is anchored to the rack", _new.vertex_anchors[-1] == _rack_f.id)
ok("and to the office drop it started from", _new.vertex_anchors[0] == _office.id)
recalculate_all_cable_offsets(cv.scene_obj)
ok("it bundles with the trunk it followed",
   any(w > 1.0 for w in _new.segment_widths))

# Quick-drawn branches have to become a REAL bundle, not just lines lying on top of
# each other. Each Ctrl+click joins the trunk at its own point, so without splitting the
# followed run at that junction every branch's shared stretch starts mid-segment: the
# segments never match, and recalculate_all_cable_offsets treats them as unrelated solo
# runs that never thicken and never fan.
_rack_b, _trunk_b = _trunked_floor()
_branches = []
for _y in (300, 340):
    _off = cv.place_device(cat["generic-wall-drop-2port"], QPointF(800, _y))
    cv.cable_draw_points = [_off.pos()]
    cv.cable_draw_points.extend(cv.follow_bundle_path(QPointF(600, _y), split=True))
    cv.complete_cable_drawing()
    _branches.append([c for c in cv.get_cables() if c not in _branches and c is not _trunk_b][-1])

def _groups():
    """Shared segments, as {snapped key: [(cable, segment index), ...]}."""
    from collections import defaultdict as _dd
    g = _dd(list)
    step = 15.0 * cv.icon_scale()
    def snap(p):
        return (round(p.x() / step) * step, round(p.y() / step) * step)
    for c in cv.get_cables():
        for i in range(1, len(c.points)):
            a, b = snap(c.points[i - 1]), snap(c.points[i])
            g[(a, b) if a < b else (b, a)].append((c, i - 1))
    return g

_shared = {k: v for k, v in _groups().items() if len(v) > 1}
ok("a quick-drawn branch shares segments with the run it followed", bool(_shared))
ok("the trunk gains a vertex at each junction", len(_trunk_b.points) >= 5)
ok("branches bundle with each other, not just with the trunk",
   any(len(v) >= 3 for v in _shared.values()))

# The stretch carrying one branch plus the trunk has to fan as a pair when hovered.
_pairs = [(k, v) for k, v in _shared.items() if len(v) == 2]
ok("the two-cable stretch is recognised as a bundle", bool(_pairs))
if _pairs:
    (_a, _b), _members = _pairs[0]
    cv.scene_obj.view_lod = 1.0
    _recalc(cv.scene_obj, hover_pt=QPointF((_a[0] + _b[0]) / 2.0, (_a[1] + _b[1]) / 2.0))
    _offsets = [(c.draw_points[i].x() - c.points[i].x(),
                 c.draw_points[i].y() - c.points[i].y()) for c, i in _members]
    _sep = max(abs(_offsets[0][0] - _offsets[1][0]), abs(_offsets[0][1] - _offsets[1][1]))
    ok("and the two-cable stretch fans when hovered", _sep > 1.0)
    _recalc(cv.scene_obj, hover_pt=QPointF(_a[0] + 4000, _a[1] + 4000))
    ok("and merges again when the cursor leaves",
       all(abs(c.draw_points[i].x() - c.points[i].x()) < 0.01
           and abs(c.draw_points[i].y() - c.points[i].y()) < 0.01 for c, i in _members))

# Direction: clicking the stretch BEYOND the junction, further from the rack, must
# still route toward the rack rather than away down the corridor.
_rack_f, _trunk_f = _trunked_floor()
_office = cv.place_device(cat["generic-wall-drop-2port"], QPointF(750, 380))
cv.cable_draw_points = [_office.pos()]
_far = cv.follow_bundle_path(QPointF(600, 380))
ok("a click near the far end still heads for the rack", _far[-1] == _rack_f.pos())

# Chaining: an office run that dies loose on the corridor trunk should be followed
# onto that trunk and onward, not stop at its own dead end.
_rack_f, _trunk_f = _trunked_floor()
_spur_drop = cv.place_device(cat["generic-wall-drop-1port"], QPointF(900, 300))
_spur = CableItem(); _spur.cable_type = "CAT6"
_spur.set_points([_spur_drop.pos(), QPointF(600, 300)], anchors=[_spur_drop.id, None])
cv.scene_obj.addItem(_spur)
_office = cv.place_device(cat["generic-wall-drop-2port"], QPointF(950, 340))
cv.cable_draw_points = [_office.pos()]
_chained = cv.follow_bundle_path(QPointF(800, 300))   # click the spur, not the trunk
ok("following a spur chains onto the trunk", _chained[-1] == _rack_f.pos())
ok("and passes through the hand-off point",
   any(abs(p.x() - 600) < 1 and abs(p.y() - 300) < 1 for p in _chained))

# A run that reaches no rack still lays its path and leaves the end loose.
fresh()
cv.set_tool("cable")
_a2 = cv.place_device(cat["generic-wall-drop-1port"], QPointF(0, 0))
_loose = CableItem(); _loose.cable_type = "CAT6"
_loose.set_points([_a2.pos(), QPointF(300, 0)], anchors=[_a2.id, None])
cv.scene_obj.addItem(_loose)
cv.cable_draw_points = [QPointF(150, 100)]
_dead = cv.follow_bundle_path(QPointF(150, 0))
ok("a bundle that reaches nothing still returns a path", bool(_dead))
ok("and it terminates rather than looping", len(_dead) <= cv.FOLLOW_MAX_HOPS * 4)

# Two cables lying on each other must not send the walk round in circles.
fresh()
cv.set_tool("cable")
_l1 = CableItem(); _l1.cable_type = "CAT6"
_l1.set_points([QPointF(0, 0), QPointF(200, 0)], anchors=[None, None])
cv.scene_obj.addItem(_l1)
_l2 = CableItem(); _l2.cable_type = "CAT6"
_l2.set_points([QPointF(200, 0), QPointF(0, 0)], anchors=[None, None])
cv.scene_obj.addItem(_l2)
cv.cable_draw_points = [QPointF(100, 80)]
_looped = cv.follow_bundle_path(QPointF(100, 0))
ok("a cycle in the cable plant terminates", _looped is not None)

# Ctrl+click on empty space is an ordinary waypoint, not a surprise.
ok("a click that misses every cable follows nothing",
   cv.follow_bundle_path(QPointF(5000, 5000)) is None)

# The preview is feedback only -- it must never leave anything behind.
cv.cable_draw_points = [QPointF(100, 80)]
cv.update_follow_preview(QPointF(100, 0))
ok("the preview draws while it has a route", cv.follow_preview_line is not None)
cv.update_follow_preview(QPointF(5000, 5000))
ok("and clears itself when the route goes away", cv.follow_preview_line is None)
cv.update_follow_preview(QPointF(100, 0))
cv.cleanup_temp_shapes()
ok("cleanup removes the preview", cv.follow_preview_line is None)
cv.cable_draw_points = []
cv.set_tool("select")

print("\n== The follow preview must not touch the design ==")
# update_follow_preview runs on every mouse-move while Ctrl is held. It used to call
# the same splitting code the commit does, so hesitating over the trunk before landing
# a branch carved a fresh vertex into it for every pixel the cursor travelled.
_rack_p, _trunk_p = _trunked_floor()
_office_p = cv.place_device(cat["generic-wall-drop-2port"], QPointF(800, 500))
cv.cable_draw_points = [_office_p.pos()]
_before_pts = len(_trunk_p.points)
for _sweep in range(4):
    for _y in range(220, 700, 10):
        cv.update_follow_preview(QPointF(600, _y if _sweep % 2 == 0 else 920 - _y))
ok("waggling the cursor adds no vertices", len(_trunk_p.points) == _before_pts)
ok("the preview still drew a route", cv.follow_preview_line is not None)

# It must nevertheless describe exactly the route the commit produces.
_preview_route = cv.follow_bundle_path(QPointF(600, 300))
cv.cable_draw_points.extend(cv.follow_bundle_path(QPointF(600, 300), split=True))
cv.complete_cable_drawing()
_committed = [c for c in cv.get_cables() if c is not _trunk_p][0]
ok("committing adds exactly one vertex", len(_trunk_p.points) == _before_pts + 1)
ok("the preview matched what was committed",
   [(p.x(), p.y()) for p in _preview_route]
   == [(p.x(), p.y()) for p in _committed.points[1:]])
cv.cable_draw_points = []
cv.cleanup_temp_shapes()
cv.set_tool("select")

print("\n== Undo restores the design, not the viewport ==")
fresh()
cv.scene_obj.view_lod = 1.0
from src.graphics.floorplan_item import FloorPlanItem as _FPI3
_plan3 = QPixmap(3000, 2400); _plan3.fill(Qt.darkGray)
cv.floorplan_item = _FPI3(_plan3)
cv.floorplan_item.filepath = "plan.png"
cv.scene_obj.addItem(cv.floorplan_item)
cv.scene_obj.setSceneRect(cv.floorplan_item.boundingRect())
win._seed_floorplan_encoding(win._encoded_floorplan())
_victim = cv.place_device(cat["ubiquiti-usw-24-poe"], QPointF(1500, 1200))
win.resize(1100, 800); win.show(); app.processEvents()

cv.zoom_fit(); cv.scale(4.0, 4.0); cv.centerOn(QPointF(1500, 1200))
app.processEvents()
_zoom0 = cv.transform().m11()
_h0, _v0 = cv.horizontalScrollBar().value(), cv.verticalScrollBar().value()

cv.is_active_tab = True
cv.scene_obj.clearSelection(); _victim.setSelected(True)
cv.delete_selected(); app.processEvents()
_t0 = _t2.perf_counter()
win.undo_manager.undo()
_undo_ms = (_t2.perf_counter() - _t0) * 1000
app.processEvents()

ok("undo brings the device back",
   any(d.label == _victim.label for d in cv.get_network_devices()))
ok("undo leaves the zoom alone", abs(cv.transform().m11() - _zoom0) < 1e-9)
ok("undo leaves the frame alone",
   cv.horizontalScrollBar().value() == _h0 and cv.verticalScrollBar().value() == _v0)
ok("the floor plan survives undo", cv.floorplan_item is not None
   and cv.floorplan_item.pixmap().width() == 3000)
# Reusing the decoded pixmap instead of decoding the embedded copy again is what keeps
# undo instant on a large plan.
ok("undo does not re-decode the plan", _undo_ms < 250)

# Opening a project still frames it, rather than keeping wherever you happened to be.
_state_open = win._serialize_state()
cv.scale(3.0, 3.0); app.processEvents()
_zoom_wrong = cv.transform().m11()
win._replace_scene_state(_state_open)   # undo path: keeps the view
ok("undo/redo keep the view", abs(cv.transform().m11() - _zoom_wrong) < 1e-9)

print("\n== Auto-numbering ==")
fresh()
cv.set_icon_scale(1.0)
# Numbering used to come from the TOTAL device count, so the first wall drop dropped
# onto a design that already had other equipment was christened "Wall Drop 7".
for _i in range(6):
    cv.place_device(cat["ubiquiti-usw-24-poe"], QPointF(_i * 40, 0))
_d1 = cv.place_device(cat["generic-wall-drop-2port"], QPointF(0, 200))
ok("the first wall drop is Wall Drop 1", _d1.label == "Wall Drop 1")
cv.is_active_tab = True
cv.scene_obj.clearSelection(); _d1.setSelected(True)
cv.duplicate_selected()
ok("its duplicate is Wall Drop 2",
   sorted(d.label for d in cv.get_network_devices() if d.label.startswith("Wall Drop"))
   == ["Wall Drop 1", "Wall Drop 2"])
_d3 = cv.place_device(cat["generic-wall-drop-2port"], QPointF(0, 300))
ok("the next placement continues the run", _d3.label == "Wall Drop 3")

# A gap left by a deletion gets filled rather than skipped, and a count-based name
# would have collided with the survivor.
cv.scene_obj.clearSelection(); _d1.setSelected(True)
cv.delete_selected()
_d4 = cv.place_device(cat["generic-wall-drop-2port"], QPointF(0, 400))
ok("a freed number is reused", _d4.label == "Wall Drop 1")

fresh()
_c1 = cv.place_camera(cat["ubiquiti-g5-bullet"], QPointF(0, 0))
_c2 = cv.place_camera(cat["ubiquiti-g5-bullet"], QPointF(40, 0))
_c3 = cv.place_camera(cat["ubiquiti-g5-bullet"], QPointF(80, 0))
cv.scene_obj.clearSelection(); _c2.setSelected(True)
cv.delete_selected()
ok("camera numbering does not collide after a delete",
   cv.place_camera(cat["ubiquiti-g5-bullet"], QPointF(120, 0)).label == "Camera 2")

fresh()
cv.place_device(cat["ubiquiti-usw-24-poe"], QPointF(0, 0))
# _mount renames what it mounts, so go through the panel directly to see the auto-name
# the Rack Editor actually assigns -- it had the same total-count bug.
_rk2 = cv.place_rack(QPointF(300, 0), label="R", ru_height=12)
win.open_rack_tab(_rk2)
_pn2 = next(win.sheet_tabs.widget(i) for i in range(win.sheet_tabs.count())
            if getattr(win.sheet_tabs.widget(i), "rack", None) is _rk2)
_pn2.mount_new_device("generic-patch-panel-24")
ok("rack-mounted equipment numbers per type too",
   [s2.device.label for s2 in _rk2.slots] == ["PATCH-PANEL 1"])
win.sheet_tabs.setCurrentIndex(0)

print("\n== Bounding rects cover what gets painted ==")
# SmartViewportUpdate only repaints the region an item declares. Labels and warning
# badges draw at a constant on-screen size, so their footprint in scene units grows as
# you zoom out -- if boundingRect stops at the icon, Qt never repaints where the label
# was and a dragged item smears a trail of stale labels behind it.
from PySide6.QtWidgets import QStyleOptionGraphicsItem as _Opt
from PySide6.QtGui import QImage as _Img, QPainter as _Pnt
fresh()
_probe_items = {
    "device": cv.place_device(cat["ubiquiti-usw-24-poe"], QPointF(0, 0)),
    "drop": cv.place_device(cat["generic-wall-drop-2port"], QPointF(0, 0)),
    "camera": cv.place_camera(cat["ubiquiti-g5-bullet"], QPointF(0, 0)),
    "rack": cv.place_rack(QPointF(0, 0), label="Rack", ru_height=12),
}
for _nm, _it in _probe_items.items():
    _it.label = f"{_nm.title()} With A Realistically Long Name"

def _paints_inside(item, lod):
    cv.scene_obj.view_lod = lod
    rect = item.boundingRect()
    img = _Img(1200, 1200, _Img.Format_ARGB32)
    img.fill(Qt.transparent)
    p = _Pnt(img)
    p.translate(600, 600)
    p.scale(lod, lod)
    item.paint(p, _Opt(), None)
    p.end()
    for y in range(0, img.height(), 4):
        for x in range(0, img.width(), 4):
            if (img.pixel(x, y) >> 24) & 0xFF:
                if not rect.contains((x - 600) / lod, (y - 600) / lod):
                    return False
    return True

_leaks = [f"{n}@{l}" for l in (1.0, 0.3, 0.1)
          for n, i in _probe_items.items() if not _paints_inside(i, l)]
ok("nothing paints outside its bounding rect, at any zoom", not _leaks)
if _leaks:
    print("   leaked:", _leaks)
cv.scene_obj.view_lod = 1.0

print("\n== Drag-path costs ==")
fresh()
_hub2 = cv.place_device(cat["ubiquiti-usw-24-poe"], QPointF(0, 0))
_runs = [link(cv.place_device(cat["generic-wall-drop-1port"], QPointF(200, _i * 40)), _hub2)
         for _i in range(12)]
from src.graphics import cable_item as _ci
recalculate_all_cable_offsets(cv.scene_obj)
_repaints = {"n": 0}
_real_update = _ci.CableItem.update_path
def _counting_update(self):
    _repaints["n"] += 1
    return _real_update(self)
_ci.CableItem.update_path = _counting_update
recalculate_all_cable_offsets(cv.scene_obj)      # nothing moved
_idle = _repaints["n"]
_hub2.setPos(_hub2.x() + 30, _hub2.y())
sync_anchored_vertices(cv.scene_obj)
recalculate_all_cable_offsets(cv.scene_obj)      # one device moved
_after_move = _repaints["n"] - _idle
_ci.CableItem.update_path = _real_update
# Every cable used to be handed a fresh path on every mouse-move, which makes Qt
# re-index and repaint it -- most of the cost of dragging on a real design.
ok("an unchanged scene repaints no cables", _idle == 0)
ok("moving a device repaints only its own runs", 0 < _after_move <= len(_runs))

# The anchor lookup used to rescan the whole scene once per anchor.
_rk3, _pn3, _made3 = _mount([("SW", "ubiquiti-usw-24-poe")])
_index = anchor_target_index(cv.scene_obj)
ok("the anchor index finds loose items", _hub2.id in _index)
ok("and equipment mounted inside racks", _made3["SW"].id in _index)
ok("and the rack itself", _rk3.id in _index)

print("\n== One bundle open at a time ==")
# Fanning proportionally to cursor distance meant the cables squirmed away as you
# approached and closed as you leaned in -- you chased the thing you were inspecting.
fresh()
cv.scene_obj.view_lod = 1.0
cv.set_icon_scale(1.0)      # fan span follows the icon scale; pin it so distances are known
_hubL = cv.place_device(cat["ubiquiti-usw-24-poe"], QPointF(0, 0))
def _corridor(x, tag):
    runs = []
    way = QPointF(x, 600)
    for i in range(3):
        d = cv.place_device(cat["generic-wall-drop-1port"], QPointF(x + 100 + i * 40, 900))
        c = CableItem(); c.cable_type = "CAT6"
        c.set_points([d.pos(), way, QPointF(x, 100)], anchors=[d.id, None, None])
        c.label = f"{tag}{i}"
        cv.scene_obj.addItem(c); runs.append(c)
    return runs
_A = _corridor(0, "A")
_B = _corridor(900, "B")
def _spread(runs):
    xs = [(c.draw_points or c.points)[1].x() for c in runs]
    return max(xs) - min(xs)
def _hover(pt):
    _recalc(cv.scene_obj, hover_pt=pt)

_hover(QPointF(450, 300))
ok("nothing is open to start with", _spread(_A) == 0 and _spread(_B) == 0)
_hover(QPointF(0, 300))
_open_span = _spread(_A)
ok("hovering a bundle opens it", _open_span > 0)
ok("and only that one", _spread(_B) == 0)

# Fully open, not a function of how close you got -- that is the whole point.
_hover(QPointF(20, 300))
ok("it opens all the way, not proportionally", abs(_spread(_A) - _open_span) < 0.01)

# It has to stay open well past the radius that opened it, or reading it is impossible:
# the cables themselves have moved aside by half the fan span.
# The real requirement: once open, the cursor can rest on any cable in the fan -- the
# outermost has moved half the span off the original centreline -- without it shutting.
_hover(QPointF(_open_span / 2.0, 300))
ok("it stays open out where its own cables now are",
   abs(_spread(_A) - _open_span) < 0.01)
_hover(QPointF(1200, 300))
ok("it closes once you are clearly away", _spread(_A) == 0)

# Moving from one bundle straight into another hands over rather than opening both.
_hover(QPointF(900, 300))
ok("a second bundle opens when entered", _spread(_B) > 0)
_hover(QPointF(0, 300))
ok("entering the first hands over", _spread(_A) > 0 and _spread(_B) == 0)

# A recalc with no cursor information (a drag, an icon-scale change) must not slam it
# shut -- it knows nothing about where the pointer is.
_recalc(cv.scene_obj, hover_pt=None)
ok("a passing recalc leaves it open", _spread(_A) > 0)

# Leaving the select tool does close it, though.
cv.set_tool("cable")
ok("switching tools closes it", cv.scene_obj.open_bundle is None and _spread(_A) == 0)
cv.set_tool("select")

# Dynamic mode restores the original proportional feel, for anyone who preferred it.
cv.scene_obj.fanout_mode = "dynamic"
_hover(QPointF(0, 300))
_dyn_on = _spread(_A)
_hover(QPointF(18, 300))
_dyn_off = _spread(_A)
ok("dynamic mode still fans", _dyn_on > 0)
ok("dynamic mode is proportional to distance", 0 < _dyn_off < _dyn_on)
_hover(QPointF(500, 300))
ok("dynamic mode closes with distance", _spread(_A) == 0)
ok("dynamic mode latches nothing", cv.scene_obj.open_bundle is None)
cv.scene_obj.fanout_mode = "latched"
_hover(QPointF(0, 300))
_hover(QPointF(18, 300))
ok("latched mode ignores how close you got", abs(_spread(_A) - _open_span) < 0.01)
_hover(QPointF(5000, 5000))

print("\n== Label collision avoidance ==")
from src.graphics import label_layout as _ll
fresh()
cv.set_icon_scale(1.0)
_row = []
for _i in range(5):
    _d5 = cv.place_device(cat["generic-wall-drop-2port"], QPointF(_i * 60, 0))
    _d5.label = f"Wall Drop {_i + 1}"
    _row.append(_d5)

def _collisions():
    lod = cv.scene_obj.view_lod
    rects = [_ll.label_rect(d, lod, d.label_row) for d in _row]
    return sum(1 for a in range(len(rects)) for b in range(a + 1, len(rects))
               if rects[a].intersects(rects[b]))

cv.scene_obj.view_lod = 0.25
for _d5 in _row:
    _d5.label_row = 0
ok("labels collide when nothing is done about it", _collisions() > 0)
cv.relayout_labels()
ok("stacking clears the collisions", _collisions() == 0)
ok("the first label keeps its natural spot", _row[0].label_row == 0)
ok("and each label is on its own row", len({d.label_row for d in _row}) == len(_row))
# Both directions, so a crowd packs around its items instead of growing a tail.
ok("labels go above as well as below", any(d.label_row < 0 for d in _row))
ok("and none strays further than the cap",
   all(abs(d.label_row) <= _ll.MAX_ROWS for d in _row))

# Zoom back in and they must settle, not stay stacked forever.
cv.scene_obj.view_lod = 3.0
cv.relayout_labels()
ok("zooming in unstacks them", all(d.label_row == 0 for d in _row))
ok("with no collisions either", _collisions() == 0)

# A lone label is never pushed anywhere.
fresh()
cv.scene_obj.view_lod = 0.1
_solo = cv.place_device(cat["generic-wall-drop-2port"], QPointF(0, 0))
_solo.label = "All By Myself"
cv.relayout_labels()
ok("a label with nothing near it stays put", _solo.label_row == 0)

# The drawn label and the rect Qt repaints have to move together, or a stacked label
# smears -- the same class of bug as the bounding-rect work earlier.
fresh()
cv.scene_obj.view_lod = 0.3
_stack = []
for _i in range(3):
    _d6 = cv.place_device(cat["generic-wall-drop-2port"], QPointF(_i * 40, 0))
    _d6.label = f"Stacked Drop {_i + 1}"
    _stack.append(_d6)
cv.relayout_labels()
_pushed = [d for d in _stack if d.label_row > 0]
ok("something actually got pushed down", bool(_pushed))
for _d6 in _pushed:
    _rect = _d6.boundingRect()
    _base = _d6.height / 2 + 2.0
    _drawn = label_render.label_anchor_y(_d6, _base)
    ok(f"the bounding rect covers the moved label ({_d6.label})",
       _rect.top() <= _drawn <= _rect.bottom())
ok("a pushed label paints inside its own rect",
   all(_paints_inside(d, cv.scene_obj.view_lod) for d in _pushed))
cv.scene_obj.view_lod = 1.0

print("\n== Cable allowances (vertical rise + service loop) ==")
from src.core import cable_length as _cl
_cl.clear_defaults()
fresh()
cv.scene_obj.scale_ratio = 10.0          # 10 px per foot
_mdf = cv.place_rack(QPointF(0, 0), label="MDF", ru_height=12)
_wd = cv.place_device(cat["generic-wall-drop-2port"], QPointF(1500, 0))   # 150 ft across
_run = CableItem(); _run.cable_type = "CAT6"
_run.set_points([_mdf.pos(), _wd.pos()], anchors=[_mdf.id, _wd.id])
cv.scene_obj.addItem(_run)

_h, _v, _l, _tot = _run.length_breakdown()
ok("the plan still measures the horizontal run", abs(_h - 150.0) < 0.01)
# Rack 10 up + drop 10 down; rack 10 ft of loop + drop 5.
ok("each end adds its vertical rise", abs(_v - 20.0) < 0.01)
ok("each end adds its service loop", abs(_l - 15.0) < 0.01)
ok("the total is what actually gets pulled", abs(_tot - 185.0) < 0.01)
ok("get_length_feet reports the real pull", abs(_run.get_length_feet() - 185.0) < 0.01)
ok("horizontal_feet still reports the flat measure", abs(_run.horizontal_feet() - 150.0) < 0.01)

# Blank means inherit; typing a number pins just that object.
ok("a fresh object inherits its category default", _cl.is_inherited(_wd, "vertical_rise"))
_wd.vertical_rise = 22.0
ok("an override takes effect", abs(_run.get_length_feet() - 197.0) < 0.01)
ok("and is no longer inherited", not _cl.is_inherited(_wd, "vertical_rise"))
_wd.vertical_rise = None
ok("clearing it returns to the default", abs(_run.get_length_feet() - 185.0) < 0.01)

# Changing the default moves everything still following it, and nothing that isn't.
_wd.service_loop = 3.0
_cl.set_defaults("drop", 12.0, 99.0)
ok("an untouched value follows the new default",
   abs(_cl.rise_feet(_wd) - 12.0) < 0.01)
ok("an overridden one does not", abs(_cl.loop_feet(_wd) - 3.0) < 0.01)
_cl.clear_defaults()
_wd.service_loop = None

# A run to nothing has no endpoint to charge for.
_free = CableItem(); _free.cable_type = "CAT6"
_free.set_points([_mdf.pos(), QPointF(1000, 500)], anchors=[_mdf.id, None])
cv.scene_obj.addItem(_free)
_fh, _fv, _fl, _ft2 = _free.length_breakdown()
ok("a free cable end costs nothing", abs(_fv - _cl.rise_feet(_mdf)) < 0.01
   and abs(_fl - _cl.loop_feet(_mdf)) < 0.01)

# Patch cables are cut to fit in the rack, not pulled through a pathway.
_jump = CableItem(); _jump.cable_type = "Patch"
_jump.set_points([_mdf.pos(), _mdf.pos()], anchors=[_mdf.id, _mdf.id])
cv.scene_obj.addItem(_jump)
ok("patch jumpers get no allowance", _jump.length_breakdown()[1:3] == (0.0, 0.0))

# The maximum-run check has to see the real cable, not the flat measure.
_run.set_points([_mdf.pos(), QPointF(3150, 0)], anchors=[_mdf.id, _wd.id])   # 315 ft
ok("a run under 328 ft flat but over it in reality is flagged",
   _run.horizontal_feet() < 328 and _run.get_length_feet() > 328 and _run.is_over_limit())

# Uncalibrated projects have no feet at all, and must not pretend otherwise.
cv.scene_obj.scale_ratio = None
ok("no calibration means no length", _run.length_breakdown() is None
   and _run.get_length_feet() is None)
cv.scene_obj.scale_ratio = 10.0

# Category routing: a rack, a camera and an AP must not share one bucket.
ok("categories resolve per object type",
   _cl.category_of(_mdf) == "rack" and _cl.category_of(_wd) == "drop"
   and _cl.category_of(cv.place_camera(cat["ubiquiti-g5-bullet"], QPointF(0, 900))) == "camera"
   and _cl.category_of(cv.place_device(cat["ubiquiti-u6-plus"], QPointF(200, 900))) == "access-point")

# Both fields have to survive a save/load round trip -- including None, which is the
# difference between "follows the default" and "pinned to today's default".
_wd.vertical_rise = 17.5
_wd.service_loop = None
_state_cl = win._serialize_state()
win._replace_scene_state(_state_cl)
_wd2 = next(d for d in cv.get_network_devices() if d.label == _wd.label)
ok("an override survives save/load", _wd2.vertical_rise == 17.5)
ok("an inherited value stays inherited", _wd2.service_loop is None)
_rack2 = cv.get_racks()[0]
ok("racks carry the fields too",
   hasattr(_rack2, "vertical_rise") and hasattr(_rack2, "service_loop"))
_cl.clear_defaults()

print("\n== A selected cable is visible on any background ==")
# A white "selected" line over a white floor plan was invisible. Tinting it to the
# inverse of what is underneath does not fix that: inverting mid grey gives back the
# same grey, and inverting a saturated mid-tone shifts the hue while leaving the
# luminance -- which is what a thin line is read by -- nearly unchanged. The casing
# gives the line its own contrast instead of borrowing the background's.
from src.graphics.floorplan_item import FloorPlanItem as _FPI4
from PySide6.QtGui import QPixmap as _QPm, QColor
from PySide6.QtCore import QRectF

def _luminance(c):
    return 0.2126 * c.redF() + 0.7152 * c.greenF() + 0.0722 * c.blueF()

def _cable_contrast(bg_hex):
    fresh()
    cv.scene_obj.view_lod = 1.0
    _pm = _QPm(600, 200); _pm.fill(Qt.white if bg_hex == "#ffffff" else QColor(bg_hex))
    cv.floorplan_item = _FPI4(_pm); cv.floorplan_item.setZValue(-100)
    cv.scene_obj.addItem(cv.floorplan_item)
    _s1 = cv.place_device(cat["ubiquiti-usw-24-poe"], QPointF(60, 100))
    _s2 = cv.place_device(cat["generic-wall-drop-2port"], QPointF(540, 100))
    _cc = CableItem(); _cc.cable_type = "CAT6"
    _cc.set_points([_s1.pos(), _s2.pos()], anchors=[_s1.id, _s2.id])
    cv.scene_obj.addItem(_cc)
    cv.scene_obj.clearSelection(); _cc.setSelected(True)
    _im = _Img(600, 200, _Img.Format_ARGB32); _im.fill(QColor(bg_hex))
    _pp = _Pnt(_im); _pp.setRenderHint(_Pnt.Antialiasing)
    cv.scene_obj.render(_pp, QRectF(_im.rect()), QRectF(0, 0, 600, 200))
    _pp.end()
    # Look down a column the cable crosses, well clear of both end icons.
    _bg = _luminance(QColor(bg_hex))
    _worst = max(abs(_luminance(QColor(_im.pixel(300, y))) - _bg) for y in range(80, 120))
    return _worst

for _bg, _name in (("#ffffff", "white paper"), ("#808080", "mid grey"),
                   ("#3b82f6", "blueprint blue"), ("#0a0a0f", "dark canvas")):
    _delta = _cable_contrast(_bg)
    ok(f"a selected cable stands out on {_name}", _delta > 0.25)

# ...and the casing is for the SELECTED run only. Casing every cable thickened the
# whole layer and made a busy plan look bloated, for contrast an ordinary run does not
# need. Measured off what actually gets painted, not off the flag that drives it.
fresh()
cv.scene_obj.view_lod = 1.0
_ca = cv.place_device(cat["ubiquiti-usw-24-poe"], QPointF(0, 0))
_cb = cv.place_device(cat["generic-wall-drop-2port"], QPointF(400, 0))
_cc2 = CableItem(); _cc2.cable_type = "CAT6"
_cc2.set_points([_ca.pos(), _cb.pos()], anchors=[_ca.id, _cb.id])
cv.scene_obj.addItem(_cc2)

def _drawn_thickness():
    _i2 = _Img(500, 120, _Img.Format_ARGB32); _i2.fill(Qt.transparent)
    _p2 = _Pnt(_i2); _p2.setRenderHint(_Pnt.Antialiasing); _p2.translate(50, 60)
    _cc2.paint(_p2, _Opt(), None); _p2.end()
    _col = [y for y in range(120) if (_i2.pixel(250, y) >> 24) & 0xFF]
    return (max(_col) - min(_col) + 1) if _col else 0

cv.scene_obj.clearSelection()
_thin = _drawn_thickness()
_cc2.setSelected(True)
_thick = _drawn_thickness()
ok("an unselected cable draws no casing", _thin < _thick)
ok("and stays a thin line", _thin <= 3)
ok("a selected cable is visibly heavier", _thick >= _thin + 2)
cv.scene_obj.clearSelection()
cv.scene_obj.view_lod = 1.0

print("\n== Rapid placement (item stays in hand) ==")
fresh()
cv.set_tool("infra", cat["generic-wall-drop-2port"])
ok("arming puts a ghost on the cursor", cv.hand_preview is not None)
# The ghost sits in the scene, so it must be invisible to everything that reads the
# design -- inventory, topology, PoE. Its object_type is one nothing recognises.
ok("the ghost is not a device", cv.hand_preview not in cv.get_network_devices())
ok("nor a camera", cv.hand_preview not in cv.get_cameras())
ok("nor in the topology",
   not any(getattr(n["device"], "object_type", "") == "hand_preview"
           for n in cv.build_network_topology()))
ok("nor labelled", cv.hand_preview.label == "")
ok("nor serialized",
   not any(d.get("spec_id") is None for d in win._serialize_state()["devices"]))

for _i in range(4):
    cv.place_from_hand(QPointF(_i * 120, 0))
ok("four clicks place four devices", len(cv.get_network_devices()) == 4)
ok("the tool is still armed", cv.active_tool == "infra")
ok("with the same item in hand",
   cv.selected_catalog_spec["id"] == "generic-wall-drop-2port")
ok("and numbering continues",
   sorted(d.label for d in cv.get_network_devices())
   == ["Wall Drop 1", "Wall Drop 2", "Wall Drop 3", "Wall Drop 4"])

# The ghost follows whatever is armed, and swaps when you pick something else.
cv.set_tool("camera", cat["ubiquiti-g5-bullet"])
ok("swapping the item swaps the ghost",
   cv.hand_preview is not None and cv.hand_preview.preview_spec_id == "ubiquiti-g5-bullet")
_cam_placed = cv.place_from_hand(QPointF(500, 0))
ok("a camera in hand places a camera", _cam_placed in cv.get_cameras())

# Escape is the way out, and it must take the ghost with it.
cv.keyPressEvent(_QKE(_QEv.KeyPress, Qt.Key_Escape, Qt.NoModifier))
ok("Escape disarms the tool", cv.active_tool == "select")
ok("and clears the ghost", cv.hand_preview is None)

# Dropping a switch onto a rack still mounts it rather than laying it alongside.
fresh()
_rk5 = cv.place_rack(QPointF(0, 0), label="Rack", ru_height=12)
cv.set_tool("infra", cat["ubiquiti-usw-24-poe"])
cv.place_from_hand(_rk5.pos())
ok("a switch dropped on a rack mounts inside it", len(_rk5.slots) == 1)
ok("and does not also sit loose on the plan",
   not [d for d in cv.scene_obj.items() if getattr(d, "object_type", None) == "device"])
cv.set_tool("select")

# The path that actually gets used: pick from the Place tool's menu. It used to drop
# you back to Select the moment you chose something, so nothing was ever in hand and
# there was never a ghost to see.
fresh()
cv._place_from_menu(cv.place_device, cat["generic-wall-drop-2port"], QPointF(0, 0))
ok("picking from the menu places it", len(cv.get_network_devices()) == 1)
ok("and leaves it in hand", cv.selected_catalog_spec["id"] == "generic-wall-drop-2port")
ok("with the place tool still armed", cv.active_tool == "place")
ok("and a ghost on the cursor", cv.hand_preview is not None)

# A ghost draws no label and no badge, so it must not reserve room for them -- that gave
# it a bounding rect thousands of units across, dirtying a huge region every mouse move.
cv.scene_obj.view_lod = 0.01
_gr = cv.hand_preview.boundingRect()
ok("the ghost's bounding rect stays small at any zoom",
   _gr.width() < 400 and _gr.height() < 400)
cv.scene_obj.view_lod = 1.0

# It has to actually paint something, or "in hand" is invisible.
_gimg = _Img(300, 300, _Img.Format_ARGB32); _gimg.fill(Qt.transparent)
_gp = _Pnt(_gimg); _gp.translate(150, 150)
cv.hand_preview.paint(_gp, _Opt(), None)
_gp.end()
ok("the ghost paints visible pixels",
   any((_gimg.pixel(x, y) >> 24) & 0xFF
       for y in range(0, 300, 3) for x in range(0, 300, 3)))

# ...and paints entirely INSIDE that rect, at every zoom. It draws no label, so an
# empty label must not paint its backdrop either -- that pill sat below the ghost's
# bounds and smeared a grey trail behind the cursor as it moved.
_trail = [l for l in (1.0, 0.3, 0.1) if not _paints_inside(cv.hand_preview, l)]
ok("the ghost never paints outside its bounds", not _trail)
if _trail:
    print("   leaked at zoom:", _trail)
cv.scene_obj.view_lod = 1.0
cv.set_tool("select")

print("\n== Super Mega Death Laser ==")
# A deliberately impossible load: 1000 W over PoE+, which delivers at most 25.5 W. It
# exists to be silly, but nothing about it is special-cased -- the value of it as a test
# is that the power checks catch it on their own, the same way they would a real
# over-spec device somebody actually tried to install.
_laser_spec = cat["generic-misc-death-laser"]
ok("the laser is in the misc catalog", _laser_spec.get("category") == "misc")
ok("it draws an absurd 1000 W", _laser_spec.get("maxPower") == 1000)
ok("over PoE+, which cannot deliver that", "802.3at" in _laser_spec.get("poeInput", ""))

fresh()
cv.scene_obj.scale_ratio = 10.0
_sw_l = cv.place_device(cat["ubiquiti-usw-24-poe"], QPointF(0, 0)); _sw_l.label = "Switch"
_laser = cv.place_device(_laser_spec, QPointF(200, 0))
_lc = link(_sw_l, _laser, 5, None)
ok("it renders as an appliance", type(_laser).__name__ == "ApplianceItem")
_lw = cv.get_device_connectivity_warnings(_laser)
_sww = cv.get_device_connectivity_warnings(_sw_l)
ok("the port ceiling catches it", any("1000W" in w for w in _lw))
ok("and so does the switch's budget", any("budget exceeded" in w for w in _sww))
# It must not blow up anything structural: it is a normal item that happens to be daft.
_lp_img = _Img(300, 300, _Img.Format_ARGB32); _lp_img.fill(Qt.transparent)
_lp = _Pnt(_lp_img); _lp.translate(150, 150)
try:
    _laser.paint(_lp, _Opt(), None)
    _laser_painted = True
except Exception as _exc:
    _laser_painted = False
    print("   paint raised:", _exc)
_lp.end()
ok("it still paints, selected and all", _laser_painted)
ok("and serializes like any other device",
   any(d.get("spec_id") == _laser_spec["id"] for d in win._serialize_state()["devices"]))

print("\n== Version and packaging ==")
import ast as _ast2
from src.core import version as _ver
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ok("the app reports a version", bool(_ver.APP_VERSION))
ok("the window title carries it", _ver.APP_VERSION in win.windowTitle())
# The .lense "version" field describes the FILE FORMAT, not the app. Tying them together
# would mean every release claimed to be a new format and old files stopped loading.
ok("the project file format version is independent",
   win._serialize_state()["version"] == "1.0")

# The Windows version resource is only parsed on Windows, so a typo there would first
# show up on someone else's machine mid-build. Check it here instead.
_vi = os.path.join(_root, "version_info.txt")
ok("version_info.txt exists", os.path.exists(_vi))
_tree = _ast2.parse(open(_vi).read(), mode="eval")
ok("it is a valid VSVersionInfo block",
   isinstance(_tree.body, _ast2.Call) and _tree.body.func.id == "VSVersionInfo")
_strings = {}
def _walk(node):
    if isinstance(node, _ast2.Call) and getattr(node.func, "id", "") == "StringStruct":
        _strings[_ast2.literal_eval(node.args[0])] = _ast2.literal_eval(node.args[1])
    for _c in _ast2.iter_child_nodes(node):
        _walk(_c)
_walk(_tree.body)
ok("it declares the same version as the app",
   _strings.get("FileVersion") == _ver.APP_VERSION
   and _strings.get("ProductVersion") == _ver.APP_VERSION)
_ffi = {k.arg: k.value for k in
        {kk.arg: kk.value for kk in _tree.body.keywords}["ffi"].keywords}
ok("its numeric version matches VERSION_TUPLE",
   tuple(_ast2.literal_eval(e) for e in _ffi["filevers"].elts) == _ver.VERSION_TUPLE)

for _needed in ("Lense.spec", "version_info.txt", "requirements.txt",
                "build_windows.bat", "main.py"):
    ok(f"{_needed} is present for a build", os.path.exists(os.path.join(_root, _needed)))
_spec = open(os.path.join(_root, "Lense.spec")).read()
ok("the spec bundles src/data", "('src/data', 'data')" in _spec)
ok("the spec carries QtMultimedia", "PySide6.QtMultimedia" in _spec)
ok("the spec sets the Windows icon and version",
   "Catmera.ico" in _spec and "version_info.txt" in _spec)

print("\n== Zoom sensitivity (mouse vs touchpad) ==")
# One fixed zoom step per wheel EVENT was right for a mouse (one event per notch) and far
# too fast for a touchpad, which sends dozens of tiny events per swipe -- each got a full
# step. Zoom now follows event size, and each device has its own sensitivity.
from src.core import zoom_input as _zi
from src.core.utils import ZOOM_STEP as _ZSTEP
from PySide6.QtGui import QWheelEvent as _QWE2

ok("a whole notch reads as a mouse", not _zi.is_touchpad(120, 0, False, False))
ok("a pixel delta reads as a touchpad", _zi.is_touchpad(0, 6, False, False))
ok("a scroll phase reads as a touchpad", _zi.is_touchpad(120, 0, True, False))
# Touchpads often report themselves as a plain mouse, so shape has to count too.
ok("sub-notch increments read as a touchpad", _zi.is_touchpad(12, 0, False, False))
ok("one notch is one step", _zi.notches(120, 0) == 1.0)
ok("a huge flick is clamped", _zi.notches(120000, 0) == _zi.MAX_NOTCHES_PER_EVENT)
ok("pixel-only events still zoom", _zi.notches(0, 60) == 1.0)
ok("zooming in then out cancels exactly",
   abs(_zi.zoom_factor(_ZSTEP, 0.7, 1.3) * _zi.zoom_factor(_ZSTEP, -0.7, 1.3) - 1.0) < 1e-12)
ok("sensitivity is clamped to a sane range", _zi.clamp_sensitivity(99) == _zi.MAX_SENSITIVITY
   and _zi.clamp_sensitivity("junk") == _zi.DEFAULT_SENSITIVITY)

fresh()
win.resize(1100, 750); win.show(); win.sheet_tabs.setCurrentWidget(cv); app.processEvents()
cv.zoom_sensitivity_mouse = 1.0
cv.zoom_sensitivity_touchpad = 1.0

def _wheel(angle, pixel=0, phase=Qt.NoScrollPhase):
    _at = QPointF(400, 300)
    cv.wheelEvent(_QWE2(_at, _at, QPoint(0, pixel), QPoint(0, angle),
                        Qt.NoButton, Qt.NoModifier, phase, False))

def _ratio(fn):
    _b = cv.transform().m11(); fn(); return cv.transform().m11() / _b

def _swipe():
    for _i in range(30):
        _wheel(12, 6, Qt.ScrollUpdate)

ok("a mouse notch zooms exactly as it always did", abs(_ratio(lambda: _wheel(120)) - _ZSTEP) < 1e-9)
_tp = _ratio(_swipe)
ok("a touchpad swipe no longer takes a full step per event", _tp < _ZSTEP ** 30 / 4)
ok("but it still zooms", _tp > 1.0)

cv.zoom_sensitivity_touchpad = 0.5
ok("lowering touchpad sensitivity slows the touchpad", _ratio(_swipe) < _tp)
ok("and leaves the mouse alone", abs(_ratio(lambda: _wheel(120)) - _ZSTEP) < 1e-9)
cv.zoom_sensitivity_touchpad = 1.0
cv.zoom_sensitivity_mouse = 2.0
ok("mouse sensitivity scales the mouse", abs(_ratio(lambda: _wheel(120)) - _ZSTEP ** 2) < 1e-9)
ok("and leaves the touchpad alone", abs(_ratio(_swipe) - _tp) < 1e-6)
cv.zoom_sensitivity_mouse = 1.0
ok("a zero-length gesture end does nothing", _ratio(lambda: _wheel(0, 0, Qt.ScrollEnd)) == 1.0)

from src.ui import settings_dialog as _sd2
_dz = _sd2.SettingsDialog(win)
_dz.zoom_mouse_slider.setValue(80)
_dz.zoom_touchpad_slider.setValue(35)
_dz.accept()
ok("OK stores both sensitivities",
   abs(_sd2.get_float(_sd2.KEY_ZOOM_MOUSE) - 0.8) < 1e-9
   and abs(_sd2.get_float(_sd2.KEY_ZOOM_TOUCHPAD) - 0.35) < 1e-9)
ok("and applies them to the canvas immediately",
   abs(cv.zoom_sensitivity_mouse - 0.8) < 1e-9 and abs(cv.zoom_sensitivity_touchpad - 0.35) < 1e-9)
cv.zoom_sensitivity_mouse = cv.zoom_sensitivity_touchpad = 1.0
_sd2.apply_saved_settings(win)
ok("they come back on startup",
   abs(cv.zoom_sensitivity_mouse - 0.8) < 1e-9 and abs(cv.zoom_sensitivity_touchpad - 0.35) < 1e-9)
_dz = _sd2.SettingsDialog(win)
_dz.restore_defaults()
ok("Restore Defaults puts both back to 100%",
   _dz.zoom_mouse_slider.value() == 100 and _dz.zoom_touchpad_slider.value() == 100)
_dz.reject()
QSettings().remove(_sd2.KEY_ZOOM_MOUSE)
QSettings().remove(_sd2.KEY_ZOOM_TOUCHPAD)
cv.zoom_sensitivity_mouse = cv.zoom_sensitivity_touchpad = 1.0
win.sheet_tabs.setCurrentIndex(0)

print("\n== Catalog thumbnails stay small ==")
import glob as _glob, json as _json, shutil as _shutil, tempfile as _tempfile
from PySide6.QtGui import QColor as _QColor, QImage as _QImage
from src.core import image_assets as _ia
from src.ui.catalog_manager import CatalogManagerDialog as _CMD

# Every image the catalog ships must already be within policy. This is the guard
# that stops a 3 MB vendor original being dropped back in: it would bloat the
# repository and every executable built from it, for pixels nothing ever draws.
_img_paths = sorted(_glob.glob("src/data/images/*"))
ok("the catalog ships its thumbnails", len(_img_paths) > 50)
_oversized = [(os.path.basename(p), os.path.getsize(p))
              for p in _img_paths if os.path.getsize(p) > _ia.MAX_BYTES]
ok("none exceeds the size cap", not _oversized)
_too_big = []
for _p in _img_paths:
    _i = _QImage(_p)
    if max(_i.width(), _i.height()) > _ia.MAX_EDGE:
        _too_big.append(os.path.basename(_p))
ok("none exceeds the resolution cap", not _too_big)

# And every entry still points at a file that exists -- the trap when converting
# an opaque PNG to JPEG renames it.
_broken = []
for _f in _glob.glob("src/data/*.json"):
    _d = _json.load(open(_f))
    for _e in (_d.values() if isinstance(_d, dict) else _d):
        if isinstance(_e, dict) and _e.get("imagePath"):
            if not os.path.exists(os.path.join("src/data", _e["imagePath"])):
                _broken.append(_e.get("id"))
ok("every catalog entry's image exists", not _broken)

# The policy itself.
_alpha = _QImage(1200, 900, _QImage.Format_ARGB32); _alpha.fill(_QColor(0, 0, 0, 0))
_data, _ext = _ia.encode_thumbnail(_alpha)
ok("transparency is kept as PNG", _ext == ".png")
ok("and is scaled down to the cap",
   max(_QImage.fromData(_data).width(), _QImage.fromData(_data).height()) == _ia.MAX_EDGE)

_opaque = _QImage(1200, 900, _QImage.Format_RGB32)
for _y in range(_opaque.height()):        # noise, so it can't trivially compress
    for _x in range(0, _opaque.width(), 7):
        _opaque.setPixelColor(_x, _y, _QColor((_x * 7) % 255, (_y * 13) % 255, (_x + _y) % 255))
_data, _ext = _ia.encode_thumbnail(_opaque)
ok("an opaque image becomes a JPEG", _ext == ".jpg")
ok("and lands under the size cap", len(_data) <= _ia.MAX_BYTES)

_small = _QImage(64, 48, _QImage.Format_RGB32); _small.fill(_QColor("#334455"))
_data, _ext = _ia.encode_thumbnail(_small)
_back = _QImage.fromData(_data)
ok("a small image is never upscaled", (_back.width(), _back.height()) == (64, 48))
ok("an unreadable file yields nothing", _ia.encode_thumbnail(_QImage()) is None)

# write_thumbnail on disk, including replacing a thumbnail of the other format.
_tmp = _tempfile.mkdtemp()
_srcimg = os.path.join(_tmp, "source.png")
_alpha.save(_srcimg)
_rel = _ia.write_thumbnail(_srcimg, _tmp, "widget-1")
ok("write_thumbnail returns a catalog-relative path", _rel == "images/widget-1.png")
ok("and actually wrote the file", os.path.exists(os.path.join(_tmp, "widget-1.png")))
_opaque_src = os.path.join(_tmp, "source2.jpg"); _opaque.save(_opaque_src)
_rel2 = _ia.write_thumbnail(_opaque_src, _tmp, "widget-1")
ok("re-importing in another format replaces the old file",
   _rel2 == "images/widget-1.jpg"
   and os.path.exists(os.path.join(_tmp, "widget-1.jpg"))
   and not os.path.exists(os.path.join(_tmp, "widget-1.png")))

# The Catalog Manager's own import path, not just the helper underneath it.
_dlg_images = os.path.join(_tmp, "catalog_images")
_cm = _CMD(win.catalog_tree)
_cm.catalog_tree.get_images_dir = lambda: _dlg_images
_huge = os.path.join(_tmp, "huge.png")
_QImage(3000, 3000, _QImage.Format_ARGB32).save(_huge)
_stored = _cm._copy_image_in("test-widget", _huge)
_written = os.path.join(_dlg_images, os.path.basename(_stored))
ok("picking an image in the Catalog Manager normalises it",
   os.path.exists(_written) and os.path.getsize(_written) <= _ia.MAX_BYTES
   and max(_QImage(_written).width(), _QImage(_written).height()) <= _ia.MAX_EDGE)
_junk = os.path.join(_tmp, "notanimage.png")
open(_junk, "w").write("this is not an image")
_stored_junk = _cm._copy_image_in("test-junk", _junk)
ok("a file Qt cannot read is still kept as chosen",
   os.path.exists(os.path.join(_dlg_images, os.path.basename(_stored_junk))))
_cm.deleteLater()
_shutil.rmtree(_tmp, ignore_errors=True)

print("\n== Multi-gang wall drops (one cable per outlet) ==")
from PySide6.QtCore import QRectF as _QRectF
from src.ui import settings_dialog as _sd3

# Drawn the way a user draws them: real presses through the real event handlers, at 1:1
# so a click lands exactly where it is aimed.
def _md_setup(tool="cable"):
    cv.setTransform(QTransform()); cv._zoom_changed()
    cv.setSceneRect(_QRectF(0, 0, 1000, 700)); cv.centerOn(500, 350)
    cv.cable_draw_points = []
    cv.set_tool(tool)

def _md_press(world, mods=Qt.NoModifier):
    pos = cv.mapFromScene(world)
    cv.mousePressEvent(_QME(_QEv.MouseButtonPress, pos, cv.viewport().mapToGlobal(pos),
                            Qt.LeftButton, Qt.LeftButton, mods))

def _md_finish(world):
    _md_press(world)
    pos = cv.mapFromScene(world)
    cv.mouseDoubleClickEvent(_QME(_QEv.MouseButtonDblClick, pos, cv.viewport().mapToGlobal(pos),
                                  Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))

def _md_draw(*points, ctrl_last=False, tool="cable"):
    """Draw a run through these scene points; returns the cables it created."""
    _md_setup(tool)
    before = {c.id for c in cv.get_cables()}
    for pt in points[:-1]:
        _md_press(pt)
    if ctrl_last:
        _md_press(points[-1], mods=Qt.ControlModifier)
    else:
        _md_finish(points[-1])
    return [c for c in cv.get_cables() if c.id not in before]

win.resize(1400, 900); win.show(); app.processEvents()
fresh()
cv.scene_obj.scale_ratio = 10.0
ok("the catalog carries every plate size up to six",
   all(f"generic-wall-drop-{n}port" in cat for n in range(1, 7)))
ok("and each one reports its outlet count",
   [cat[f"generic-wall-drop-{n}port"]["ports"] for n in range(1, 7)] == [1, 2, 3, 4, 5, 6])

_md_rack = cv.place_rack(QPointF(100, 100), label="MDF", ru_height=12)
_md6 = cv.place_device(cat["generic-wall-drop-6port"], QPointF(700, 100))
ok("a six-gang plate builds with six ports", _md6.port_count == 6)

_md_made = _md_draw(_md_rack.pos(), QPointF(400, 250), _md6.pos())
ok("one drawn run to a six-gang plate pulls six cables", len(_md_made) == 6)
ok("one per outlet, in order",
   sorted(c.end_port for c in _md_made) == [1, 2, 3, 4, 5, 6])
ok("named for the plate and the outlet",
   sorted(c.label for c in _md_made) == [f"({_md6.label})-{i}" for i in range(1, 7)])
ok("every copy follows the same route",
   all([(p.x(), p.y()) for p in c.points] ==
       [(p.x(), p.y()) for p in _md_made[0].points] for c in _md_made))
ok("and they all land on the plate",
   all(c.vertex_anchors[-1] == _md6.id for c in _md_made))

# Copies must be independent objects, not six views of one path.
_md_moved = list(_md_made[0].points)
_md_moved[1] = QPointF(450, 300)
_md_made[0].set_points(_md_moved, anchors=list(_md_made[0].vertex_anchors))
ok("dressing one strand leaves the others alone",
   _md_made[1].points[1].x() != 450)

# The branch tool starts AT the plate and follows the bundle home -- same result.
_md4 = cv.place_device(cat["generic-wall-drop-4port"], QPointF(700, 400))
_md_branch = _md_draw(_md4.pos(), QPointF(400, 250), ctrl_last=True)
ok("Ctrl+click branching pulls one per outlet too", len(_md_branch) == 4)
ok("with the plate at the near end", sorted(c.start_port for c in _md_branch) == [1, 2, 3, 4])
ok("named the same way",
   sorted(c.label for c in _md_branch) == [f"({_md4.label})-{i}" for i in range(1, 5)])
ok("and each one traces the whole bundle", all(len(c.points) > 2 for c in _md_branch))

# The leg from a plate out to the gear it serves is one cable, never six.
_md_ap = cv.place_device(cat["ubiquiti-u7-pro"], QPointF(900, 100))
_md_leg = _md_draw(_md6.pos(), _md_ap.pos())
ok("a plate-to-AP leg stays a single cable", len(_md_leg) == 1)
ok("and keeps an ordinary cable name", not _md_leg[0].label.startswith("("))

# Outlets already spoken for aren't doubled up.
_md_spare = _md_draw(_md_rack.pos(), _md6.pos())
ok("a run to a full plate adds just one spare", len(_md_spare) == 1)
_md2 = cv.place_device(cat["generic-wall-drop-2port"], QPointF(250, 500))
_md_first = _md_draw(_md_rack.pos(), _md2.pos())
_md_second = _md_draw(_md_rack.pos(), _md2.pos())
ok("a half-used plate fills only what is left",
   len(_md_first) == 2 and len(_md_second) == 1)
ok("and the spare takes no outlet twice",
   sorted((c.end_port for c in _md_first + _md_second),
          key=lambda port: (port is None, port)) == [1, 2, None])

# A single-gang plate is still one cable, and fiber never lands on a keystone.
_md1 = cv.place_device(cat["generic-wall-drop-1port"], QPointF(250, 200))
ok("a single-gang plate pulls one", len(_md_draw(_md_rack.pos(), _md1.pos())) == 1)
_md_f = cv.place_device(cat["generic-wall-drop-6port"], QPointF(250, 620))
_md_fiber = _md_draw(_md_rack.pos(), _md_f.pos(), tool="fiber")
ok("fiber to a plate is never multiplied", len(_md_fiber) == 1)

# Clicking the MDF has to START a run, even though every cable in the job converges
# there -- it used to graft a vertex onto whichever run the click landed on.
_md_trunk = cv.get_cables()[0]
_md_before_pts = len(_md_trunk.points)
_md_setup()
_md_press(_md_rack.pos())
ok("clicking the rack starts a run rather than editing one",
   len(cv.cable_draw_points) == 1
   and len(_md_trunk.points) == _md_before_pts)
cv.cable_draw_points = []

# The whole behaviour is optional.
cv.multidrop_auto_cables = False
_md_off = _md_draw(_md_rack.pos(), cv.place_device(cat["generic-wall-drop-6port"],
                                                    QPointF(900, 620)).pos())
ok("turning it off draws one cable again", len(_md_off) == 1)
cv.multidrop_auto_cables = True

_mddlg = _sd3.SettingsDialog(win)
ok("Settings exposes the toggle", _mddlg.multidrop_chk.isChecked() is True)
_mddlg.multidrop_chk.setChecked(False)
_mddlg.accept()
ok("unchecking it reaches the canvas", cv.multidrop_auto_cables is False)
ok("and is remembered", _sd3.get_bool(_sd3.KEY_MULTIDROP_CABLES) is False)
_mddlg = _sd3.SettingsDialog(win)
_mddlg.restore_defaults(); _mddlg.accept()
ok("Restore Defaults turns it back on", cv.multidrop_auto_cables is True)
QSettings().clear()
cv.setTransform(QTransform()); cv._zoom_changed()

print("\n== Termination (RJ45 vs jack & patch cable) ==")
from src.core import termination as _term
from src.ui import inventory_panel as _inv
_term.clear_defaults()
fresh()
cv.scene_obj.scale_ratio = 10.0

# Defaults: the two passive jacks punch down, everything else gets a crimped plug.
ok("a patch panel defaults to a jack", _term.default_method("patch-panel") == _term.JACK)
ok("a wall drop defaults to a jack", _term.default_method("drop") == _term.JACK)
ok("active gear defaults to RJ45",
   all(_term.default_method(c) == _term.RJ45
       for c in ("camera", "access-point", "switch", "nvr", "rack", "misc")))

_t_rack = cv.place_rack(QPointF(0, 0), label="MDF", ru_height=12)
_t_panel = cv.place_device(cat["generic-patch-panel-24"], QPointF(200, 0))
_t_drop = cv.place_device(cat["generic-wall-drop-1port"], QPointF(900, 0))
_t_cam = cv.place_camera(cat["ubiquiti-g5-bullet"], QPointF(1400, 0))

ok("a fresh object inherits its category default", _term.is_inherited(_t_drop))
ok("and resolves to that default", _term.method_of(_t_drop) == _term.JACK)
_t_drop.termination = _term.RJ45
ok("an override takes effect", _term.method_of(_t_drop) == _term.RJ45
   and not _term.is_inherited(_t_drop))
_t_drop.termination = None
ok("clearing it returns to the default", _term.method_of(_t_drop) == _term.JACK)

_term.set_default("drop", _term.RJ45)
ok("changing a category default moves untouched objects",
   _term.method_of(_t_drop) == _term.RJ45)
_t_cam.termination = _term.JACK
_term.set_default("camera", _term.RJ45)
ok("but never one set by hand", _term.method_of(_t_cam) == _term.JACK)
_term.clear_defaults()
_t_cam.termination = None
_t_note = cv.place_label(QPointF(50, 50), "note")
ok("an object cable never lands on has no termination",
   _term.method_of(_t_note) is None and _term.method_of(None) is None)
cv.scene_obj.removeItem(_t_note)

# Counting. One run panel -> drop: the panel end is a jack (plus the jumper it needs),
# the drop end is a jack too, and neither is an RJ45 plug.
_t_run = link(_t_panel, _t_drop, 1, 1)
_m = _term.count_materials(cv)
ok("each jack end is counted", _m.jacks == 2)
ok("a jack with nothing patched implies a patch cable", _m.patch_cables_implied == 2)
ok("no plugs where nothing crimps", _m.plugs == 0)

# A real patch cable drawn in the Rack Editor is the jumper -- don't invent a second.
_t_patch = CableItem(); _t_patch.cable_type = "Patch"
_t_patch.set_points([_t_panel.pos(), _t_rack.pos()], anchors=[_t_panel.id, _t_rack.id])
_t_patch.start_port, _t_patch.end_port = 1, None
cv.scene_obj.addItem(_t_patch)
_m = _term.count_materials(cv)
ok("a drawn patch cable is counted once", _m.patch_cables_drawn == 1)
ok("and the jack it serves implies no second one", _m.patch_cables_implied == 1)
ok("the patch total is drawn plus implied", _m.patch_cables == 2)
ok("a patch cable terminates nothing itself", _m.jacks == 2 and _m.plugs == 0)

# An RJ45 end crimps a plug, and the breakdown says which kind of object it came from.
_t_run2 = link(_t_drop, _t_cam, 1, None)
_m = _term.count_materials(cv)
ok("an RJ45 end counts a plug", _m.plugs == 1)
ok("the breakdown attributes it to the camera",
   _m.by_category.get("camera", {}).get("plugs") == 1)
ok("and the jacks to the passive gear",
   _m.by_category["patch-panel"]["jacks"] == 1 and _m.by_category["drop"]["jacks"] == 2)

# Fiber lands on LC/SC connectors, not anything counted here; a free end lands on nothing.
_t_fiber = CableItem(); _t_fiber.cable_type = "Fiber"
_t_fiber.set_points([_t_rack.pos(), _t_panel.pos()], anchors=[_t_rack.id, _t_panel.id])
cv.scene_obj.addItem(_t_fiber)
_t_free = CableItem(); _t_free.cable_type = "CAT6"
_t_free.set_points([_t_rack.pos(), QPointF(700, 700)], anchors=[_t_rack.id, None])
cv.scene_obj.addItem(_t_free)
_m2 = _term.count_materials(cv)
ok("fiber is not counted", _m2.jacks == _m.jacks)
ok("a free cable end terminates nothing", _m2.plugs == _m.plugs + 1)   # only the rack end

# Cost reaches the inventory panel, and the grand total with it.
_inv_panel = win.inventory_panel
for _key, _value in ((_inv.KEY_PLUG_PRICE, 1.0), (_inv.KEY_JACK_PRICE, 10.0),
                     (_inv.KEY_PATCH_PRICE, 100.0)):
    _inv_panel.price_spins[_key].setValue(_value)
_inv_panel.refresh()
_m3 = _term.count_materials(cv)
_expected = _m3.plugs * 1.0 + _m3.jacks * 10.0 + _m3.patch_cables * 100.0
ok("the panel prices terminations",
   _inv_panel.lbl_termination_cost.text() == f"${_expected:,.0f}")
ok("a unit price is remembered for the next project",
   _inv.price(_inv.KEY_JACK_PRICE) == 10.0)
_t_names = [_inv_panel.tree.topLevelItem(_i).text(0)
            for _i in range(_inv_panel.tree.topLevelItemCount())]
ok("and lists them in the takeoff", "Terminations" in _t_names)
for _key in (_inv.KEY_PLUG_PRICE, _inv.KEY_JACK_PRICE, _inv.KEY_PATCH_PRICE):
    QSettings().remove(_key)
    _inv_panel.price_spins[_key].setValue(_inv.DEFAULT_PRICES[_key])

# The parameter has to survive a save, on every kind of object that can hold one.
_t_drop.termination = _term.RJ45
_t_cam.termination = _term.JACK
_t_rack.termination = _term.JACK
_t_saved = win._serialize_state()
cv.scene_obj.clear(); cv.floorplan_item = None
win._restore_state(_t_saved)
_by_label = {getattr(i, "label", None): i for i in cv.scene_obj.items()}
ok("a device keeps its termination",
   _by_label[_t_drop.label].termination == _term.RJ45)
ok("a camera keeps its termination", _by_label[_t_cam.label].termination == _term.JACK)
ok("a rack keeps its termination", _by_label["MDF"].termination == _term.JACK)
ok("and an untouched object still inherits",
   _by_label[_t_panel.label].termination is None)

# Drive the real widgets, not just the model underneath them: a parameter you can't
# reach from the UI isn't a feature.
from PySide6.QtWidgets import QComboBox as _QCB

def _termination_combo(item):
    win.sidebar.select_item(item)
    # The panel tears old rows down with deleteLater(), which needs an event loop the
    # tests never spin -- flush it, or we'd find the PREVIOUS item's combo still
    # parented to the container and test a widget nobody can see.
    app.sendPostedEvents(None, _QEv.DeferredDelete)
    for combo in win.sidebar.container.findChildren(_QCB):
        data = [combo.itemData(i) for i in range(combo.count())]
        if data == [None, _term.RJ45, _term.JACK]:
            return combo
    return None

_ui_drop = _by_label[_t_drop.label]
_ui_drop.termination = None
_combo = _termination_combo(_ui_drop)
ok("the properties panel offers a termination control", _combo is not None)
ok("it opens on the inherited default",
   _combo.currentData() is None and "Jack" in _combo.itemText(0))
_combo.setCurrentIndex(1)
ok("picking RJ45 in the panel sets the object", _ui_drop.termination == _term.RJ45)
_combo.setCurrentIndex(0)
ok("picking Default hands it back to the category", _ui_drop.termination is None)

_ui_cam = _by_label[_t_cam.label]
ok("a camera gets the control too", _termination_combo(_ui_cam) is not None)
ok("so does a rack", _termination_combo(_by_label["MDF"]) is not None)
win.sidebar.select_item(None)

_tdlg = _sd.SettingsDialog(win)
ok("Settings exposes every category", set(_tdlg.termination_combos) == set(_term.CATEGORY_DEFAULTS))
_tdlg.termination_combos["camera"].setCurrentIndex(
    _tdlg.termination_combos["camera"].findData(_term.JACK))
_tdlg.accept()
ok("OK persists a new category default", _term.default_method("camera") == _term.JACK)
ok("and an object following it moves with it", _term.method_of(_ui_cam) == _term.JACK)

_tdlg = _sd.SettingsDialog(win)
ok("the dialog reopens showing what is in force",
   _tdlg.termination_combos["camera"].currentData() == _term.JACK)
_tdlg.restore_defaults()
_tdlg.accept()
ok("Restore Defaults puts the category back", _term.default_method("camera") == _term.RJ45)

_term.clear_defaults()

print("\n== Splash chime ==")
import src.main as _splash_main
from src.core.utils import get_data_dir as _gdd
_sound = os.path.join(_gdd(), _splash_main.SPLASH_SOUND)
ok("the chime ships in the bundled data dir", os.path.exists(_sound))
ok("its length reads back", abs(_splash_main._wav_duration(_sound) - 4.0) < 0.01)
ok("the splash is held long enough not to clip it",
   min(_splash_main.SPLASH_MAX_SECONDS,
       max(_splash_main.SPLASH_MIN_SECONDS,
           _splash_main._wav_duration(_sound) + _splash_main.SPLASH_SOUND_TAIL))
   >= _splash_main._wav_duration(_sound))
# QSoundEffect tags its stream media.role="event" -- the system-notification category,
# which is muted outright on any desktop with event sounds turned off, while Qt still
# reports the sound playing. Anything that reintroduces it here is silent in the field.
_src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "main.py")).read()
import ast as _ast
def _identifiers(source):
    """Every name the module actually references -- prose in comments and docstrings
    explaining what NOT to use is not a usage."""
    names = set()
    for node in _ast.walk(_ast.parse(source)):
        if isinstance(node, _ast.Name):
            names.add(node.id)
        elif isinstance(node, _ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, (_ast.Import, _ast.ImportFrom)):
            names.update(a.name for a in node.names)
    return names
_names = _identifiers(_src)
ok("the chime does not go through QSoundEffect",
   "QSoundEffect" not in _names and "QMediaPlayer" in _names)
_handle = _splash_main._load_splash_sound(_sound)
ok("the player loads", _handle is not None)
ok("the QAudioOutput is kept alive alongside it", _handle is not None and len(_handle) == 2)
ok("a missing file degrades to None", _splash_main._load_splash_sound("/nope/none.wav") is None)
ok("an unreadable wav reports zero length", _splash_main._wav_duration(os.path.join(_gdd(), "logo.png")) == 0.0)

print("\n== Performance guard (validation must stay off the paint path) ==")
import time as _t
from PySide6.QtGui import QPainter, QPixmap
fresh()
_core = cv.place_device(cat["ubiquiti-usw-24-poe"], QPointF(0,0))
for _i in range(24):
    _flex = cv.place_device(cat["ubiquiti-usw-flex"], QPointF(300, _i*120))
    link(_core, _flex, 17+(_i%8), 1)
    for _j in range(3):
        link(_flex, cv.place_device(cat["ubiquiti-u7-lite"], QPointF(600, _i*120+_j*30)), 2+_j)
cv.refresh_connectivity_badges()
_pm = QPixmap(1200, 900); _p = QPainter(_pm)
_t0 = _t.perf_counter()
for _ in range(10):
    cv.scene_obj.render(_p)
_frame = (_t.perf_counter() - _t0) / 10
_p.end()
print(f"  {len(cv.get_network_devices())} devices -> {_frame*1000:.1f} ms/frame ({1/_frame:.0f} fps)")
ok("repaint stays under 50ms/frame at ~100 devices", _frame < 0.05)

_modal_watchdog.stop()
ok("no check was blocked by an unexpected modal dialog", not dialogs_seen)
if dialogs_seen:
    print("  dialogs raised:", dialogs_seen)

failed = [n for n, p in checks if not p]
print(f"\n{len(checks)-len(failed)}/{len(checks)} passed")
if failed:
    print("FAILED:", *failed, sep="\n  - "); sys.exit(1)
print("ALL REGRESSION CHECKS PASSED")
