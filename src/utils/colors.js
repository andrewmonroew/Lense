/** Color constants for FOV visualization and equipment */

export const FOV_COLORS = {
    identify: {
        fill: 'hsla(140, 70%, 45%, 0.30)',
        stroke: 'hsla(140, 70%, 45%, 0.60)',
        label: 'ID Zone'
    },
    recognize: {
        fill: 'hsla(45, 90%, 55%, 0.22)',
        stroke: 'hsla(45, 90%, 55%, 0.50)',
        label: 'Recognition'
    },
    detect: {
        fill: 'hsla(0, 75%, 50%, 0.15)',
        stroke: 'hsla(0, 75%, 50%, 0.40)',
        label: 'Detection'
    },
    irRange: {
        fill: 'hsla(260, 60%, 55%, 0.08)',
        stroke: 'hsla(260, 60%, 55%, 0.35)',
        label: 'IR Range'
    }
};

export const EQUIPMENT_COLORS = {
    camera: '#22c55e',
    switch: '#3b82f6',
    nvr: '#8b5cf6',
    cable: '#60a5fa',
    cableError: '#ef4444',
    cableBundle: '#818cf8'
};

export const SELECTION_COLOR = '#60a5fa';
export const CALIBRATION_COLOR = '#f59e0b';
export const MEASUREMENT_COLOR = '#06b6d4';
