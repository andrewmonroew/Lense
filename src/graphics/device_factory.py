from src.graphics.device_item import DeviceItem
from src.graphics.patch_panel_item import PatchPanelItem
from src.graphics.drop_item import DropItem
from src.graphics.access_point_item import AccessPointItem
from src.graphics.appliance_item import ApplianceItem

_CATEGORY_CLASSES = {
    "patch-panel": PatchPanelItem,
    "drop": DropItem,
    "access-point": AccessPointItem,
    "misc": ApplianceItem,
}


def make_device_item(spec, x, y):
    """Constructs the right DeviceItem subclass for a catalog spec's category, falling
    back to the plain DeviceItem for switch/nvr/anything else. Single source of truth
    for this dispatch -- used everywhere a device is built from a spec (placing loose on
    the canvas, dropping into a rack, mounting via the Rack Editor, reconstructing from a
    saved project or an undo/redo snapshot) so a patch-panel/drop/access-point/misc spec
    renders correctly immediately, not just after a save/reload round-trip."""
    cls = _CATEGORY_CLASSES.get(spec.get("category"), DeviceItem)
    return cls(spec, x, y)
