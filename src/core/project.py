import json
import os

class Project:
    def __init__(self):
        self.filepath = None
        self.floorplan_path = None
        self.scale_ratio = None      # Pixels per foot
        self.calibration_unit = "feet"
        self.calibration_dist = 0.0
        self.calibration_p1 = None   # Tuple (x, y)
        self.calibration_p2 = None   # Tuple (x, y)

        self.cameras = []            # List of dicts
        self.devices = []            # List of dicts (Switches / NVRs)
        self.cables = []             # List of dicts

    def clear(self):
        self.filepath = None
        self.floorplan_path = None
        self.scale_ratio = None
        self.calibration_unit = "feet"
        self.calibration_dist = 0.0
        self.calibration_p1 = None
        self.calibration_p2 = None
        self.cameras.clear()
        self.devices.clear()
        self.cables.clear()

    def save(self, filepath):
        self.filepath = filepath
        data = {
            "version": "1.0",
            "floorplan_path": self.floorplan_path,
            "scale_ratio": self.scale_ratio,
            "calibration_unit": self.calibration_unit,
            "calibration_dist": self.calibration_dist,
            "calibration_p1": self.calibration_p1,
            "calibration_p2": self.calibration_p2,
            "cameras": self.cameras,
            "devices": self.devices,
            "cables": self.cables
        }
        with open(filepath, 'w') as f:
            json.dump(data, f, indent=4)

    def load(self, filepath):
        self.clear()
        self.filepath = filepath
        with open(filepath, 'r') as f:
            data = json.load(f)

        self.floorplan_path = data.get("floorplan_path")
        self.scale_ratio = data.get("scale_ratio")
        self.calibration_unit = data.get("calibration_unit", "feet")
        self.calibration_dist = data.get("calibration_dist", 0.0)
        self.calibration_p1 = data.get("calibration_p1")
        self.calibration_p2 = data.get("calibration_p2")
        self.cameras = data.get("cameras", [])
        self.devices = data.get("devices", [])
        self.cables = data.get("cables", [])
