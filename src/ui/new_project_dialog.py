from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel
from PySide6.QtCore import Qt


class NewProjectDialog(QDialog):
    """Lets the user pick which kind of project File > New Project should create.
    Both project types share the exact same underlying scene/save format/undo system/
    catalog dock/Rack Editor/Network Diagram tab -- this dialog only decides the
    `project_type` flag MainWindow._apply_project_mode() reacts to, not a separate
    codepath. Two big buttons rather than a combo box + OK, since there are only two
    options and a click should just commit immediately."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New Project")
        self.setMinimumWidth(420)
        self._chosen_type = None

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("What are you designing?"))

        btn_row = QHBoxLayout()
        btn_row.addWidget(self._build_option(
            "CCTV / Security Design",
            "Floor plan, camera placement/FOV, racks, and cabling -- the full design workflow.",
            "cctv"))
        btn_row.addWidget(self._build_option(
            "Network Topology",
            "A lighter workflow for laying out switches, access points, and wall drops "
            "without needing a floor plan or camera-specific tools.",
            "network_topology"))
        layout.addLayout(btn_row)

        cancel_row = QHBoxLayout()
        cancel_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        cancel_row.addWidget(cancel_btn)
        layout.addLayout(cancel_row)

    def _build_option(self, title, description, project_type):
        btn = QPushButton()
        btn.setMinimumHeight(90)
        inner = QVBoxLayout(btn)
        title_lbl = QLabel(title)
        title_lbl.setStyleSheet("font-weight: bold; font-size: 13px;")
        title_lbl.setAlignment(Qt.AlignCenter)
        desc_lbl = QLabel(description)
        desc_lbl.setWordWrap(True)
        desc_lbl.setAlignment(Qt.AlignCenter)
        desc_lbl.setStyleSheet("font-size: 10px;")
        inner.addWidget(title_lbl)
        inner.addWidget(desc_lbl)
        btn.clicked.connect(lambda: self._choose(project_type))
        return btn

    def _choose(self, project_type):
        self._chosen_type = project_type
        self.accept()

    @staticmethod
    def get_project_type(parent=None):
        """Shows the dialog modally; returns "cctv"/"network_topology", or None if
        the user cancelled (dialog closed without picking an option)."""
        dlg = NewProjectDialog(parent)
        if dlg.exec() == QDialog.Accepted:
            return dlg._chosen_type
        return None
