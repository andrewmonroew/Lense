/** Geometry utilities for canvas math */

/** Convert degrees to radians */
export function degToRad(deg) {
    return deg * (Math.PI / 180);
}

/** Convert radians to degrees */
export function radToDeg(rad) {
    return rad * (180 / Math.PI);
}

/** Distance between two points */
export function distance(x1, y1, x2, y2) {
    const dx = x2 - x1;
    const dy = y2 - y1;
    return Math.sqrt(dx * dx + dy * dy);
}

/** Angle from p1 to p2 in radians (0 = right, clockwise) */
export function angle(x1, y1, x2, y2) {
    return Math.atan2(y2 - y1, x2 - x1);
}

/** Normalize angle to [0, 2PI) */
export function normalizeAngle(a) {
    a = a % (2 * Math.PI);
    if (a < 0) a += 2 * Math.PI;
    return a;
}

/** Check if angle is within a cone defined by centerAngle ± halfAngle */
export function isAngleInCone(testAngle, centerAngle, halfAngle) {
    let diff = normalizeAngle(testAngle - centerAngle + Math.PI) - Math.PI;
    return Math.abs(diff) <= halfAngle;
}

/** Check if point is inside a convex polygon (array of {x,y}) */
export function pointInPolygon(px, py, polygon) {
    let inside = false;
    for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
        const xi = polygon[i].x, yi = polygon[i].y;
        const xj = polygon[j].x, yj = polygon[j].y;
        if ((yi > py) !== (yj > py) && px < (xj - xi) * (py - yi) / (yj - yi) + xi) {
            inside = !inside;
        }
    }
    return inside;
}

/** Check if point is within a circle */
export function pointInCircle(px, py, cx, cy, r) {
    return distance(px, py, cx, cy) <= r;
}

/** Check if point is within a pie slice (sector) */
export function pointInSector(px, py, cx, cy, radius, startAngle, endAngle) {
    const d = distance(px, py, cx, cy);
    if (d > radius) return false;
    const a = normalizeAngle(Math.atan2(py - cy, px - cx));
    const s = normalizeAngle(startAngle);
    const e = normalizeAngle(endAngle);
    if (s < e) {
        return a >= s && a <= e;
    }
    return a >= s || a <= e;
}

/** Snap angle to nearest N degrees */
export function snapAngle(angleDeg, snapDeg) {
    return Math.round(angleDeg / snapDeg) * snapDeg;
}

/** Polyline total length from array of {x,y} */
export function polylineLength(points) {
    let total = 0;
    for (let i = 1; i < points.length; i++) {
        total += distance(points[i - 1].x, points[i - 1].y, points[i].x, points[i].y);
    }
    return total;
}

/** Closest point on a line segment to a point */
export function closestPointOnSegment(px, py, ax, ay, bx, by) {
    const dx = bx - ax, dy = by - ay;
    const len2 = dx * dx + dy * dy;
    if (len2 === 0) return { x: ax, y: ay, t: 0 };
    let t = ((px - ax) * dx + (py - ay) * dy) / len2;
    t = Math.max(0, Math.min(1, t));
    return { x: ax + t * dx, y: ay + t * dy, t };
}

/** Distance from point to polyline */
export function distanceToPolyline(px, py, points) {
    let minDist = Infinity;
    for (let i = 1; i < points.length; i++) {
        const cp = closestPointOnSegment(px, py, points[i-1].x, points[i-1].y, points[i].x, points[i].y);
        const d = distance(px, py, cp.x, cp.y);
        if (d < minDist) minDist = d;
    }
    return minDist;
}

/** Generate unique ID */
let _idCounter = 0;
export function uid() {
    return `obj_${Date.now()}_${++_idCounter}`;
}
