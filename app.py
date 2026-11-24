"""
ItemFinder - find duplicate / same items in your photo library and clean them up.
Runs 100% offline. Photos never leave this computer.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import traceback

from PySide6.QtCore import QObject, QRunnable, QSize, Qt, QThread, QThreadPool, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QImage, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog, QFileDialog, QHBoxLayout, QInputDialog,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QProgressBar,
    QPushButton, QSpinBox, QSplitter, QTabWidget, QVBoxLayout, QWidget,
)

import engine
import updater
import version

APP_NAME = "ItemFinder"
THUMB = 170
PATH_ROLE = Qt.UserRole
SCORE_ROLE = Qt.UserRole + 1
PIXELS_ROLE = Qt.UserRole + 2
GROUP_ROLE = Qt.UserRole + 3
FLAG_ROLE = Qt.UserRole + 4
WARN_BG, WARN_FG, WARN_BORDER = "#ffe3c2", "#8a3d00", "#f08c00"

STRICTNESS = [
    ("Exact copies only (resized / re-saved)", 0.97),
    ("Same item - strict", 0.94),
    ("Same item - recommended", 0.91),
    ("Same item - loose (different angles)", 0.88),
    ("Similar-looking items", 0.84),
]


# =========================================================================== helpers
def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def open_file(path: str):
    if sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def show_in_folder(path: str):
    if sys.platform.startswith("win"):
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    else:
        open_file(os.path.dirname(path))


def pil_to_qimage(im) -> QImage:
    im = im.convert("RGB")
    data = im.tobytes("raw", "RGB")
    return QImage(data, im.width, im.height, 3 * im.width, QImage.Format_RGB888).copy()


# =========================================================================== thumbnails
class ThumbSignals(QObject):
    done = Signal(str, QImage, str, int)  # path, image, info text, pixel count


class ThumbTask(QRunnable):
    def __init__(self, path: str, sig: ThumbSignals):
        super().__init__()
        self.path, self.sig = path, sig

    def run(self):
        from PIL import Image

        info, pixels, qimg = "", 0, QImage()
        try:
            size = os.path.getsize(self.path)
            with Image.open(self.path) as raw:
                w, h = raw.size
            pixels = w * h
            info = f"{w}x{h}  ·  {human_size(size)}"
            im = engine.load_image(self.path, max_side=THUMB)
            im.thumbnail((THUMB, THUMB))
            qimg = pil_to_qimage(im)
        except Exception as e:
            info = f"Cannot open: {e}"[:80]
        self.sig.done.emit(self.path, qimg, info, pixels)


class ThumbLoader(QObject):
    """Shared background thumbnail loader with a small cache."""
    ready = Signal(str, QImage, str, int)

    def __init__(self):
        super().__init__()
        self.pool = QThreadPool()
        self.pool.setMaxThreadCount(max(2, min(6, (os.cpu_count() or 4) - 1)))
        self.cache: dict[str, tuple[QImage, str, int]] = {}
        self.pending: set[str] = set()
        self.sig = ThumbSignals()
        self.sig.done.connect(self._done)

    def request(self, path: str, priority: int = 0):
        if path in self.cache:
            self.ready.emit(path, *self.cache[path])
            return
        if path in self.pending:
            return
        self.pending.add(path)
        self.pool.start(ThumbTask(path, self.sig), priority)

    def forget(self, path: str):
        self.cache.pop(path, None)

    def _done(self, path, img, info, pixels):
        self.pending.discard(path)
        if len(self.cache) > 4000:
            self.cache.clear()
        self.cache[path] = (img, info, pixels)
        self.ready.emit(path, img, info, pixels)


# =========================================================================== workers
class ModelLoader(QThread):
    status = Signal(str)
    ready = Signal(object)
    failed = Signal(str)

    def run(self):
        try:
            self.ready.emit(engine.Embedder(status=self.status.emit))
        except Exception:
            self.failed.emit(traceback.format_exc())


class ScanWorker(QThread):
    progress = Signal(int, int, str)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, index, embedder):
        super().__init__()
        self.index, self.embedder, self.cancel = index, embedder, threading.Event()

    def run(self):
        try:
            self.done.emit(self.index.scan(self.embedder, self.progress.emit, self.cancel))
        except Exception:
            self.failed.emit(traceback.format_exc())


class DupWorker(QThread):
    progress = Signal(int, int, str)
    done = Signal(list)
    failed = Signal(str)

    def __init__(self, index, threshold):
        super().__init__()
        self.index, self.threshold, self.cancel = index, threshold, threading.Event()

    def run(self):
        try:
            self.progress.emit(0, 0, "Loading index...")
            self.done.emit(self.index.find_duplicates(self.threshold, self.progress.emit, self.cancel))
        except Exception:
            self.failed.emit(traceback.format_exc())


class CodeWorker(QThread):
    progress = Signal(int, int, str)
    done = Signal(list)
    failed = Signal(str)

    def __init__(self, index):
        super().__init__()
        self.index, self.cancel = index, threading.Event()

    def run(self):
        try:
            self.progress.emit(0, 0, "Loading index...")
            self.done.emit(self.index.find_code_conflicts(progress=self.progress.emit, cancel=self.cancel))
        except Exception:
            self.failed.emit(traceback.format_exc())


class SearchWorker(QThread):
    done = Signal(list, bool, list)  # results, text_only, same-code [(path, sim or -1)]
    failed = Signal(str)

    def __init__(self, index, embedder, image, image_path, text, top_k):
        super().__init__()
        self.index, self.embedder = index, embedder
        self.image, self.image_path, self.text, self.top_k = image, image_path, text, top_k

    def run(self):
        try:
            iv = tv = None
            if self.image_path:
                iv = self.index.vector_for(self.image_path)
            if iv is None and self.image is not None:
                iv = self.embedder.embed_image(self.image)
            if self.text:
                tv = self.embedder.embed_text(self.text)
            q = engine.combine(iv, tv, 0.35)
            same = []
            if self.image_path:
                for p in self.index.same_code(self.image_path):
                    v = self.index.vector_for(p)
                    same.append((p, float(v @ iv) if (v is not None and iv is not None) else -1.0))
            self.done.emit(self.index.search(q, self.top_k), iv is None, same)
        except Exception:
            self.failed.emit(traceback.format_exc())


class UpdateCheckWorker(QThread):
    done = Signal(object, bool)  # UpdateInfo or None, manual?
    failed = Signal(str, bool)

    def __init__(self, manual: bool):
        super().__init__()
        self.manual = manual

    def run(self):
        try:
            self.done.emit(updater.check(ignore_skipped=self.manual), self.manual)
        except Exception as e:
            self.failed.emit(str(e), self.manual)


class UpdateDownloadWorker(QThread):
    progress = Signal(int, int)
    done = Signal(str)
    failed = Signal(str)

    def __init__(self, info):
        super().__init__()
        self.info, self.cancel = info, threading.Event()

    def run(self):
        try:
            self.done.emit(updater.download(self.info, self.progress.emit, self.cancel))
        except InterruptedError:
            self.failed.emit("")
        except Exception as e:
            self.failed.emit(str(e))


# =========================================================================== photo grid
class PhotoGrid(QListWidget):
    action = Signal(str, object)  # action name, PhotoGrid

    def __init__(self, thumbs: ThumbLoader, priority: int = 5):
        super().__init__()
        self.thumbs, self.priority = thumbs, priority
        self.items: dict[str, QListWidgetItem] = {}
        self.setViewMode(QListWidget.IconMode)
        self.setIconSize(QSize(THUMB, THUMB))
        self.setGridSize(QSize(THUMB + 40, THUMB + 78))
        self.setResizeMode(QListWidget.Adjust)
        self.setMovement(QListWidget.Static)
        self.setWordWrap(True)
        self.setUniformItemSizes(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setSpacing(4)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        self.itemDoubleClicked.connect(lambda it: open_file(it.data(PATH_ROLE)))
        thumbs.ready.connect(self._thumb_ready)
        blank = QPixmap(THUMB, THUMB)
        blank.fill(QColor("#e9ecef"))
        self.placeholder = QIcon(blank)

    def clear_photos(self):
        self.clear()
        self.items.clear()

    def add_photo(self, path: str, score_text: str = "", flag: str = ""):
        """flag: warning text (e.g. 'SAME CODE') - the photo is highlighted orange."""
        it = QListWidgetItem(self.placeholder, self._label(path, score_text, "", flag))
        it.setData(PATH_ROLE, path)
        it.setData(SCORE_ROLE, score_text)
        it.setData(FLAG_ROLE, flag)
        if flag:
            it.setBackground(QColor(WARN_BG))
            it.setForeground(QColor(WARN_FG))
        it.setData(PIXELS_ROLE, 0)
        it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
        it.setCheckState(Qt.Unchecked)
        it.setToolTip(path)
        self.addItem(it)
        self.items[path] = it
        self.thumbs.request(path, self.priority)

    @staticmethod
    def _label(path, score, info, flag=""):
        name = os.path.basename(path)
        if len(name) > 28:
            name = name[:12] + "..." + name[-13:]
        lines = [f"\u26a0 {flag}"] if flag else []
        lines.append(name)
        if score:
            lines.append(score)
        if info:
            lines.append(info)
        return "\n".join(lines)

    def _thumb_ready(self, path, img, info, pixels):
        it = self.items.get(path)
        if it is None:
            return
        if not img.isNull():
            pm = QPixmap.fromImage(img)
            if it.data(FLAG_ROLE):
                pm = self._warn_frame(pm)
            it.setIcon(QIcon(pm))
        it.setData(PIXELS_ROLE, pixels)
        it.setText(self._label(path, it.data(SCORE_ROLE), info, it.data(FLAG_ROLE) or ""))
        it.setToolTip(f"{path}\n{info}")

    @staticmethod
    def _warn_frame(pm: QPixmap) -> QPixmap:
        """Thick orange border around a thumbnail."""
        from PySide6.QtGui import QPainter, QPen

        out = QPixmap(THUMB, THUMB)
        out.fill(Qt.transparent)
        p = QPainter(out)
        p.drawPixmap((THUMB - pm.width()) // 2, (THUMB - pm.height()) // 2, pm)
        pen = QPen(QColor(WARN_BORDER))
        pen.setWidth(8)
        p.setPen(pen)
        p.drawRect(4, 4, THUMB - 8, THUMB - 8)
        p.end()
        return out

    def targets(self) -> list[str]:
        checked = [it.data(PATH_ROLE) for it in self.items.values() if it.checkState() == Qt.Checked]
        return checked or [it.data(PATH_ROLE) for it in self.selectedItems()]

    def remove_path(self, path: str):
        it = self.items.pop(path, None)
        if it is not None:
            self.takeItem(self.row(it))

    def rename_path(self, old: str, new: str):
        it = self.items.pop(old, None)
        if it is None:
            return
        it.setData(PATH_ROLE, new)
        self.items[new] = it
        self.thumbs.forget(old)
        it.setText(self._label(new, it.data(SCORE_ROLE), "", it.data(FLAG_ROLE) or ""))
        it.setToolTip(new)
        self.thumbs.request(new, self.priority)

    def all_paths(self) -> list[str]:
        return [self.item(i).data(PATH_ROLE) for i in range(self.count())]

    def _menu(self, pos):
        it = self.itemAt(pos)
        if it is not None and not it.isSelected() and not self.targets():
            self.setCurrentItem(it)
        m = QMenu(self)
        for label, name in [
            ("Open", "open"), ("Show in folder", "show"), (None, None),
            ("Rename...  (F2)", "rename"), ("Move to folder...", "move"),
            ("Delete to Recycle Bin  (Del)", "delete"), (None, None),
            ("Find items like this", "similar"), ("Copy file path", "copy"),
        ]:
            if label is None:
                m.addSeparator()
            else:
                m.addAction(label, lambda n=name: self.action.emit(n, self))
        m.exec(self.mapToGlobal(pos))

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Delete:
            self.action.emit("delete", self)
        elif e.key() == Qt.Key_F2:
            self.action.emit("rename", self)
        elif e.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.action.emit("open", self)
        else:
            super().keyPressEvent(e)


# =========================================================================== drop zone
class DropZone(QLabel):
    dropped = Signal(object, str)  # PIL image, path ("" if pasted)

    def __init__(self):
        super().__init__("Drag & drop a photo here\n\nor use Browse / Paste")
        self.setAlignment(Qt.AlignCenter)
        self.setAcceptDrops(True)
        self.setMinimumSize(240, 240)
        self.setObjectName("dropzone")
        self.setWordWrap(True)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() or e.mimeData().hasImage():
            e.acceptProposedAction()

    def dropEvent(self, e):
        md = e.mimeData()
        if md.hasUrls():
            for u in md.urls():
                p = u.toLocalFile()
                if p and os.path.splitext(p)[1].lower() in engine.IMAGE_EXTS:
                    self.load_path(p)
                    return
        if md.hasImage():
            self.load_qimage(QImage(md.imageData()))

    def load_path(self, path: str):
        try:
            im = engine.load_image(path)
        except Exception as ex:
            QMessageBox.warning(self, APP_NAME, f"Could not open image:\n{ex}")
            return
        self._show(im)
        self.dropped.emit(im, path)

    def load_qimage(self, qimg: QImage):
        from PIL import Image

        qimg = qimg.convertToFormat(QImage.Format_RGB888)
        w, h = qimg.width(), qimg.height()
        ptr = qimg.constBits()
        data = bytes(ptr)[: qimg.bytesPerLine() * h]
        im = Image.frombuffer("RGB", (w, h), data, "raw", "RGB", qimg.bytesPerLine(), 1).copy()
        self._show(im)
        self.dropped.emit(im, "")

    def _show(self, im):
        t = im.copy()
        t.thumbnail((230, 230))
        self.setPixmap(QPixmap.fromImage(pil_to_qimage(t)))


# =========================================================================== folders dialog
class FoldersDialog(QDialog):
    def __init__(self, index: engine.PhotoIndex, parent=None):
        super().__init__(parent)
        self.index, self.changed = index, False
        self.setWindowTitle("Photo folders")
        self.resize(560, 320)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("ItemFinder scans these folders (and all sub-folders) for photos:"))
        self.list = QListWidget()
        lay.addWidget(self.list)
        row = QHBoxLayout()
        add, rem, close = QPushButton("Add folder..."), QPushButton("Remove"), QPushButton("Done")
        add.clicked.connect(self._add)
        rem.clicked.connect(self._remove)
        close.clicked.connect(self.accept)
        row.addWidget(add)
        row.addWidget(rem)
        row.addStretch()
        row.addWidget(close)
        lay.addLayout(row)
        self._refresh()

    def _refresh(self):
        self.list.clear()
        self.list.addItems(self.index.folders())

    def _add(self):
        d = QFileDialog.getExistingDirectory(self, "Choose your photo folder")
        if d:
            self.index.add_folder(d)
            self.changed = True
            self._refresh()

    def _remove(self):
        it = self.list.currentItem()
        if it:
            self.index.remove_folder(it.text())
            self.changed = True
            self._refresh()


# =========================================================================== main window
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} - duplicate item finder (offline)")
        self.resize(1280, 820)
        self.setAcceptDrops(True)
        self.index = engine.PhotoIndex()
        self.embedder = None
        self.scan_worker = self.dup_worker = self.search_worker = None
        self.upd_check = self.upd_dl = None
        self.groups: dict[int, list[str]] = {}
        self.group_info: dict[int, dict] = {}
        self.query_image = None
        self.query_path = ""
        self.thumbs = ThumbLoader()
        self.thumbs.ready.connect(self._group_icon_ready)

        # ---- top bar
        top = QHBoxLayout()
        self.btn_folders = QPushButton("Photo folders...")
        self.btn_scan = QPushButton("Update index")
        self.btn_folders.clicked.connect(self.manage_folders)
        self.btn_scan.clicked.connect(self.start_scan)
        self.lbl_count = QLabel()
        top.addWidget(self.btn_folders)
        top.addWidget(self.btn_scan)
        top.addSpacing(12)
        top.addWidget(self.lbl_count)
        top.addStretch()
        privacy = QLabel("Offline  ·  photos never leave this PC")
        privacy.setObjectName("privacy")
        top.addWidget(privacy)
        top.addSpacing(12)
        ver = QLabel(f"v{version.__version__}")
        ver.setObjectName("hint")
        top.addWidget(ver)
        self.btn_update = QPushButton("Check for updates")
        self.btn_update.clicked.connect(lambda: self.check_updates(manual=True))
        top.addWidget(self.btn_update)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_dup_tab(), "Find duplicates")
        self.tabs.addTab(self._build_search_tab(), "Search by photo")

        central = QWidget()
        v = QVBoxLayout(central)
        v.addLayout(top)
        v.addWidget(self.tabs)
        self.setCentralWidget(central)

        # ---- status bar
        self.status = QLabel("Starting...")
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(320)
        self.progress.setVisible(False)
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setVisible(False)
        self.btn_cancel.clicked.connect(self.cancel_work)
        sb = self.statusBar()
        sb.addWidget(self.status, 1)
        sb.addPermanentWidget(self.progress)
        sb.addPermanentWidget(self.btn_cancel)

        QShortcut(QKeySequence.Paste, self, activated=self.paste_image)
        self._update_count()
        self._set_busy(True)

        self.loader = ModelLoader()
        self.loader.status.connect(self.status.setText)
        self.loader.ready.connect(self._model_ready)
        self.loader.failed.connect(self._model_failed)
        self.loader.start()

        if updater.enabled() and updater.check_due():
            QTimer.singleShot(15000, lambda: self.check_updates(manual=False))

    # ------------------------------------------------------------------ UI build
    def _build_dup_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        row = QHBoxLayout()
        row.addWidget(QLabel("Match level:"))
        self.cmb_strict = QComboBox()
        for label, t in STRICTNESS:
            self.cmb_strict.addItem(label, t)
        self.cmb_strict.setCurrentIndex(2)
        row.addWidget(self.cmb_strict)
        self.btn_dups = QPushButton("Find duplicates")
        self.btn_dups.setObjectName("primary")
        self.btn_dups.clicked.connect(self.start_dups)
        row.addWidget(self.btn_dups)
        self.btn_codes = QPushButton("Check item codes")
        self.btn_codes.setToolTip("Find item codes (e.g. KEY-0001234A) used on photos of different items")
        self.btn_codes.clicked.connect(self.start_codes)
        row.addWidget(self.btn_codes)
        self.lbl_dups = QLabel("")
        row.addSpacing(10)
        row.addWidget(self.lbl_dups)
        row.addStretch()
        v.addLayout(row)

        split = QSplitter()
        self.group_list = QListWidget()
        self.group_list.setIconSize(QSize(56, 56))
        self.group_list.setMinimumWidth(230)
        self.group_list.currentItemChanged.connect(self._show_group)
        split.addWidget(self.group_list)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        self.dup_grid = PhotoGrid(self.thumbs, priority=10)
        self.dup_grid.action.connect(self.do_action)
        rv.addLayout(self._action_bar(self.dup_grid, keep_best=True))
        rv.addWidget(self.dup_grid)
        hint = QLabel("Tick the photos you want to act on (or select with Ctrl/Shift-click). "
                      "Double-click opens a photo. Deleted files go to the Recycle Bin.")
        hint.setObjectName("hint")
        rv.addWidget(hint)
        split.addWidget(right)
        split.setStretchFactor(1, 1)
        split.setSizes([260, 1000])
        v.addWidget(split, 1)
        return w

    def _build_search_tab(self) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        left = QVBoxLayout()
        self.drop = DropZone()
        self.drop.dropped.connect(self._set_query)
        left.addWidget(self.drop)
        self.lbl_qcode = QLabel("")
        self.lbl_qcode.setObjectName("hint")
        self.lbl_qcode.setWordWrap(True)
        left.addWidget(self.lbl_qcode)
        r = QHBoxLayout()
        b1, b2 = QPushButton("Browse..."), QPushButton("Paste")
        b1.clicked.connect(self.browse_image)
        b2.clicked.connect(self.paste_image)
        r.addWidget(b1)
        r.addWidget(b2)
        left.addLayout(r)
        left.addSpacing(8)
        left.addWidget(QLabel("Optional - describe it:"))
        self.txt = QLineEdit()
        self.txt.setPlaceholderText("e.g. blue turtle")
        self.txt.returnPressed.connect(self.run_search)
        left.addWidget(self.txt)
        r2 = QHBoxLayout()
        r2.addWidget(QLabel("Show top"))
        self.spin_k = QSpinBox()
        self.spin_k.setRange(10, 1000)
        self.spin_k.setValue(100)
        self.spin_k.setSingleStep(50)
        r2.addWidget(self.spin_k)
        r2.addStretch()
        left.addLayout(r2)
        self.btn_search = QPushButton("Search")
        self.btn_search.setObjectName("primary")
        self.btn_search.clicked.connect(self.run_search)
        left.addWidget(self.btn_search)
        btn_clear = QPushButton("Clear")
        btn_clear.clicked.connect(self.clear_query)
        left.addWidget(btn_clear)
        left.addStretch()
        lw = QWidget()
        lw.setLayout(left)
        lw.setFixedWidth(270)
        h.addWidget(lw)

        right = QVBoxLayout()
        self.search_grid = PhotoGrid(self.thumbs, priority=10)
        self.search_grid.action.connect(self.do_action)
        right.addLayout(self._action_bar(self.search_grid))
        self.code_banner = QLabel("")
        self.code_banner.setObjectName("warn")
        self.code_banner.setWordWrap(True)
        self.code_banner.setVisible(False)
        right.addWidget(self.code_banner)
        right.addWidget(self.search_grid)
        h.addLayout(right, 1)
        return w

    def _action_bar(self, grid: PhotoGrid, keep_best=False) -> QHBoxLayout:
        row = QHBoxLayout()
        buttons = []
        if keep_best:
            buttons.append(("Tick all but best copy", "keepbest"))
        buttons += [("Tick all", "tickall"), ("Untick all", "untick"), (None, None),
                    ("Rename...", "rename"), ("Move to...", "move"), ("Delete", "delete")]
        for label, name in buttons:
            if label is None:
                row.addSpacing(16)
                continue
            b = QPushButton(label)
            if name == "delete":
                b.setObjectName("danger")
            b.clicked.connect(lambda _=False, n=name: self.do_action(n, grid))
            row.addWidget(b)
        row.addStretch()
        return row

    # ------------------------------------------------------------------ state
    def _set_busy(self, busy: bool):
        ready = self.embedder is not None
        for b in (self.btn_scan, self.btn_dups, self.btn_codes, self.btn_search, self.btn_folders):
            b.setEnabled(ready and not busy)

    def _progress(self, done, total, msg):
        self.status.setText(msg)
        self.progress.setVisible(True)
        self.progress.setMaximum(max(total, 0))
        self.progress.setValue(done)

    def _end_progress(self, msg=""):
        self.progress.setVisible(False)
        self.btn_cancel.setVisible(False)
        if msg:
            self.status.setText(msg)
        self._set_busy(False)
        self._update_count()

    def _update_count(self):
        self.lbl_count.setText(f"{self.index.count():,} photos indexed")

    def _error(self, tb: str):
        self._end_progress("Something went wrong.")
        QMessageBox.critical(self, APP_NAME, tb[-2000:])

    def cancel_work(self):
        for w in (self.scan_worker, self.dup_worker, getattr(self, "code_worker", None)):
            if w is not None and w.isRunning():
                w.cancel.set()
        self.status.setText("Cancelling...")

    # ------------------------------------------------------------------ model
    def _model_ready(self, emb):
        self.embedder = emb
        dev = "GPU" if emb.device == "cuda" else "CPU"
        self._end_progress(f"AI model ready ({dev}).")
        if not self.index.folders():
            QMessageBox.information(
                self, APP_NAME,
                "Welcome! First, choose the folder(s) where your item photos are stored.\n\n"
                "ItemFinder will read every photo once to build its index (this can take a while "
                "for 50,000+ photos, but you can cancel and resume any time).",
            )
            self.manage_folders()
        else:
            self.start_scan()  # quick incremental refresh on startup

    def _model_failed(self, tb):
        self.status.setText("Could not load AI model.")
        QMessageBox.critical(
            self, APP_NAME,
            "Could not load the AI model.\n\nThe first run needs internet once to download it "
            f"(~600 MB). After that ItemFinder works fully offline.\n\n{tb[-1500:]}",
        )

    # ------------------------------------------------------------------ folders / scan
    def manage_folders(self):
        d = FoldersDialog(self.index, self)
        d.exec()
        if d.changed:
            self.start_scan()

    def start_scan(self):
        if self.embedder is None or (self.scan_worker and self.scan_worker.isRunning()):
            return
        self._set_busy(True)
        self.btn_cancel.setVisible(True)
        self.scan_worker = ScanWorker(self.index, self.embedder)
        self.scan_worker.progress.connect(self._progress)
        self.scan_worker.done.connect(self._scan_done)
        self.scan_worker.failed.connect(self._error)
        self.scan_worker.start()

    def _scan_done(self, r: engine.ScanResult):
        parts = [f"{r.added:,} new/updated"]
        if r.removed:
            parts.append(f"{r.removed:,} removed")
        if r.failed:
            parts.append(f"{r.failed:,} unreadable")
        msg = ("Indexing paused - click Update index to resume. " if r.cancelled else "Index up to date. ")
        self._end_progress(msg + ", ".join(parts) + ".")

    # ------------------------------------------------------------------ duplicates
    def start_dups(self):
        if self.index.count() == 0:
            QMessageBox.information(self, APP_NAME, "No photos indexed yet. Add a folder first.")
            return
        self._set_busy(True)
        self.btn_cancel.setVisible(True)
        self.dup_worker = DupWorker(self.index, self.cmb_strict.currentData())
        self.dup_worker.progress.connect(self._progress)
        self.dup_worker.done.connect(self._dups_done)
        self.dup_worker.failed.connect(self._error)
        self.dup_worker.start()

    def _dups_done(self, groups):
        self.group_list.clear()
        self.dup_grid.clear_photos()
        self.groups = {}
        self.group_info = {}
        self.group_icon_items: dict[str, QListWidgetItem] = {}
        total = 0
        for gid, g in enumerate(groups):
            self.groups[gid] = [p for p, _ in g]
            total += len(g)
            it = QListWidgetItem(self.placeholder_icon(), "")
            it.setData(GROUP_ROLE, gid)
            self.group_list.addItem(it)
            self._label_group(it)
            if gid < 400:
                self.group_icon_items[g[0][0]] = it
                self.thumbs.request(g[0][0], 0)
        cancelled = self.dup_worker.cancel.is_set()
        self.lbl_dups.setText(f"{len(groups):,} groups  ·  {total:,} photos  ·  "
                              f"{total - len(groups):,} possible extras")
        self._end_progress("Duplicate search cancelled." if cancelled else
                           f"Found {len(groups):,} groups of duplicate items.")
        if self.group_list.count():
            self.group_list.setCurrentRow(0)

    def start_codes(self):
        if self.index.count() == 0:
            QMessageBox.information(self, APP_NAME, "No photos indexed yet. Add a folder first.")
            return
        self._set_busy(True)
        self.btn_cancel.setVisible(True)
        self.code_worker = CodeWorker(self.index)
        self.code_worker.progress.connect(self._progress)
        self.code_worker.done.connect(self._codes_done)
        self.code_worker.failed.connect(self._error)
        self.code_worker.start()

    def _codes_done(self, conflicts):
        """Show item codes that are used on different-looking photos."""
        self.group_list.clear()
        self.dup_grid.clear_photos()
        self.groups, self.group_info = {}, {}
        self.group_icon_items = {}
        for gid, (code, members) in enumerate(conflicts):
            self.groups[gid] = [p for p, _, _ in members]
            self.group_info[gid] = {"code": code, "members": {p: (s, f) for p, s, f in members}}
            it = QListWidgetItem(self.placeholder_icon(), "")
            it.setData(GROUP_ROLE, gid)
            it.setForeground(QColor(WARN_FG))
            self.group_list.addItem(it)
            self._label_group(it)
            if gid < 400:
                self.group_icon_items[members[0][0]] = it
                self.thumbs.request(members[0][0], 0)
        cancelled = self.code_worker.cancel.is_set()
        n_odd = sum(f for _, m in conflicts for _, _, f in m)
        self.lbl_dups.setText(f"\u26a0 {len(conflicts):,} item code{'s' if len(conflicts) != 1 else ''} used on "
                              f"different-looking photos  ·  {n_odd:,} photo{'s' if n_odd != 1 else ''} to check" if conflicts else "No item code problems found.")
        self._end_progress("Item code check cancelled." if cancelled else
                           f"Item code check done: {len(conflicts):,} code{'s' if len(conflicts) != 1 else ''} to review.")
        if self.group_list.count():
            self.group_list.setCurrentRow(0)

    def placeholder_icon(self):
        pm = QPixmap(56, 56)
        pm.fill(QColor("#e9ecef"))
        return QIcon(pm)

    def _label_group(self, it: QListWidgetItem):
        gid = it.data(GROUP_ROLE)
        g = self.groups[gid]
        info = self.group_info.get(gid)
        if info:
            odd = sum(1 for p in g if info["members"].get(p, (0, False))[1])
            it.setText(f"\u26a0 {info['code']}  ·  {len(g)} files\n{odd} look{'s' if odd == 1 else ''} different")
            return
        name = os.path.basename(g[0]) if g else ""
        it.setText(f"Group {it.data(GROUP_ROLE) + 1}  ·  {len(g)} photos\n{name[:30]}")

    def _group_icon_ready(self, path, img, info, pixels):
        it = getattr(self, "group_icon_items", {}).get(path)
        if it is not None and not img.isNull():
            it.setIcon(QIcon(QPixmap.fromImage(img)))

    def _show_group(self, cur, _prev=None):
        self.dup_grid.clear_photos()
        if cur is None:
            return
        gid = cur.data(GROUP_ROLE)
        g = self.groups.get(gid, [])
        info = self.group_info.get(gid)
        if info:  # item-code check group
            for p in g:
                s, odd = info["members"].get(p, (0.0, False))
                self.dup_grid.add_photo(p, f"{engine.to_percent(s)}% like the others",
                                        "LOOKS DIFFERENT" if odd else "")
            return
        vecs = [self.index.vector_for(p) for p in g]
        ref = vecs[0] if g else None
        for p, vv in zip(g, vecs):
            s = float(vv @ ref) if (vv is not None and ref is not None) else 0.0
            self.dup_grid.add_photo(p, f"{engine.to_percent(s)}% match")

    def _sync_groups_after(self, removed: list[str], renamed: dict[str, str]):
        """Keep the duplicate-group list in step with file operations."""
        rm = set(removed)
        for i in range(self.group_list.count() - 1, -1, -1):
            it = self.group_list.item(i)
            gid = it.data(GROUP_ROLE)
            g = [renamed.get(p, p) for p in self.groups[gid] if p not in rm]
            self.groups[gid] = g
            info = self.group_info.get(gid)
            if info:
                info["members"] = {renamed.get(p, p): v for p, v in info["members"].items() if p not in rm}
            if len(g) < 2 and it is not self.group_list.currentItem():
                self.group_list.takeItem(i)
            else:
                self._label_group(it)

    # ------------------------------------------------------------------ search
    def browse_image(self):
        exts = " ".join(f"*{e}" for e in sorted(engine.IMAGE_EXTS))
        p, _ = QFileDialog.getOpenFileName(self, "Choose a photo", "", f"Images ({exts})")
        if p:
            self.drop.load_path(p)

    def paste_image(self):
        cb = QApplication.clipboard()
        md = cb.mimeData()
        if md.hasImage():
            self.tabs.setCurrentIndex(1)
            self.drop.load_qimage(cb.image())
        elif md.hasUrls() and md.urls()[0].toLocalFile():
            self.tabs.setCurrentIndex(1)
            self.drop.load_path(md.urls()[0].toLocalFile())

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() or e.mimeData().hasImage():
            e.acceptProposedAction()

    def dropEvent(self, e):
        self.tabs.setCurrentIndex(1)
        self.drop.dropEvent(e)

    def _set_query(self, im, path):
        self.query_image, self.query_path = im, path
        code = engine.item_code(path) if path else None
        self.lbl_qcode.setText(f"Item code: <b>{code}</b>" if code else ("No item code in file name" if path else ""))
        self.run_search()

    def clear_query(self):
        self.query_image, self.query_path = None, ""
        self.drop.clear()
        self.drop.setText("Drag & drop a photo here\n\nor use Browse / Paste")
        self.txt.clear()
        self.search_grid.clear_photos()
        self.code_banner.setVisible(False)
        self.lbl_qcode.setText("")

    def run_search(self):
        text = self.txt.text().strip()
        if self.embedder is None or (self.query_image is None and not text):
            return
        if self.search_worker and self.search_worker.isRunning():
            return
        self.status.setText("Searching...")
        self.search_worker = SearchWorker(self.index, self.embedder, self.query_image,
                                          self.query_path, text, self.spin_k.value())
        self.search_worker.done.connect(self._search_done)
        self.search_worker.failed.connect(self._error)
        self.search_worker.start()

    def _search_done(self, results, text_only, same):
        self.search_grid.clear_photos()
        q = engine.norm_path(self.query_path) if self.query_path else ""
        code = engine.item_code(self.query_path) if self.query_path else None
        shown = set()
        # 1) files with the SAME ITEM CODE first, highlighted orange
        n_diff = 0
        for p, s in sorted(same, key=lambda t: t[1]):
            different = 0 <= s < engine.LOOKS_DIFFERENT_BELOW
            n_diff += different
            pct = f"{engine.to_percent(s)}% match" if s >= 0 else ""
            self.search_grid.add_photo(p, pct, "SAME CODE - LOOKS DIFFERENT" if different else "SAME CODE")
            shown.add(engine.norm_path(p))
        # 2) visually similar items
        for p, s in results:
            if engine.norm_path(p) in shown:
                continue
            tag = "  (this photo)" if q and engine.norm_path(p) == q else ""
            self.search_grid.add_photo(p, f"{engine.to_percent(s, text_only)}% match{tag}")
        if same:
            msg = (f"\u26a0  Item code {code} is also used by {len(same)} other file(s) - shown first, "
                   "highlighted in orange.")
            if n_diff:
                msg += (f"  {n_diff} of them look{'s' if n_diff == 1 else ''} like a DIFFERENT item "
                        "- please check.")
            self.code_banner.setText(msg)
        self.code_banner.setVisible(bool(same))
        self.status.setText(f"Showing {len(results)} closest items.")

    # ------------------------------------------------------------------ file actions
    def do_action(self, name: str, grid: PhotoGrid):
        if name == "tickall":
            for it in grid.items.values():
                it.setCheckState(Qt.Checked)
            return
        if name == "untick":
            for it in grid.items.values():
                it.setCheckState(Qt.Unchecked)
            return
        if name == "keepbest":
            self._tick_all_but_best(grid)
            return
        paths = grid.targets()
        if not paths:
            QMessageBox.information(self, APP_NAME, "Tick or select at least one photo first.")
            return
        if name == "open":
            for p in paths[:10]:
                open_file(p)
        elif name == "show":
            show_in_folder(paths[0])
        elif name == "copy":
            QApplication.clipboard().setText("\n".join(paths))
            self.status.setText("Path copied.")
        elif name == "similar":
            self.tabs.setCurrentIndex(1)
            self.drop.load_path(paths[0])
        elif name == "rename":
            self._rename(paths)
        elif name == "move":
            self._move(paths)
        elif name == "delete":
            self._delete(paths)

    def _tick_all_but_best(self, grid: PhotoGrid):
        """Keep the highest-resolution (then largest file) copy, tick the rest."""
        def quality(p):
            it = grid.items[p]
            px = it.data(PIXELS_ROLE) or 0
            try:
                sz = os.path.getsize(p)
            except OSError:
                sz = 0
            return (px, sz)

        paths = grid.all_paths()
        if len(paths) < 2:
            return
        best = max(paths, key=quality)
        for p, it in grid.items.items():
            it.setCheckState(Qt.Unchecked if p == best else Qt.Checked)
        self.status.setText(f"Keeping: {os.path.basename(best)}  -  {len(paths) - 1} extra copies ticked.")

    def _apply_everywhere(self, removed: list[str], renamed: dict[str, str]):
        for g in (self.dup_grid, self.search_grid):
            for p in removed:
                g.remove_path(p)
            for o, n in renamed.items():
                g.rename_path(o, n)
        self._sync_groups_after(removed, renamed)
        self._update_count()

    def _rename(self, paths: list[str]):
        renamed, errors = {}, []
        if len(paths) == 1:
            old = paths[0]
            new, ok = QInputDialog.getText(self, "Rename", "New file name:", text=os.path.basename(old))
            if not ok or not new.strip():
                return
            try:
                renamed[old] = self.index.rename_file(old, new)
            except Exception as e:
                errors.append(f"{os.path.basename(old)}: {e}")
        else:
            first = os.path.splitext(os.path.basename(paths[0]))[0]
            base, ok = QInputDialog.getText(
                self, "Rename photos",
                f"Rename {len(paths)} photos to a common name.\n"
                f"They will become  NAME_01, NAME_02, ...  (extensions are kept)\n\nName:",
                text=first)
            if not ok or not base.strip():
                return
            base = base.strip()
            width = max(2, len(str(len(paths))))
            for i, old in enumerate(paths, 1):
                ext = os.path.splitext(old)[1]
                n, suffix = i, ""
                while True:
                    cand = f"{base}_{str(n).zfill(width)}{suffix}{ext}"
                    if not os.path.exists(os.path.join(os.path.dirname(old), cand)) or \
                            engine.norm_path(os.path.join(os.path.dirname(old), cand)) == engine.norm_path(old):
                        break
                    suffix = f"-{len(suffix) + 1}" if suffix else "-b"
                try:
                    renamed[old] = self.index.rename_file(old, cand)
                except Exception as e:
                    errors.append(f"{os.path.basename(old)}: {e}")
        self._apply_everywhere([], renamed)
        self._report(f"Renamed {len(renamed)} photo(s).", errors)

    def _move(self, paths: list[str]):
        dest = QFileDialog.getExistingDirectory(self, f"Move {len(paths)} photo(s) to...")
        if not dest:
            return
        renamed, errors = {}, []
        for p in paths:
            try:
                renamed[p] = self.index.move_file(p, dest)
            except Exception as e:
                errors.append(f"{os.path.basename(p)}: {e}")
        self._apply_everywhere([], renamed)
        self._report(f"Moved {len(renamed)} photo(s) to {dest}.", errors)

    def _delete(self, paths: list[str]):
        names = "\n".join("  • " + os.path.basename(p) for p in paths[:12])
        more = f"\n  ...and {len(paths) - 12} more" if len(paths) > 12 else ""
        if QMessageBox.question(
            self, "Delete photos",
            f"Move {len(paths)} photo(s) to the Recycle Bin?\n\n{names}{more}\n\n"
            "You can restore them from the Recycle Bin if needed.",
        ) != QMessageBox.Yes:
            return
        removed, errors = [], []
        for p in paths:
            try:
                self.index.delete_file(p)
                removed.append(p)
            except Exception as e:
                errors.append(f"{os.path.basename(p)}: {e}")
        self._apply_everywhere(removed, {})
        self._report(f"Moved {len(removed)} photo(s) to the Recycle Bin.", errors)

    def _report(self, msg: str, errors: list[str]):
        self.status.setText(msg)
        if errors:
            QMessageBox.warning(self, APP_NAME, msg + "\n\nProblems:\n" + "\n".join(errors[:20]))

    # ------------------------------------------------------------------ updates
    def check_updates(self, manual: bool):
        if not updater.enabled():
            if manual:
                QMessageBox.information(self, APP_NAME, "Automatic updates are available in the installed "
                                        "version of ItemFinder (this copy is running from source).")
            return
        if (self.upd_check and self.upd_check.isRunning()) or (self.upd_dl and self.upd_dl.isRunning()):
            return
        self.btn_update.setEnabled(False)
        if manual:
            self.status.setText("Checking for updates...")
        self.upd_check = UpdateCheckWorker(manual)
        self.upd_check.done.connect(self._update_checked)
        self.upd_check.failed.connect(self._update_check_failed)
        self.upd_check.start()

    def _update_check_failed(self, err, manual):
        self.btn_update.setEnabled(True)
        if manual:
            self.status.setText("")
            QMessageBox.warning(self, APP_NAME, f"Could not check for updates.\n\n{err}")

    def _update_checked(self, info, manual):
        self.btn_update.setEnabled(True)
        if info is None:
            if manual:
                self.status.setText("")
                QMessageBox.information(self, APP_NAME, f"You have the latest version (v{version.__version__}).")
            return
        notes = info.notes[:1500] or "Bug fixes and improvements."
        size = f" ({human_size(info.size)})" if info.size else ""
        box = QMessageBox(self)
        box.setWindowTitle("Update available")
        box.setIcon(QMessageBox.Information)
        box.setText(f"<b>ItemFinder {info.version}</b> is available - you have {version.__version__}.")
        box.setInformativeText(f"Download{size} and install now? ItemFinder will restart. "
                               "Your index and settings are kept.")
        box.setDetailedText(notes)
        b_now = box.addButton("Update now", QMessageBox.AcceptRole)
        box.addButton("Later", QMessageBox.RejectRole)
        b_skip = box.addButton("Skip this version", QMessageBox.DestructiveRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is b_skip:
            updater.skip_version(info.version)
        elif clicked is b_now:
            if not updater.is_installed_build():
                QDesktopServices.openUrl(QUrl(info.page or info.url))
                return
            self._download_update(info)

    def _download_update(self, info):
        self.btn_update.setEnabled(False)
        self.upd_dl = UpdateDownloadWorker(info)
        self.upd_dl.progress.connect(lambda d, t: self._progress(
            d // 1024, max(t, 0) // 1024, f"Downloading update {info.version}... {human_size(d)}"
            + (f" of {human_size(t)}" if t else "")))
        self.upd_dl.done.connect(self._install_update)
        self.upd_dl.failed.connect(self._update_dl_failed)
        self.progress.setVisible(True)
        self.upd_dl.start()

    def _update_dl_failed(self, err):
        self.btn_update.setEnabled(True)
        self.progress.setVisible(False)
        self.status.setText("Update cancelled." if not err else "Update download failed.")
        if err:
            QMessageBox.warning(self, APP_NAME, f"The update could not be downloaded.\n\n{err}")

    def _install_update(self, path):
        self.progress.setVisible(False)
        busy = any(w is not None and w.isRunning() for w in (self.scan_worker, self.dup_worker))
        msg = "The update is ready. ItemFinder will close, update, and reopen in about a minute."
        if busy:
            msg += "\n\nIndexing will pause and continue automatically after the restart."
        if QMessageBox.question(self, "Install update", msg + "\n\nInstall now?") != QMessageBox.Yes:
            self.status.setText("Update downloaded - it will be offered again next time.")
            self.btn_update.setEnabled(True)
            return
        try:
            updater.launch_installer(path)
        except Exception as e:
            QMessageBox.warning(self, APP_NAME, f"Could not start the installer:\n{e}")
            self.btn_update.setEnabled(True)
            return
        self.close()

    def closeEvent(self, e):
        self.cancel_work()
        for w in (self.scan_worker, self.dup_worker):
            if w is not None:
                w.wait(5000)
        super().closeEvent(e)


STYLE = """
QWidget { font-size: 10pt; color: #1f2328; }
QWidget:disabled { color: #a0a4a8; }
QMainWindow, QDialog, QTabWidget::pane, QTabBar::tab { background: #f6f7f9; }
QTabBar::tab { padding: 6px 14px; border: 1px solid #dee2e6; border-bottom: none;
               border-top-left-radius: 6px; border-top-right-radius: 6px; margin-right: 2px; }
QTabBar::tab:selected { background: #ffffff; font-weight: 600; }
QLineEdit, QSpinBox, QComboBox { background: #ffffff; border: 1px solid #c8ccd2; border-radius: 5px; padding: 3px 6px; }
QComboBox QAbstractItemView { background: #ffffff; selection-background-color: #dbe9ff; }
QToolTip { background: #ffffff; color: #1f2328; border: 1px solid #c8ccd2; }
QPushButton { padding: 6px 12px; border: 1px solid #c8ccd2; border-radius: 6px; background: #fff; }
QPushButton:hover { background: #f1f3f5; }
QPushButton:disabled { color: #a0a4a8; }
QPushButton#primary { background: #1f6feb; color: white; border-color: #1f6feb; font-weight: 600; }
QPushButton#primary:hover { background: #1a5fd0; }
QPushButton#primary:disabled { background: #9dbcf0; border-color: #9dbcf0; }
QPushButton#danger { color: #c62828; border-color: #e0a0a0; }
QPushButton#danger:hover { background: #fdecec; }
QLabel#dropzone { border: 2px dashed #9aa4b2; border-radius: 12px; color: #5c6670; background: #f8f9fb; }
QLabel#privacy { color: #2e7d32; font-weight: 600; }
QLabel#hint { color: #6c757d; font-size: 9pt; }
QLabel#warn { background: #ffe3c2; color: #8a3d00; border: 1px solid #f08c00; border-radius: 6px;
              padding: 8px 10px; font-weight: 600; }
QListWidget { border: 1px solid #dee2e6; border-radius: 6px; background: white; }
QListWidget::item:selected { background: #dbe9ff; color: black; }
"""


def _single_instance_mutex():
    """Named mutex so the installer can wait until ItemFinder has closed."""
    if sys.platform.startswith("win"):
        try:
            import ctypes

            return ctypes.windll.kernel32.CreateMutexW(None, False, "ItemFinder_AppMutex")
        except Exception:
            return None
    return None


def _selftest(out_path: str) -> int:
    """Used by the build pipeline to prove the packaged app can load everything."""
    lines = []
    try:
        import numpy, PIL, torch, open_clip  # noqa: F401

        tok = open_clip.get_tokenizer(engine.MODEL_NAME)
        tok(["blue turtle"])
        model = open_clip.create_model(engine.MODEL_NAME)
        _, _, pre = open_clip.create_model_and_transforms(engine.MODEL_NAME)
        from PIL import Image

        x = pre(Image.new("RGB", (64, 64), "blue")).unsqueeze(0)
        with torch.no_grad():
            model.encode_image(x)
        import send2trash  # noqa: F401
        import safetensors.torch  # noqa: F401  (model file format)

        lines.append(f"OK version={version.__version__} torch={torch.__version__} open_clip={open_clip.__version__}")
        code = 0
    except Exception:
        lines.append("FAIL\n" + traceback.format_exc())
        code = 1
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return code


def apply_light_theme(app):
    """Always use a light theme. Windows dark mode otherwise switches Qt to
    white text, which is unreadable on the app's light buttons and lists."""
    from PySide6.QtGui import QPalette

    try:
        app.styleHints().setColorScheme(Qt.ColorScheme.Light)  # Qt 6.8+
    except Exception:
        pass
    text, dim = QColor("#1f2328"), QColor("#a0a4a8")
    pal = QPalette()
    for role, col in [
        (QPalette.Window, "#f6f7f9"), (QPalette.WindowText, "#1f2328"),
        (QPalette.Base, "#ffffff"), (QPalette.AlternateBase, "#f1f3f5"),
        (QPalette.Text, "#1f2328"), (QPalette.Button, "#ffffff"),
        (QPalette.ButtonText, "#1f2328"), (QPalette.BrightText, "#c62828"),
        (QPalette.Highlight, "#1f6feb"), (QPalette.HighlightedText, "#ffffff"),
        (QPalette.ToolTipBase, "#ffffff"), (QPalette.ToolTipText, "#1f2328"),
        (QPalette.PlaceholderText, "#8a9099"), (QPalette.Link, "#1f6feb"),
    ]:
        pal.setColor(role, QColor(col))
    for role in (QPalette.Text, QPalette.ButtonText, QPalette.WindowText):
        pal.setColor(QPalette.Disabled, role, dim)
    app.setPalette(pal)


def main():
    # Windowed builds have no console: send stray output to a log file instead
    # of crashing (e.g. download progress bars writing to a missing stderr).
    if sys.stdout is None or sys.stderr is None:
        try:
            log = open(os.path.join(engine.app_data_dir(), "itemfinder.log"), "a", encoding="utf-8", buffering=1)
        except Exception:
            log = open(os.devnull, "w")
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log
    if "--selftest" in sys.argv:
        i = sys.argv.index("--selftest")
        out = sys.argv[i + 1] if len(sys.argv) > i + 1 else "selftest.txt"
        sys.exit(_selftest(out))
    _mutex = _single_instance_mutex()  # noqa: F841 (kept alive for the app's lifetime)
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    apply_light_theme(app)
    app.setStyleSheet(STYLE)
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    ico = os.path.join(base, "assets", "itemfinder.ico")
    if os.path.exists(ico):
        app.setWindowIcon(QIcon(ico))
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    if getattr(sys, "frozen", False):
        import multiprocessing

        multiprocessing.freeze_support()
    main()
