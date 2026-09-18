import math
import os
import sys

# Constants
METERS_PER_FOOT = 0.3048
FEET_PER_METER = 1.0 / METERS_PER_FOOT
CAT6_MAX_RUN_FEET = 300.0
FIBER_MAX_RUN_FEET = 10000.0  # single-mode fiber runs far beyond copper's 300ft limit
PATCH_MAX_RUN_FEET = 10.0  # patch cables are short jumpers within a rack, not field runs
CATALOG_SPEC_MIME_TYPE = "application/x-lense-spec-id"  # Drag payload: catalog tree -> canvas
# Per-step zoom multiplier shared by every QGraphicsView with wheel-zoom (CanvasView,
# RackElevationView, NetworkDiagramView) -- was 1.15 (15% per wheel tick/zoom button
# press), toned down by half to 1.075 (7.5%) since 15% felt too aggressive.
ZOOM_STEP = 1.075

def get_data_dir():
    """Resolves the src/data directory, whether running from source or a frozen build."""
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, "data")
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_dir, "data")

def get_logs_dir():
    """Resolves (and creates if missing) the project's logs directory."""
    if getattr(sys, 'frozen', False):
        base_dir = os.path.dirname(sys.executable)
    else:
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    logs_dir = os.path.join(base_dir, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    return logs_dir

def get_ir_range_feet(spec, default):
    """Safely reads a camera spec's IR range in feet. Some cameras (e.g. color-only
    night vision with no IR illuminator) deliberately store irRange as null rather than
    omitting the key -- spec.get("irRange", {}).get("feet", default) does NOT fall back
    in that case, since the "irRange" key is present (its value is just None), so the
    dict-level default never triggers and None reaches arithmetic downstream."""
    ir_range = spec.get("irRange") or {}
    feet = ir_range.get("feet")
    return feet if feet is not None else default

def deg_to_rad(deg):
    return deg * (math.pi / 180.0)

def rad_to_deg(rad):
    return rad * (180.0 / math.pi)

def distance(p1, p2):
    """Calculates distance between two points (QPointF, tuple, or list)."""
    try:
        dx = p2.x() - p1.x()
        dy = p2.y() - p1.y()
    except AttributeError:
        dx = p2[0] - p1[0]
        dy = p2[1] - p1[1]
    return math.sqrt(dx * dx + dy * dy)

def point_hits_item(item, pt, radius=20.0):
    """Whether a scene point should count as landing on `item`.

    A plain centre-distance test is wrong for anything physically large. A rack icon is
    34x44, so its top and bottom edges fall outside a 20px radius of its centre, and a
    cable dropped squarely on the visible icon would silently fail to anchor -- which
    then means no pitchfork stub in the Rack Editor. Anywhere inside the item's own
    shape counts as a hit too, with the radius still applying as a bit of slop around
    small icons.
    """
    try:
        if distance((pt.x(), pt.y()), (item.x(), item.y())) <= radius:
            return True
        return bool(item.shape().contains(item.mapFromScene(pt)))
    except AttributeError:
        return False


def angle_between(p1, p2):
    """Calculates angle from p1 to p2 in radians."""
    try:
        return math.atan2(p2.y() - p1.y(), p2.x() - p1.x())
    except AttributeError:
        return math.atan2(p2[1] - p1[1], p2[0] - p1[0])

def normalize_angle(rad):
    """Normalizes angle to [0, 2pi)."""
    rad = rad % (2 * math.pi)
    if rad < 0:
        rad += 2 * math.pi
    return rad

def format_distance(value_feet, unit='feet'):
    """Formats a distance value with appropriate unit label."""
    if unit == 'meters':
        m = value_feet * METERS_PER_FOOT
        return f"{m:.1f} m"
    return f"{value_feet:.1f} ft"

def format_distance_both(value_feet):
    """Formats a distance value showing both feet and meters."""
    m = value_feet * METERS_PER_FOOT
    return f"{value_feet:.1f} ft ({m:.1f} m)"

def closest_point_on_segment(p, a, b):
    """Finds the closest point on line segment ab to point p.
    All points are tuples (x, y) or list.
    """
    px, py = p[0], p[1]
    ax, ay = a[0], a[1]
    bx, by = b[0], b[1]

    dx = bx - ax
    dy = by - ay
    len2 = dx * dx + dy * dy
    if len2 == 0:
        return (ax, ay), 0.0

    t = ((px - ax) * dx + (py - ay) * dy) / len2
    t = max(0.0, min(1.0, t))
    return (ax + t * dx, ay + t * dy), t

def segment_intersection(p1, p2, p3, p4):
    """Where segments p1-p2 and p3-p4 cross, as (x, y), or None if they don't.

    Points are (x, y) tuples. Parallel/collinear segments report no crossing -- there's
    no single point to pick, and for the Lasso tool "graze a cable lengthwise" isn't a
    meaningful hit anyway.
    """
    x1, y1 = p1
    x2, y2 = p2
    x3, y3 = p3
    x4, y4 = p4
    denom = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(denom) < 1e-9:
        return None
    t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / denom
    u = ((x1 - x3) * (y1 - y2) - (y1 - y3) * (x1 - x2)) / denom
    if 0.0 <= t <= 1.0 and 0.0 <= u <= 1.0:
        return (x1 + t * (x2 - x1), y1 + t * (y2 - y1))
    return None


def distance_to_segment(p, a, b):
    """Finds the distance from point p to line segment ab."""
    cp, _ = closest_point_on_segment(p, a, b)
    return distance(p, cp)

def compute_ground_range(elevation_ft, downtilt_deg, vertical_fov_deg, max_range_ft):
    """Projects a camera's vertical FOV cone onto the ground plane.

    A camera mounted at elevation_ft, tilted down by downtilt_deg from horizontal,
    with a vertical field of view of vertical_fov_deg, sees a band on the ground
    rather than a disc centered on itself: the upper edge of the cone (shallower
    angle) reaches farther out, the lower edge (steeper angle) reaches closer in,
    and if the lower edge doesn't reach straight down there's a blind spot
    directly beneath the camera.

    Ground distance for a ray at angle a below horizontal: elevation_ft / tan(a).

    Returns (near_ft, far_ft): the near edge (blind-spot radius, 0 if none) and
    far edge (capped at max_range_ft, the camera's rated IR/detection range) of
    the visible ground band.
    """
    # Defend against None/negative/out-of-range values reaching here (e.g. an older or
    # hand-edited save file) -- this runs from paint(), so a bad value must never raise.
    elevation_ft = max(0.0, elevation_ft or 0.0)
    downtilt_deg = max(0.0, min(90.0, downtilt_deg or 0.0))
    vertical_fov_deg = max(0.0, vertical_fov_deg or 0.0)
    max_range_ft = max(0.0, max_range_ft or 0.0)

    half_vfov = vertical_fov_deg / 2.0
    top_angle = downtilt_deg - half_vfov     # upper cone edge, degrees below horizontal
    bottom_angle = downtilt_deg + half_vfov  # lower cone edge, degrees below horizontal

    epsilon = 1e-6  # guards tan() against exact/near 0 and 90 degree singularities

    if top_angle <= epsilon:
        # Upper edge points at/above the horizon -- not limited by the ground,
        # so the far edge is just the camera's rated max range.
        far_ft = max_range_ft
    else:
        far_ft = min(max_range_ft, elevation_ft / math.tan(deg_to_rad(top_angle)))

    if bottom_angle >= 90.0 - epsilon:
        near_ft = 0.0
    elif bottom_angle <= epsilon:
        near_ft = 0.0
    else:
        near_ft = min(elevation_ft / math.tan(deg_to_rad(bottom_angle)), far_ft)

    return near_ft, far_ft
