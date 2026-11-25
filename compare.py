"""Side-by-side compare window: two photos large, with details and zoom."""
from __future__ import annotations

import datetime as _dt
import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout,
)

import engine


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def photo_details(path: str) -> dict:
    """Resolution, size and dates for a photo (reads only the file header)."""
    from PIL import Image

    d = {"name": os.path.basename(path), "folder": os.path.dirname(path), "path": path,
         "w": 0, "h": 0, "size": 0, "modified": "", "code": engine.item_code(path) or ""}
    try:
        st = os.stat(path)
        d["size"] = st.st_size
        d["modified"] = _dt.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
        with Image.open(path) as im:
            d["w"], d["h"] = im.size
    except Exception:
        pass
    return d


def quality_key(path: str) -> tuple[int, int]:
    """Higher = better copy to keep: more pixels, then bigger file."""
    d = photo_details(path)
    return (d["w"] * d["h"], d["size"])


class _Side(QFrame):
    def __init__(self, path: str, title: str):
        super().__init__()
        self.path = path
        self.setObjectName("cmpSide")
        v = QVBoxLayout(self)
        head = QLabel(title)
        head.setObjectName("cmpTitle")
        v.addWidget(head)
        self.scroll = QScrollArea()
        self.scroll.setAlignment(Qt.AlignCenter)
        self.scroll.setWidgetResizable(False)
        self.img = QLabel()
        self.img.setAlignment(Qt.AlignCenter)
        self.scroll.setWidget(self.img)
        v.addWidget(self.scroll, 1)
        self.info = QLabel()
        self.info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.info.setWordWrap(True)
        v.addWidget(self.info)
        self.pix = QPixmap()
        try:
            im = engine.load_image(path, max_side=2400).convert("RGB")
            data = im.tobytes("raw", "RGB")
            qimg = QImage(data, im.width, im.height, 3 * im.width, QImage.Format_RGB888).copy()
            self.pix = QPixmap.fromImage(qimg)
        except Exception:
            self.img.setText("Cannot open this file")

    def set_details(self, d: dict, other: dict, sim_text: str):
        def mark(val, better):
            return f"<b style='color:#2e7d32'>{val} ✔</b>" if better else val

        px, opx = d["w"] * d["h"], other["w"] * other["h"]
        res = mark(f"{d['w']} x {d['h']}", px > opx)
        size = mark(_human_size(d["size"]), d["size"] > other["size"] and px >= opx)
        self.info.setText(
            f"<b>{d['name']}</b><br>"
            f"Item code: {d['code'] or '-'}<br>"
            f"Resolution: {res}<br>Size: {size}<br>Modified: {d['modified']}<br>"
            f"<span style='color:#6c757d'>{d['folder']}</span>"
            + (f"<br>{sim_text}" if sim_text else "")
        )

    def show_scaled(self, fit: bool):
        if self.pix.isNull():
            return
        if fit:
            vp = self.scroll.viewport().size()
            pm = self.pix.scaled(vp.width() - 4, vp.height() - 4, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        else:
            pm = self.pix
        self.img.setPixmap(pm)
        self.img.resize(pm.size())


class CompareDialog(QDialog):
    """Returns via .result_action: None, ('delete', path) """

    def __init__(self, left: str, right: str, similarity: float | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Compare photos")
        self.resize(1200, 780)
        self.result_action = None
        self.fit = True
        dl, dr = photo_details(left), photo_details(right)
        sim = f"Similarity: <b>{engine.to_percent(similarity)}%</b>" if similarity is not None else ""

        lay = QVBoxLayout(self)
        grid = QGridLayout()
        self.left, self.right = _Side(left, "Left"), _Side(right, "Right")
        self.left.set_details(dl, dr, sim)
        self.right.set_details(dr, dl, sim)
        grid.addWidget(self.left, 0, 0)
        grid.addWidget(self.right, 0, 1)
        lay.addLayout(grid, 1)

        # keep both views scrolled together when zoomed in
        for a, b in ((self.left, self.right), (self.right, self.left)):
            a.scroll.horizontalScrollBar().valueChanged.connect(b.scroll.horizontalScrollBar().setValue)
            a.scroll.verticalScrollBar().valueChanged.connect(b.scroll.verticalScrollBar().setValue)

        row = QHBoxLayout()
        self.btn_zoom = QPushButton("Zoom 100%")
        self.btn_zoom.clicked.connect(self.toggle_zoom)
        row.addWidget(self.btn_zoom)
        row.addStretch()
        b1 = QPushButton("Keep left  •  delete right")
        b2 = QPushButton("Keep right  •  delete left")
        for b in (b1, b2):
            b.setObjectName("danger")
        b1.clicked.connect(lambda: self._done(("delete", right)))
        b2.clicked.connect(lambda: self._done(("delete", left)))
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        row.addWidget(b1)
        row.addWidget(b2)
        row.addSpacing(16)
        row.addWidget(close)
        lay.addLayout(row)
        hint = QLabel("Green ✔ marks the higher-quality copy. Deleting can be undone (Ctrl+Z) until you close ItemFinder.")
        hint.setObjectName("hint")
        lay.addWidget(hint)

    def _done(self, action):
        self.result_action = action
        self.accept()

    def toggle_zoom(self):
        self.fit = not self.fit
        self.btn_zoom.setText("Fit to window" if not self.fit else "Zoom 100%")
        self.left.show_scaled(self.fit)
        self.right.show_scaled(self.fit)

    def showEvent(self, e):
        super().showEvent(e)
        self.left.show_scaled(self.fit)
        self.right.show_scaled(self.fit)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.fit:
            self.left.show_scaled(True)
            self.right.show_scaled(True)
