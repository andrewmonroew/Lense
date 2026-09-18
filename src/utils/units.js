/** Unit conversion utilities */

const METERS_PER_FOOT = 0.3048;
const FEET_PER_METER = 1 / METERS_PER_FOOT;

export function feetToMeters(feet) {
    return feet * METERS_PER_FOOT;
}

export function metersToFeet(meters) {
    return meters * FEET_PER_METER;
}

/** Format a distance value with appropriate unit label */
export function formatDistance(valueFeet, unit = 'feet') {
    if (unit === 'meters') {
        const m = feetToMeters(valueFeet);
        return m < 1 ? `${(m * 100).toFixed(0)} cm` : `${m.toFixed(1)} m`;
    }
    return valueFeet < 1 ? `${(valueFeet * 12).toFixed(0)} in` : `${valueFeet.toFixed(1)} ft`;
}

/** Format distance with both units */
export function formatDistanceBoth(valueFeet) {
    const ft = valueFeet.toFixed(1);
    const m = feetToMeters(valueFeet).toFixed(1);
    return `${ft} ft (${m} m)`;
}

export const CAT6_MAX_RUN_FEET = 300;
export const CAT6_MAX_RUN_METERS = feetToMeters(300);
