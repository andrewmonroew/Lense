"""PoE power-chain resolution and validation.

The catalog models PoE on two sides: a source (PSE) publishes `poePortGroups` (how many
ports of each 802.3 class, and the per-port ceiling) plus a total `poeBudget`, and a load
(PD) publishes `poeInput` (the class it needs) and `maxPower` (what it actually draws).

The interesting case -- and the reason this module exists rather than a flat budget check
-- is a device that is BOTH, like the USW Flex: it is powered over Ethernet and re-sources
power downstream, and how much it can offer depends entirely on how it is itself fed:

    PoE++ in -> 46W out     PoE+ in -> 20W out     PoE in -> 8W out

So a USW Pro 24 PoE feeding a Flex feeding a U7 Pro (21W) works on one of that switch's
8 PoE++ ports and fails on any of its other 16 PoE+ ports, where the Flex can only offer
20W. That one-watt shortfall is invisible to a per-device budget check; it only falls out
of walking the whole chain. See `evaluate_device`.
"""

import functools

# Ordered weakest -> strongest so classes can be compared directly.
POE_CLASS_ORDER = ["802.3af", "802.3at", "802.3bt"]
POE_CLASS_RANK = {name: i for i, name in enumerate(POE_CLASS_ORDER)}
POE_CLASS_LABEL = {"802.3af": "PoE", "802.3at": "PoE+", "802.3bt": "PoE++"}

# Powered-device maximums per class, used only when a spec has no explicit maxPower --
# conservative, so validation degrades to "assume it draws the most its class allows"
# rather than silently passing a device we know nothing about.
POE_CLASS_PD_MAX_WATTS = {"802.3af": 12.95, "802.3at": 25.5, "802.3bt": 51.0}


def normalize_class(value):
    """'802.3at (PoE+)' -> '802.3at'. Catalog entries carry the human-readable form."""
    if not value:
        return None
    for name in POE_CLASS_ORDER:
        if name in value:
            return name
    return None


def class_label(standard):
    return POE_CLASS_LABEL.get(standard, standard or "unknown")


def _spec(item):
    return getattr(item, "spec", None) or {}


# ── Load (PD) side ────────────────────────────────────────────────────────────
def required_class(item):
    """The PoE class this device needs to be powered, or None if it isn't PoE-powered."""
    return normalize_class(_spec(item).get("poeInput"))


def is_powered_device(item):
    if required_class(item) is not None:
        return True
    # Cameras are PoE-powered by definition in this catalog -- none declare poeInput,
    # but they all carry maxPower, so a positive draw is proof enough. Their class is
    # unknown, so the class check simply doesn't apply to them (see evaluate_device).
    if getattr(item, "object_type", None) == "camera":
        watts = _spec(item).get("maxPower")
        return isinstance(watts, (int, float)) and watts > 0
    return False


def power_draw(item):
    """(watts, estimated) this device pulls from its source."""
    watts = _spec(item).get("maxPower")
    if isinstance(watts, (int, float)) and watts > 0:
        return float(watts), False
    fallback = POE_CLASS_PD_MAX_WATTS.get(required_class(item))
    if fallback:
        return fallback, True
    return 0.0, True


# ── Source (PSE) side ─────────────────────────────────────────────────────────
def port_groups(item):
    """PoE output port groups, normalized with an explicit first/last port range.

    Groups cover the PoE-capable ports in order. `firstPort` overrides where they don't
    start at port 1 -- the USW Flex's port 1 is its power *input*, not an output.
    """
    groups = []
    next_port = None
    for raw in _spec(item).get("poePortGroups") or []:
        count = int(raw.get("count") or 0)
        if count <= 0:
            continue
        first = raw.get("firstPort")
        first = int(first) if first is not None else (next_port or 1)
        groups.append({
            "first": first,
            "last": first + count - 1,
            "count": count,
            "standard": normalize_class(raw.get("standard")),
            "maxWatts": float(raw.get("maxWatts") or 0),
        })
        next_port = first + count
    return groups


def sources_power(item):
    """Whether this device can put power onto its ports at all."""
    return bool(port_groups(item))


def port_capability(item, port):
    """What a given output port can deliver: (group, assumed).

    `assumed` is True when the port number is unknown -- canvas-drawn cables carry no
    port, since ports are only assigned in the Rack Editor -- in which case we report the
    strongest port the device has and let the caller qualify the result rather than
    guessing pessimistically and crying wolf on every un-ported design.
    """
    groups = port_groups(item)
    if not groups:
        return None, False
    if port is not None:
        for group in groups:
            if group["first"] <= port <= group["last"]:
                return group, False
        return None, False  # a real port, but not one that carries power
    best = max(groups, key=lambda g: (POE_CLASS_RANK.get(g["standard"], -1), g["maxWatts"]))
    return best, True


def output_budget(item, input_class):
    """Total watts this device can put out downstream, given how it's fed.

    For a mains-powered switch this is just its rated `poeBudget`. For a PoE-powered
    switch it's read out of `poeOutputByInput`, keyed on the class actually reaching it
    -- that conditional is the crux of the whole module.
    """
    spec = _spec(item)
    by_input = spec.get("poeOutputByInput")
    if isinstance(by_input, dict):
        if not by_input:
            return 0.0  # PoE-powered but sources nothing (Flex Mini)
        if input_class is None:
            return 0.0
        # Fall back down the class ladder: a bt-fed device also satisfies an at entry.
        for name in reversed(POE_CLASS_ORDER[:POE_CLASS_RANK[input_class] + 1]):
            if name in by_input:
                return float(by_input[name])
        return 0.0
    budget = spec.get("poeBudget")
    return float(budget) if isinstance(budget, (int, float)) else 0.0


# ── Chain resolution ──────────────────────────────────────────────────────────
# ── Result cache ───────────────────────────────────────────────────────────────
# resolve_power_source walks the cable graph, and powered_devices_on runs it once per
# device -- so evaluating every device (which DeviceItem.paint does on EVERY repaint)
# is O(n^3) uncached, and measurably tanks the frame rate while dragging on a real
# project. Results only depend on the cable graph and which specs are placed, so they
# are cached against a cheap signature of exactly that and recomputed when it changes.
# No invalidation wiring to keep in sync: a changed signature is a miss, and a stale
# ordering is at worst a spurious miss, never a wrong answer.
_cache = {"signature": None, "sources": {}, "issues": {}}


def _scene_signature(canvas_view):
    cables = tuple((c.id, c.cable_type, tuple(c.vertex_anchors), c.start_port, c.end_port)
                   for c in canvas_view.get_cables())
    devices = tuple((d.id, (_spec(d) or {}).get("id"))
                    for d in canvas_view.get_network_devices() + canvas_view.get_cameras())
    return (cables, devices)


# One signature per outermost call. Building it scans every cable and device, and a
# single evaluation looks the cache up many times over -- powered_devices_on() resolves
# every device in the scene, each resolution a fresh lookup -- so rebuilding it per
# lookup made one repaint O(devices^2 x scene): 1.6 s for the first frame of a freshly
# opened 97-device design. Inside one call the scene can't change, so reusing it there
# is exact; the moment the outermost call returns it is dropped, and the next call
# (after any edit at all) builds a new one. No timers, nothing to invalidate by hand.
_signature_scope = {"depth": 0, "view": None, "value": None}


def _one_signature_per_call(function):
    @functools.wraps(function)
    def wrapper(canvas_view, *args, **kwargs):
        scope = _signature_scope
        scope["depth"] += 1
        try:
            return function(canvas_view, *args, **kwargs)
        finally:
            scope["depth"] -= 1
            if scope["depth"] == 0:
                scope["view"] = scope["value"] = None
    return wrapper


def _current_signature(canvas_view):
    scope = _signature_scope
    # Keyed by id() so the scope never holds a reference to the view itself.
    if scope["depth"] and scope["view"] == id(canvas_view) and scope["value"] is not None:
        return scope["value"]
    value = _scene_signature(canvas_view)
    if scope["depth"]:
        scope["view"], scope["value"] = id(canvas_view), value
    return value


def _cache_for(canvas_view):
    signature = _current_signature(canvas_view)
    if _cache["signature"] != signature:
        _cache["signature"] = signature
        _cache["sources"] = {}
        _cache["issues"] = {}
    return _cache


def invalidate_cache():
    """Drop memoized results outright (e.g. after a catalog edit changes a spec)."""
    _cache["signature"] = None
    _cache["sources"] = {}
    _cache["issues"] = {}


class PowerSource:
    """Where a device's power comes from, and what that path can actually deliver."""

    def __init__(self, device=None, port=None, group=None, assumed_port=False,
                 budget=0.0, input_class=None, via=None, upstream=None):
        self.device = device            # the sourcing DeviceItem, or None if unpowered
        self.port = port                # port number on the source, if known
        self.group = group              # that port's capability {standard, maxWatts, ...}
        self.assumed_port = assumed_port
        self.budget = budget            # total watts the source can offer downstream
        self.input_class = input_class  # class feeding the source itself (PD sources only)
        self.via = via or []            # pass-throughs (wall drops, patch panels) traversed
        self.upstream = upstream        # how the source is itself fed, when it's a PD

    @property
    def chain_assumed(self):
        """True if any hop in this power path relied on an unassigned port."""
        return self.assumed_port or bool(self.upstream and self.upstream.chain_assumed)

    @property
    def standard(self):
        return self.group["standard"] if self.group else None

    @property
    def port_max_watts(self):
        return self.group["maxWatts"] if self.group else 0.0


def _cable_ends(canvas_view, item_id, include_patch=True):
    """Every (far_id, far_port, cable) reachable from item_id over one cable.

    Generalizes CanvasView._other_field_cable_end, which returns only the first match --
    fine for a camera with one uplink, but a switch has many cables and we need to find
    the specific one leading to a power source. Patch cables are included by default
    because power crosses a passive jack over whatever cable happens to be on the far
    side; a canvas-drawn chain uses plain CAT6 for both hops, a rack-built one uses a
    patch cable for the second.
    """
    ends = []
    for cable in canvas_view.get_cables():
        if not include_patch and cable.cable_type == "Patch":
            continue
        anchors = cable.vertex_anchors
        if not anchors:
            continue
        first, last = anchors[0], anchors[-1]
        if item_id == first and last and last != item_id:
            ends.append((last, cable.end_port, cable))
        elif item_id == last and first and first != item_id:
            ends.append((first, cable.start_port, cable))
    return ends


def _field_cable_ends(canvas_view, item_id):
    """(far_id, far_port) over non-patch cables only -- the device's own connections."""
    return [(far, port) for far, port, _ in _cable_ends(canvas_view, item_id, include_patch=False)]


def _walk_through_pass_throughs(canvas_view, item_id, port, arrived_on, visited):
    """Follow wall drops / patch panels transparently to whatever is really on the far side.

    A passive jack carries power straight through, so the walk continues past it: first
    over a patch cable on the same port (the rack-editor convention that
    CanvasView.resolve_camera_chain follows), otherwise over any other cable attached to
    the jack. That second case is the residential one -- a canvas-drawn run reaches an AP
    through a wall drop using plain CAT6 on both sides, with no patch cable involved and
    frequently no port numbers at all.
    """
    via = []
    current_id, current_port, current_cable = item_id, port, arrived_on
    for _ in range(len(canvas_view.get_pass_through_devices()) + 1):
        item = canvas_view.find_device_or_camera_by_id(current_id)
        if item is None or getattr(item, "object_type", None) != "device":
            return None, None, via
        if not item.is_pass_through():
            return item, current_port, via
        if current_id in visited:
            return None, None, via  # looped back on itself
        visited.add(current_id)
        via.append(item)

        next_id, next_port = None, None
        if current_port is not None:
            next_id, next_port = canvas_view._other_patch_cable_end(current_id, current_port)
        if not next_id:
            for far_id, far_port, cable in _cable_ends(canvas_view, current_id):
                if cable is not current_cable and far_id not in visited:
                    next_id, next_port, current_cable = far_id, far_port, cable
                    break
        if not next_id:
            return None, None, via
        current_id, current_port = next_id, next_port
    return None, None, via


@_one_signature_per_call
def resolve_power_source(canvas_view, device, _seen=None):
    """Walk upstream from a powered device to whatever is actually feeding it.

    Recurses when the source is itself PoE-powered, since a PD-source's available budget
    can't be known until we know how *it* is fed.
    """
    if device is None:
        return PowerSource()
    top_level = _seen is None
    if top_level:
        cached = _cache_for(canvas_view)["sources"].get(device.id)
        if cached is not None:
            return cached
    _seen = _seen or set()
    if device.id in _seen:
        return PowerSource()
    _seen = _seen | {device.id}

    for far_id, far_port, cable in _cable_ends(canvas_view, device.id, include_patch=False):
        source, port, via = _walk_through_pass_throughs(
            canvas_view, far_id, far_port, cable, set())
        if source is None or source.id == device.id:
            continue
        if not sources_power(source):
            continue  # reachable, but can't put power on the wire (e.g. a Flex Mini)
        group, assumed = port_capability(source, port)
        if group is None:
            continue

        input_class, upstream = None, None
        if is_powered_device(source):
            # A PoE-powered source: its own uplink decides what it can pass on.
            upstream = resolve_power_source(canvas_view, source, _seen)
            input_class = upstream.standard
        budget = output_budget(source, input_class)
        resolved = PowerSource(device=source, port=port, group=group, assumed_port=assumed,
                               budget=budget, input_class=input_class, via=via,
                               upstream=upstream)
        if top_level:
            _cache["sources"][device.id] = resolved
        return resolved

    unpowered = PowerSource()
    if top_level:
        _cache["sources"][device.id] = unpowered
    return unpowered


@_one_signature_per_call
def powered_devices_on(canvas_view, source_device):
    """Every PD whose resolved power source is this device."""
    loads = []
    for candidate in canvas_view.get_network_devices() + canvas_view.get_cameras():
        if candidate.id == source_device.id or not is_powered_device(candidate):
            continue
        resolved = resolve_power_source(canvas_view, candidate)
        if resolved.device is not None and resolved.device.id == source_device.id:
            loads.append(candidate)
    return loads


# ── Validation ────────────────────────────────────────────────────────────────
def _watts(value):
    """Trim a trailing .0 so 21.0 reads as 21W but 18.5 keeps its half-watt."""
    return f"{value:g}W"


def _shortfall_hint(source_item, needed_watts, through=None):
    """Spell out which of `source_item`'s ports would actually carry `needed_watts`.

    `through` is the PoE-powered switch sitting between that port and the load, if any:
    what matters then isn't whether the port can deliver the watts directly, but how much
    budget the port's class leaves that switch to pass along. That indirection is the
    whole trap -- on a Pro 24, all 24 ports comfortably exceed 21W on their own, yet 16
    of them leave a Flex with only 20W, one watt short of a U7 Pro.
    """
    ok, short = [], []
    for group in port_groups(source_item):
        if through is not None:
            deliverable = output_budget(through, group["standard"])
        else:
            deliverable = group["maxWatts"]
        (ok if deliverable >= needed_watts else short).append((group, deliverable))
    if not short or not ok:
        return ""
    short_count = sum(g["count"] for g, _ in short)
    total = sum(g["count"] for g in port_groups(source_item))
    ok_classes = " / ".join(sorted({class_label(g["standard"]) for g, _ in ok}))
    worst = min(d for _, d in short)
    return (f" Requires a {ok_classes} port -- {short_count} of this device's {total} "
            f"PoE ports deliver only {_watts(worst)}.")


def _worst_case_budget(canvas_view, source):
    """Downstream watts if every unassigned port in this chain went the wrong way.

    Only meaningful for a PoE-powered source, where the uplink's class is what decides
    the budget -- a mains-powered switch offers the same total regardless of which port
    anything landed on.
    """
    if not source.device or not is_powered_device(source.device):
        return None
    upstream = source.upstream
    if not upstream or not upstream.device or not upstream.assumed_port:
        return None
    weakest = min((g["standard"] for g in port_groups(upstream.device) if g["standard"]),
                  key=lambda s: POE_CLASS_RANK[s], default=None)
    if weakest is None:
        return None
    return output_budget(source.device, weakest)


@_one_signature_per_call
def evaluate_device(canvas_view, device):
    """PoE problems attributable to `device`, both as a load and as a power source.

    Returns a list of (severity, message) where severity is "error" (this will not work
    as designed) or "warning" (works only under a condition we can't confirm, usually an
    unassigned port).
    """
    if getattr(device, "object_type", None) not in ("device", "camera"):
        return []
    cache = _cache_for(canvas_view)
    cached = cache["issues"].get(device.id)
    if cached is not None:
        return cached
    issues = []

    # ── As a load ──
    if is_powered_device(device):
        draw, estimated = power_draw(device)
        source = resolve_power_source(canvas_view, device)
        needed = required_class(device)
        approx = "~" if estimated else ""

        if source.device is None:
            issues.append(("warning", f"No PoE source reaches this device "
                                       f"({approx}{_watts(draw)} required)"))
        else:
            src_label = getattr(source.device, "label", "source")
            where = f"{src_label} port {source.port}" if source.port else src_label
            severity = "warning" if source.assumed_port else "error"
            qualifier = " (port not assigned -- assuming its best)" if source.assumed_port else ""

            # 1. Class negotiation
            if needed and source.standard and \
                    POE_CLASS_RANK[source.standard] < POE_CLASS_RANK[needed]:
                issues.append(("error",
                               f"Needs {class_label(needed)} but {where} only provides "
                               f"{class_label(source.standard)}"))

            # 2. Per-port ceiling. Skipped for a switch that re-sources power: it
            #    negotiates down to whatever the port offers and simply passes less
            #    along, so the meaningful complaint is the reduced downstream budget
            #    (reported below / on the switch itself), not the port being "too small".
            elif source.port_max_watts and draw > source.port_max_watts \
                    and not isinstance(_spec(device).get("poeOutputByInput"), dict):
                issues.append((severity,
                               f"Draws {approx}{_watts(draw)} but {where} tops out at "
                               f"{_watts(source.port_max_watts)}{qualifier}"))

            # 3. This device alone against the chain budget. The budget is what the
            #    source can pass on, which for a PoE-powered source depends on its own
            #    uplink -- this is where the Flex's 46/20/8W split bites.
            elif source.budget and draw > source.budget:
                basis = ""
                if source.input_class:
                    basis = (f" ({src_label} is fed {class_label(source.input_class)}, "
                             f"leaving {_watts(source.budget)} downstream)")
                elif is_powered_device(source.device):
                    basis = f" ({src_label} has no PoE uplink of its own)"
                issues.append((severity,
                               f"Draws {approx}{_watts(draw)} but only "
                               f"{_watts(source.budget)} is available{basis}"
                               f"{_shortfall_hint(source.device, draw)}"))

            # 4. It passes -- but only because we assumed the best of an unassigned
            #    port somewhere up the chain. If a weaker choice at that port would
            #    starve this device, say so rather than letting the optimism ride.
            elif source.chain_assumed:
                worst = _worst_case_budget(canvas_view, source)
                if worst is not None and worst < draw:
                    culprit, through = source.device, None
                    if source.upstream and source.upstream.assumed_port and source.upstream.device:
                        culprit, through = source.upstream.device, source.device
                    issues.append(("warning",
                                   f"Draws {approx}{_watts(draw)}, which works only if "
                                   f"{getattr(culprit, 'label', 'the uplink')}'s port is the "
                                   f"right kind -- it isn't assigned, so this isn't confirmed."
                                   f"{_shortfall_hint(culprit, draw, through=through)}"))

    # ── As a source ──
    if sources_power(device):
        loads = powered_devices_on(canvas_view, device)
        if loads:
            total = sum(power_draw(load)[0] for load in loads)
            upstream_class = None
            if is_powered_device(device):
                upstream_class = resolve_power_source(canvas_view, device).standard
            budget = output_budget(device, upstream_class)
            if budget and total > budget:
                basis = ""
                if is_powered_device(device):
                    basis = (f" -- fed {class_label(upstream_class)}, so only "
                             f"{_watts(budget)} is available downstream"
                             if upstream_class else " -- it has no PoE uplink of its own")
                issues.append(("error",
                               f"PoE budget exceeded: {len(loads)} device(s) draw "
                               f"{_watts(total)} of {_watts(budget)}{basis}"))
    elif is_powered_device(device) and isinstance(_spec(device).get("poeOutputByInput"), dict):
        # PoE-powered but sources nothing (Flex Mini): anything hung off it expecting
        # power is dead, and that's worth saying on the switch itself, not just the load.
        starved = [d for d in canvas_view.get_network_devices() + canvas_view.get_cameras()
                   if d.id != device.id and is_powered_device(d)
                   and any(far == device.id for far, _ in _field_cable_ends(canvas_view, d.id))]
        if starved:
            names = ", ".join(getattr(d, "label", "?") for d in starved)
            issues.append(("error", f"Provides no PoE output, but {names} "
                                     f"{'expects' if len(starved) == 1 else 'expect'} power from it"))
    cache["issues"][device.id] = issues
    return issues


@_one_signature_per_call
def evaluate_project(canvas_view):
    """Every PoE problem in the design: [(severity, device, message), ...]."""
    found = []
    for device in canvas_view.get_network_devices() + canvas_view.get_cameras():
        for severity, message in evaluate_device(canvas_view, device):
            found.append((severity, device, message))
    # Errors first, then warnings, each group in scene order.
    found.sort(key=lambda row: 0 if row[0] == "error" else 1)
    return found
