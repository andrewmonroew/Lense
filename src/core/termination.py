"""How a cable actually lands on an object, and what hardware that costs.

Footage alone never quoted a job. Every run has to be terminated at both ends, and the
two ways of doing it cost very different things:

    RJ45              -- a modular plug crimped straight onto the run. One plug.
    Jack & patch cable -- the run is punched down on a jack, and a patch cable jumpers
                          from that jack to the equipment. One jack AND one patch cable.

A patch panel is the obvious case (every port is a punch-down plus a jumper out the
front), but it is not the only one: a camera in a junction box, an AP on a wall plate and
a switch fed from a panel all differ, and the difference is real money once a job has
sixty drops in it.

So termination is a property of the OBJECT, exactly like the cable allowances next door
in cable_length.py: it describes how cable lands on that thing, and every run reaching it
is terminated that way. Each object stores None meaning "follow the default for my kind",
so changing a category default in Settings moves every untouched object with it while
anything set by hand keeps its own answer.

Counting rule: one termination per cable END landing on an object. Patch cables are
excluded -- they arrive from the factory with plugs moulded on, so they terminate nothing
-- and so is fiber, which lands on LC/SC connectors rather than anything counted here.
A jack implies a patch cable only where no real one has been drawn on that port yet, so
a rack you have already patched in the Rack Editor is never counted twice.
"""

from PySide6.QtCore import QSettings

from src.core import cable_length

RJ45 = "rj45"
JACK = "jack"

METHODS = (RJ45, JACK)
METHOD_LABELS = {RJ45: "RJ45 plug", JACK: "Jack & patch cable"}

# Cable types that terminate nothing here: a patch cable comes with its plugs already on,
# and fiber lands on connectors this model doesn't price.
UNTERMINATED_TYPES = ("Patch", "Fiber")

# Shares cable_length's category buckets on purpose -- the same eight kinds of object,
# the same inherit-or-override rule, one mental model for the user.
CATEGORY_DEFAULTS = {
    "rack":         RJ45,
    "patch-panel":  JACK,   # a port is a punch-down plus a jumper out the front
    "drop":         JACK,   # a wall plate is a jack; the device patches into it
    "camera":       RJ45,
    "access-point": RJ45,
    "switch":       RJ45,
    "nvr":          RJ45,
    "misc":         RJ45,
}

FALLBACK = RJ45

KEY_PREFIX = "cabling/"


def category_label(category):
    """The human name for a category, straight from the allowance table."""
    entry = cable_length.CATEGORY_DEFAULTS.get(category)
    return entry[0] if entry else category


def normalize(value):
    """A stored/typed value as a real method, or None if it isn't one."""
    if isinstance(value, str) and value.strip().lower() in METHODS:
        return value.strip().lower()
    return None


# Resolved defaults, memoized for the same reason cable_length's are: this is read on the
# paint path via the inventory refresh and opening QSettings per lookup costs real frames.
_defaults_memo = {}


def default_method(category):
    if category in _defaults_memo:
        return _defaults_memo[category]
    value = normalize(QSettings().value(f"{KEY_PREFIX}{category}/termination"))
    if value is None:
        value = CATEGORY_DEFAULTS.get(category, FALLBACK)
    _defaults_memo[category] = value
    return value


def set_default(category, method):
    method = normalize(method) or FALLBACK
    QSettings().setValue(f"{KEY_PREFIX}{category}/termination", method)
    _defaults_memo.clear()


def clear_defaults():
    """Drops every stored override, so the built-in choices apply again."""
    settings = QSettings()
    for category in CATEGORY_DEFAULTS:
        settings.remove(f"{KEY_PREFIX}{category}/termination")
    _defaults_memo.clear()


def method_of(item):
    """How runs land on this object, or None if it isn't something cable lands on."""
    category = cable_length.category_of(item)
    if category is None:
        return None
    return normalize(getattr(item, "termination", None)) or default_method(category)


def is_inherited(item):
    return (normalize(getattr(item, "termination", None)) is None
            and cable_length.category_of(item) is not None)


def method_label(item):
    method = method_of(item)
    return METHOD_LABELS.get(method, "") if method else ""


class Materials:
    """What the terminations in a project add up to, in pieces.

    Patch cables are split by where the number came from so the inventory can say which
    of them exist in the design and which the jacks imply, rather than presenting one
    total the user can't reconcile against the racks they actually patched.
    """

    def __init__(self):
        self.plugs = 0
        self.jacks = 0
        self.patch_cables_drawn = 0
        self.patch_cables_implied = 0
        # category -> {"plugs": n, "jacks": n}, so a quote can be audited per kind of
        # object instead of being one unexplained lump.
        self.by_category = {}

    @property
    def patch_cables(self):
        return self.patch_cables_drawn + self.patch_cables_implied

    def _record(self, category, field):
        entry = self.by_category.setdefault(category, {"plugs": 0, "jacks": 0})
        entry[field] += 1

    def add_plug(self, category):
        self.plugs += 1
        self._record(category, "plugs")

    def add_jack(self, category, implies_patch_cable):
        self.jacks += 1
        self._record(category, "jacks")
        if implies_patch_cable:
            self.patch_cables_implied += 1


def _patch_cable_already_drawn(item, port, canvas_view):
    """True when this port already has a real patch cable in the design.

    Only a device with ports can answer that; anything else (a camera, a bare object)
    has no port to have patched, so its jack always implies a jumper of its own.
    """
    if port is None or not hasattr(item, "patch_cable_at_port"):
        return False
    return item.patch_cable_at_port(port, canvas_view) is not None


def count_materials(canvas_view):
    """Tally the termination hardware every run in the project needs."""
    materials = Materials()
    cables = canvas_view.get_cables()
    materials.patch_cables_drawn = sum(1 for c in cables if c.cable_type == "Patch")

    for cable in cables:
        if cable.cable_type in UNTERMINATED_TYPES:
            continue
        for item_id, port in ((cable.start_device_id, cable.start_port),
                              (cable.end_device_id, cable.end_port)):
            if not item_id:
                continue  # a free end runs to nothing and terminates on nothing
            item = canvas_view.find_device_or_camera_by_id(item_id)
            method = method_of(item)
            if method is None:
                continue
            category = cable_length.category_of(item)
            if method == JACK:
                materials.add_jack(
                    category, not _patch_cable_already_drawn(item, port, canvas_view))
            else:
                materials.add_plug(category)
    return materials
