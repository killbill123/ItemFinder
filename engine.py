"""
ItemFinder engine - 100% offline visual search.

* A local CLIP AI model turns every photo into a 512-number "fingerprint" that
  describes WHAT is in the picture (a blue turtle, a red mug...), not its file
  name or item code.
* Fingerprints are stored in a local SQLite file under
  %LOCALAPPDATA%\\ItemFinder - nothing is ever uploaded.
* Indexing is incremental and resumable: only new / changed photos are
  processed, and progress is saved every batch.
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Iterable

import numpy as np

os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

from PIL import Image, ImageOps

Image.MAX_IMAGE_PIXELS = None  # allow very large product shots

try:  # optional iPhone HEIC support
    import pillow_heif  # type: ignore

    pillow_heif.register_heif_opener()
    _HEIC = {".heic", ".heif"}
except Exception:  # pragma: no cover
    _HEIC = set()

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".jfif"} | _HEIC

MODEL_NAME = "ViT-B-32"
PRETRAINED = "laion2b_s34b_b79k"
MODEL_BASENAME = "clip-vit-b-32-laion2b"  # + .safetensors or .bin (format must match extension)
EMB_DIM = 512


# --------------------------------------------------------------------------- paths
def app_data_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".local", "share")
    d = os.path.join(base, "ItemFinder")
    os.makedirs(d, exist_ok=True)
    return d


def model_dir() -> str:
    d = os.path.join(app_data_dir(), "models")
    os.makedirs(d, exist_ok=True)
    return d


def norm_path(p: str) -> str:
    return os.path.normcase(os.path.abspath(p))


# --------------------------------------------------------------------------- image loading
def load_image(path: str, max_side: int = 448) -> Image.Image:
    """Open an image fast (JPEG draft-mode decode), fix EXIF rotation, RGB."""
    im = Image.open(path)
    try:
        im.draft("RGB", (max_side, max_side))  # huge speed-up for big JPEGs
    except Exception:
        pass
    if getattr(im, "is_animated", False):
        im.seek(0)
    im = ImageOps.exif_transpose(im)
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))  # transparent -> white
        bg.paste(im, mask=im.split()[-1])
        im = bg
    elif im.mode != "RGB":
        im = im.convert("RGB")
    if max(im.size) > max_side * 2:
        im.thumbnail((max_side * 2, max_side * 2))
    return im


# --------------------------------------------------------------------------- AI model
def _is_safetensors(path: str) -> bool:
    """safetensors files start with an 8-byte header length followed by '{'."""
    try:
        with open(path, "rb") as f:
            head = f.read(9)
        n = int.from_bytes(head[:8], "little")
        return len(head) == 9 and 0 < n < 100_000_000 and head[8:9] == b"{"
    except OSError:
        return False


def find_local_model() -> str | None:
    """Return the downloaded model file, fixing a wrongly-named one from v1.0.0
    (a .safetensors file saved as .bin, which PyTorch can't read)."""
    d = model_dir()
    st = os.path.join(d, MODEL_BASENAME + ".safetensors")
    if os.path.exists(st):
        return st
    b = os.path.join(d, MODEL_BASENAME + ".bin")
    if os.path.exists(b):
        if _is_safetensors(b):
            os.replace(b, st)
            return st
        return b
    return None


class Embedder:
    """Wraps the local CLIP model. Downloads it ONCE, then runs fully offline."""

    def __init__(self, status: Callable[[str], None] | None = None):
        import torch
        import open_clip

        self.torch = torch
        status = status or (lambda s: None)
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        if self.device == "cpu":
            torch.set_num_threads(max(1, (os.cpu_count() or 4)))

        local = find_local_model()
        if local is None:
            status("Downloading AI model (one time only, ~600 MB)...")
            cfg = open_clip.get_pretrained_cfg(MODEL_NAME, PRETRAINED)
            tmp_cache = os.path.join(model_dir(), "_download")
            src = open_clip.pretrained.download_pretrained(cfg, cache_dir=tmp_cache)
            ext = ".safetensors" if (str(src).endswith(".safetensors") or _is_safetensors(src)) else ".bin"
            local = os.path.join(model_dir(), MODEL_BASENAME + ext)
            shutil.copyfile(src, local + ".part")
            os.replace(local + ".part", local)
            shutil.rmtree(tmp_cache, ignore_errors=True)

        status("Loading AI model...")
        model, _, preprocess = open_clip.create_model_and_transforms(MODEL_NAME, pretrained=local)
        model.eval().to(self.device)
        self.model = model
        self.preprocess = preprocess
        self.tokenizer = open_clip.get_tokenizer(MODEL_NAME)
        self.lock = threading.Lock()

    def _norm(self, x) -> np.ndarray:
        x = x / x.norm(dim=-1, keepdim=True)
        return x.float().cpu().numpy()

    def embed_tensors(self, tensors) -> np.ndarray:
        torch = self.torch
        with self.lock, torch.no_grad():
            batch = torch.stack(tensors).to(self.device)
            return self._norm(self.model.encode_image(batch))

    def embed_image(self, img: Image.Image) -> np.ndarray:
        return self.embed_tensors([self.preprocess(img)])[0]

    def embed_text(self, text: str) -> np.ndarray:
        torch = self.torch
        prompts = [text, f"a photo of {text}", f"a product photo of {text}"]
        with self.lock, torch.no_grad():
            t = self.tokenizer(prompts).to(self.device)
            e = self.model.encode_text(t)
            e = e / e.norm(dim=-1, keepdim=True)
            e = e.mean(dim=0, keepdim=True)
            return self._norm(e)[0]


# --------------------------------------------------------------------------- item codes
# optional letter prefix (KEY-, SKU_, AB) + 3+ digits + optional suffix (A, -B, -XL)
_CODE_RE = re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z]{1,8}[-_]?)?\d{3,}[A-Za-z0-9]*(?:-[A-Za-z0-9]{1,3})*(?![A-Za-z0-9])")
LOOKS_DIFFERENT_BELOW = 0.83  # same code but pictures less similar than this = suspicious


def item_code(path: str) -> str | None:
    """Item code from a file name, e.g. 'KEY-0001234A Turtle Green.jpg' -> 'KEY-0001234A'.
    Uses the first code-like word: optional letters, then 3+ digits."""
    stem = os.path.splitext(os.path.basename(path))[0]
    m = _CODE_RE.search(stem)
    if m:
        return m.group(0).upper()
    return None


def code_key(code: str | None) -> str | None:
    """Comparison key: 'KEY-0001234A', 'key0001234a' and 'KEY_0001234A' all match."""
    if not code:
        return None
    k = re.sub(r"[^A-Z0-9]", "", code.upper())
    return k or None


# --------------------------------------------------------------------------- folders the app manages
DUPLICATES_DIR = "_Duplicates"          # one-click cleanup moves extras here (next to originals)
HOLD_DIR = ".ItemFinder-deleted"        # deleted files wait here (undo) until the app closes
NETWORK_DELETED_DIR = "_Deleted by ItemFinder"  # network drives have no Recycle Bin


def is_excluded_dir(name: str) -> bool:
    return name.startswith((".", "$")) or name.lower() in (DUPLICATES_DIR.lower(), NETWORK_DELETED_DIR.lower())


def is_network_path(path: str) -> bool:
    """True for \\\\SERVER\\share paths and mapped network drives (e.g. Z:)."""
    fake = os.environ.get("ITEMFINDER_TEST_NETWORK_PREFIX")  # used by tests on Linux
    if fake and os.path.abspath(path).startswith(fake):
        return True
    if not sys.platform.startswith("win"):
        return False
    p = os.path.abspath(path)
    if p.startswith("\\\\"):
        return True
    try:
        import ctypes

        return ctypes.windll.kernel32.GetDriveTypeW(os.path.splitdrive(p)[0] + "\\") == 4  # DRIVE_REMOTE
    except Exception:
        return False


def is_excluded_path(path: str) -> bool:
    parts = os.path.normpath(os.path.dirname(path)).split(os.sep)
    return any(p and is_excluded_dir(p) for p in parts)


def _unique_path(folder: str, name: str) -> str:
    base, ext = os.path.splitext(name)
    p, n = os.path.join(folder, name), 2
    while os.path.exists(p):
        p = os.path.join(folder, f"{base} ({n}){ext}")
        n += 1
    return p


@dataclass
class Op:
    """One undoable file operation."""
    kind: str               # "rename" | "move" | "delete"
    src: str                # original path
    dst: str                # where the file is now
    emb: bytes | None = None  # index entry removed by the op (restored on undo)


# --------------------------------------------------------------------------- index
@dataclass
class ScanResult:
    added: int = 0
    removed: int = 0
    failed: int = 0
    total: int = 0
    cancelled: bool = False
    unreachable: list = None  # folders that couldn't be reached (e.g. network drive offline)


class PhotoIndex:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or os.path.join(app_data_dir(), "index.db")
        self._paths: list[str] = []
        self._matrix = np.zeros((0, EMB_DIM), dtype=np.float32)
        self._pos: dict[str, int] = {}
        self._mx_lock = threading.Lock()
        with self._conn() as c:
            c.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS folders(path TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS images(
                    path TEXT PRIMARY KEY, mtime REAL, size INTEGER, emb BLOB);
                CREATE TABLE IF NOT EXISTS failed(
                    path TEXT PRIMARY KEY, mtime REAL, size INTEGER, error TEXT);
                CREATE TABLE IF NOT EXISTS ignored(a TEXT, b TEXT, PRIMARY KEY(a, b));
                CREATE TABLE IF NOT EXISTS held(
                    held_path TEXT PRIMARY KEY, original TEXT, emb BLOB);
                """
            )

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path, timeout=30)
        c.execute("PRAGMA synchronous=NORMAL")
        return c

    # ---- folders
    def folders(self) -> list[str]:
        with self._conn() as c:
            return [r[0] for r in c.execute("SELECT path FROM folders ORDER BY path")]

    def add_folder(self, path: str):
        with self._conn() as c:
            c.execute("INSERT OR IGNORE INTO folders VALUES (?)", (os.path.abspath(path),))

    def remove_folder(self, path: str):
        with self._conn() as c:
            c.execute("DELETE FROM folders WHERE path=?", (path,))

    def count(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM images").fetchone()[0]

    # ---- scanning
    @staticmethod
    def _walk(folders: Iterable[str], cancel: threading.Event) -> dict[str, tuple[float, int]]:
        found: dict[str, tuple[float, int]] = {}
        for root in folders:
            for dirpath, dirnames, filenames in os.walk(root):
                if cancel.is_set():
                    return found
                dirnames[:] = [d for d in dirnames if not is_excluded_dir(d)]
                for f in filenames:
                    if os.path.splitext(f)[1].lower() in IMAGE_EXTS:
                        p = os.path.join(dirpath, f)
                        try:
                            st = os.stat(p)
                            found[p] = (st.st_mtime, st.st_size)
                        except OSError:
                            pass
        return found

    def scan(
        self,
        embedder: Embedder,
        progress: Callable[[int, int, str], None] | None = None,
        cancel: threading.Event | None = None,
        batch_size: int = 32,
    ) -> ScanResult:
        progress = progress or (lambda d, t, m: None)
        cancel = cancel or threading.Event()
        res = ScanResult()

        progress(0, 0, "Looking for photos...")
        folders = self.folders()
        reachable = [f for f in folders if os.path.isdir(f)]
        res.unreachable = [f for f in folders if f not in reachable]
        on_disk = self._walk(reachable, cancel)
        if cancel.is_set():
            res.cancelled = True
            return res
        # never forget photos just because their (network) folder is offline right now
        offline = [norm_path(f).rstrip("\\/") + os.sep for f in res.unreachable]

        def is_offline(p: str) -> bool:
            n = norm_path(p)
            return any(n.startswith(o) for o in offline)

        with self._conn() as c:
            known = {r[0]: (r[1], r[2]) for r in c.execute("SELECT path, mtime, size FROM images")}
            bad = {r[0]: (r[1], r[2]) for r in c.execute("SELECT path, mtime, size FROM failed")}

            gone = [p for p in known if p not in on_disk and not is_offline(p)]
            c.executemany("DELETE FROM images WHERE path=?", [(p,) for p in gone])
            c.executemany("DELETE FROM failed WHERE path=?", [(p,) for p in bad if p not in on_disk])
            res.removed = len(gone)

        todo = [
            p for p, sig in on_disk.items()
            if known.get(p) != sig and bad.get(p) != sig
        ]
        todo.sort()
        res.total = len(todo)

        def prep(p):
            try:
                return p, embedder.preprocess(load_image(p)), None
            except Exception as e:  # corrupt / unsupported file
                return p, None, str(e)[:200]

        done = 0
        t0 = time.time()
        workers = min(8, (os.cpu_count() or 4))
        with ThreadPoolExecutor(workers) as pool:
            for i in range(0, len(todo), batch_size):
                if cancel.is_set():
                    res.cancelled = True
                    break
                chunk = todo[i:i + batch_size]
                items = list(pool.map(prep, chunk))
                ok = [(p, t) for p, t, e in items if t is not None]
                errs = [(p, e) for p, t, e in items if t is None]
                rows = []
                if ok:
                    embs = embedder.embed_tensors([t for _, t in ok]).astype(np.float16)
                    rows = [(p, *on_disk[p], embs[k].tobytes()) for k, (p, _) in enumerate(ok)]
                with self._conn() as c:
                    c.executemany("INSERT OR REPLACE INTO images VALUES (?,?,?,?)", rows)
                    c.executemany(
                        "INSERT OR REPLACE INTO failed VALUES (?,?,?,?)",
                        [(p, *on_disk[p], e) for p, e in errs],
                    )
                res.added += len(rows)
                res.failed += len(errs)
                done += len(chunk)
                rate = done / max(1e-6, time.time() - t0)
                eta = (len(todo) - done) / max(rate, 1e-6)
                progress(done, len(todo), f"Indexing {done:,}/{len(todo):,}  "
                                          f"({rate:.0f} photos/s, ~{_fmt_eta(eta)} left)")
        self.invalidate()
        return res

    # ---- search
    def invalidate(self):
        with self._mx_lock:
            self._paths = []
            self._matrix = np.zeros((0, EMB_DIM), dtype=np.float32)
            self._pos = {}
            self._loaded = False

    def _ensure_loaded(self):
        with self._mx_lock:
            if getattr(self, "_loaded", False):
                return
            with self._conn() as c:
                rows = c.execute("SELECT path, emb FROM images").fetchall()
            self._paths = [r[0] for r in rows]
            if rows:
                buf = b"".join(r[1] for r in rows)
                self._matrix = np.frombuffer(buf, dtype=np.float16).reshape(len(rows), EMB_DIM).astype(np.float32)
            else:
                self._matrix = np.zeros((0, EMB_DIM), dtype=np.float32)
            self._pos = {p: i for i, p in enumerate(self._paths)}
            self._codes: dict[str, list[int]] = {}
            for i, p in enumerate(self._paths):
                k = code_key(item_code(p))
                if k:
                    self._codes.setdefault(k, []).append(i)
            self._loaded = True

    def same_code(self, path: str) -> list[str]:
        """Other indexed files with the same item code as `path`."""
        k = code_key(item_code(path))
        if not k:
            return []
        self._ensure_loaded()
        me = norm_path(path)
        return [self._paths[i] for i in self._codes.get(k, []) if norm_path(self._paths[i]) != me]

    def find_code_conflicts(
        self,
        threshold: float | None = None,
        progress: Callable[[int, int, str], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> list[tuple[str, list[tuple[str, float, bool]]]]:
        """Item codes used by photos that look like DIFFERENT items.
        Returns [(code, [(path, similarity_to_the_others, looks_different)])]."""
        threshold = LOOKS_DIFFERENT_BELOW if threshold is None else threshold
        progress = progress or (lambda d, t, m: None)
        cancel = cancel or threading.Event()
        self._ensure_loaded()
        groups = [idxs for idxs in self._codes.values() if len(idxs) >= 2]
        ignored = self.ignored_set()
        npaths = {i: norm_path(self._paths[i]) for g in groups for i in g} if ignored else {}
        out = []
        for n, idxs in enumerate(groups):
            if cancel.is_set():
                return []
            if n % 500 == 0:
                progress(n, len(groups), f"Checking item codes {n:,}/{len(groups):,}...")
            sub = self._matrix[idxs]
            sims = sub @ sub.T
            if ignored:
                for a in range(len(idxs)):
                    for b in range(a + 1, len(idxs)):
                        if frozenset((npaths[idxs[a]], npaths[idxs[b]])) in ignored:
                            sims[a, b] = sims[b, a] = 1.0  # user said: this is fine
            if sims.min() >= threshold:
                continue  # same code, same-looking photos: fine
            k = len(idxs)
            # cluster the photos of this code: photos that look alike join one cluster
            label = list(range(k))

            def root(a):
                while label[a] != a:
                    label[a] = label[label[a]]
                    a = label[a]
                return a

            for a in range(k):
                for b in range(a + 1, k):
                    if sims[a, b] >= threshold:
                        label[root(b)] = root(a)
            roots = [root(a) for a in range(k)]
            sizes = {r: roots.count(r) for r in set(roots)}
            biggest = max(sizes.values())
            main = [r for r, n in sizes.items() if n == biggest]
            main_root = main[0] if len(main) == 1 else None  # tie = no clear "real" item
            ref = [a for a in range(k) if roots[a] == main_root] if main_root is not None else list(range(k))
            flags, avg = [], []
            for a in range(k):
                others = [b for b in ref if b != a]
                avg.append(float(sims[a, others].mean()) if others else 1.0)
                flags.append(main_root is None or roots[a] != main_root)
            members = sorted(
                [(self._paths[i], float(a), f) for i, a, f in zip(idxs, avg, flags)],
                key=lambda t: (t[2], -t[1]),
            )
            out.append((item_code(self._paths[idxs[0]]) or "", members))
        progress(len(groups), len(groups), "")
        out.sort(key=lambda g: (-sum(f for _, _, f in g[1]), g[0]))
        return out

    def vector_for(self, path: str) -> np.ndarray | None:
        self._ensure_loaded()
        i = self._pos.get(path)
        return None if i is None else self._matrix[i]

    def search(self, query: np.ndarray, top_k: int = 60, exclude: str | None = None) -> list[tuple[str, float]]:
        self._ensure_loaded()
        if len(self._paths) == 0:
            return []
        q = query.astype(np.float32)
        q = q / (np.linalg.norm(q) + 1e-8)
        scores = self._matrix @ q
        k = min(top_k + 1, len(scores))
        idx = np.argpartition(-scores, k - 1)[:k]
        idx = idx[np.argsort(-scores[idx])]
        out = []
        for i in idx:
            p = self._paths[i]
            if exclude and norm_path(p) == norm_path(exclude):
                continue
            out.append((p, float(scores[i])))
        return out[:top_k]


    # ---- "not a duplicate" memory
    def ignore_pairs(self, pairs: Iterable[tuple[str, str]]):
        rows = [tuple(sorted((norm_path(a), norm_path(b)))) for a, b in pairs if norm_path(a) != norm_path(b)]
        with self._conn() as c:
            c.executemany("INSERT OR IGNORE INTO ignored VALUES (?, ?)", rows)

    def ignored_set(self) -> set[frozenset]:
        with self._conn() as c:
            return {frozenset(r) for r in c.execute("SELECT a, b FROM ignored")}

    def clear_ignored(self) -> int:
        with self._conn() as c:
            n = c.execute("SELECT COUNT(*) FROM ignored").fetchone()[0]
            c.execute("DELETE FROM ignored")
        return n

    # ---- duplicates
    def find_duplicates(
        self,
        threshold: float = 0.92,
        progress: Callable[[int, int, str], None] | None = None,
        cancel: threading.Event | None = None,
        chunk: int = 1024,
        max_group: int = 200,
    ) -> list[list[tuple[str, float]]]:
        """Group photos of the same item. Returns groups of (path, similarity
        to the group's first photo), biggest groups first."""
        progress = progress or (lambda d, t, m: None)
        cancel = cancel or threading.Event()
        self._ensure_loaded()
        M, paths = self._matrix, self._paths
        n = len(paths)
        parent = list(range(n))

        def find(a):
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        for s in range(0, n, chunk):
            if cancel.is_set():
                return []
            block = M[s:s + chunk] @ M[s:].T  # upper triangle only
            ii, jj = np.nonzero(block >= threshold)
            for a, b in zip(ii + s, jj + s):
                if b > a:
                    ra, rb = find(a), find(b)
                    if ra != rb:
                        parent[rb] = ra
            done = min(n, s + chunk)
            progress(done, n, f"Comparing photos {done:,}/{n:,}...")

        groups: dict[int, list[int]] = {}
        for i in range(n):
            groups.setdefault(find(i), []).append(i)

        ignored = self.ignored_set()
        npaths = [norm_path(p) for p in paths] if ignored else []

        def split_ignored(members: list[int]) -> list[list[int]]:
            """Split a group so photos marked 'not a duplicate' never share a group."""
            if not ignored or len(members) < 2:
                return [members]
            sub = M[members]
            order = np.argsort(-(sub @ sub.T).sum(1))  # most central first
            clusters: list[list[int]] = []
            for k in order:
                i = members[k]
                for cl in clusters:
                    if all(frozenset((npaths[i], npaths[j])) not in ignored for j in cl) and \
                            max(float(M[i] @ M[j]) for j in cl) >= threshold:
                        cl.append(i)
                        break
                else:
                    clusters.append([i])
            return clusters

        out = []
        for raw in groups.values():
            for members in split_ignored(raw):
                if len(members) < 2:
                    continue
                members = members[:max_group]
                # order by similarity to the most "central" photo
                sub = M[members]
                centre = members[int(np.argmax((sub @ sub.T).sum(1)))]
                sims = M[members] @ M[centre]
                order = np.argsort(-sims)
                out.append([(paths[members[k]], float(sims[k])) for k in order])
        out.sort(key=lambda g: (-len(g), -g[1][1]))
        return out

    # ---- file actions (keep the index in sync; each returns an undoable Op)
    def rename_file(self, old: str, new_name: str) -> Op:
        new_name = new_name.strip()
        if not new_name or any(ch in new_name for ch in '\\/:*?"<>|'):
            raise ValueError("Invalid file name")
        if not os.path.splitext(new_name)[1]:
            new_name += os.path.splitext(old)[1]
        new = os.path.join(os.path.dirname(old), new_name)
        if norm_path(new) == norm_path(old):
            return Op("rename", old, old)
        if os.path.exists(new) and norm_path(new) != norm_path(old):
            raise FileExistsError(f"'{new_name}' already exists in that folder")
        os.rename(old, new)
        self._repath(old, new)
        return Op("rename", old, new)

    def move_file(self, old: str, dest_dir: str) -> Op:
        os.makedirs(dest_dir, exist_ok=True)
        new = _unique_path(dest_dir, os.path.basename(old))
        emb = self._take_row(old) if is_excluded_path(new) else None
        shutil.move(old, new)
        if emb is None:
            self._repath(old, new)
        return Op("move", old, new, emb)

    def move_to_duplicates(self, path: str) -> Op:
        """One-click cleanup: move into a _Duplicates folder beside the original."""
        return self.move_file(path, os.path.join(os.path.dirname(path), DUPLICATES_DIR))

    def delete_file(self, path: str) -> Op:
        """Hold the file in a hidden folder (so Undo works); flush_deleted()
        sends held files to the Recycle Bin when the app closes."""
        emb = self._take_row(path)
        if not os.path.exists(path):
            return Op("delete", path, "", emb)
        hold = os.path.join(os.path.dirname(path), HOLD_DIR)
        os.makedirs(hold, exist_ok=True)
        if sys.platform.startswith("win"):
            try:  # hide the holding folder in Explorer
                import ctypes

                ctypes.windll.kernel32.SetFileAttributesW(hold, 0x02)
            except Exception:
                pass
        dst = _unique_path(hold, os.path.basename(path))
        os.replace(path, dst)
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO held VALUES (?,?,?)", (dst, path, emb))
        return Op("delete", path, dst, emb)

    def undo(self, op: Op):
        """Reverse one operation. Raises if the original location is taken."""
        if op.kind == "rename" and op.src == op.dst:
            return
        if not op.dst:  # file was already gone; just restore the index entry
            self._put_row(op.src, op.emb)
            return
        if os.path.exists(op.src):
            raise FileExistsError(f"Can't undo: '{os.path.basename(op.src)}' already exists again")
        if not os.path.exists(op.dst):
            raise FileNotFoundError(f"Can't undo: '{os.path.basename(op.dst)}' was moved or deleted")
        os.makedirs(os.path.dirname(op.src), exist_ok=True)
        shutil.move(op.dst, op.src)
        if op.kind == "delete":
            with self._conn() as c:
                c.execute("DELETE FROM held WHERE held_path=?", (op.dst,))
        if op.emb is not None:
            self._put_row(op.src, op.emb)
        else:
            self._repath(op.dst, op.src)
        _remove_if_empty(os.path.dirname(op.dst))

    def flush_deleted(self) -> int:
        """Send held (deleted) files to the Recycle Bin. Network drives have no
        Recycle Bin (Windows would delete for good), so those files go to a
        '_Deleted by ItemFinder' folder at the top of their photo folder."""
        from send2trash import send2trash

        with self._conn() as c:
            rows = c.execute("SELECT held_path, original FROM held").fetchall()
        n = 0
        for p, original in rows:
            try:
                if os.path.exists(p):
                    if is_network_path(p):
                        dest = os.path.join(self._root_of(original or p), NETWORK_DELETED_DIR)
                        os.makedirs(dest, exist_ok=True)
                        shutil.move(p, _unique_path(dest, os.path.basename(p)))
                    else:
                        send2trash(p)
                    n += 1
                with self._conn() as c:
                    c.execute("DELETE FROM held WHERE held_path=?", (p,))
                _remove_if_empty(os.path.dirname(p))
            except Exception:
                pass
        return n

    def _root_of(self, path: str) -> str:
        """The indexed photo folder that contains `path` (or its folder)."""
        n = norm_path(path)
        best = ""
        for f in self.folders():
            nf = norm_path(f).rstrip("\\/") + os.sep
            if n.startswith(nf) and len(nf) > len(best):
                best = f
        return best or os.path.dirname(os.path.dirname(path))

    def held_has_network(self) -> bool:
        with self._conn() as c:
            return any(is_network_path(r[0]) for r in c.execute("SELECT held_path FROM held"))

    def held_count(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM held").fetchone()[0]

    # ---- row helpers
    def _take_row(self, path: str) -> bytes | None:
        with self._conn() as c:
            r = c.execute("SELECT emb FROM images WHERE path=?", (path,)).fetchone()
            c.execute("DELETE FROM images WHERE path=?", (path,))
        self.invalidate()
        return r[0] if r else None

    def _put_row(self, path: str, emb: bytes | None):
        if emb is None or is_excluded_path(path):
            return
        try:
            st = os.stat(path)
            sig = (st.st_mtime, st.st_size)
        except OSError:
            return
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO images VALUES (?,?,?,?)", (path, *sig, emb))
        self.invalidate()

    def _repath(self, old: str, new: str):
        try:
            st = os.stat(new)
            sig = (st.st_mtime, st.st_size)
        except OSError:
            sig = (0, 0)
        o, n = norm_path(old), norm_path(new)
        with self._conn() as c:
            c.execute("UPDATE OR REPLACE images SET path=?, mtime=?, size=? WHERE path=?", (new, *sig, old))
            c.execute("UPDATE OR IGNORE ignored SET a=? WHERE a=?", (n, o))
            c.execute("UPDATE OR IGNORE ignored SET b=? WHERE b=?", (n, o))
        self.invalidate()


def _remove_if_empty(folder: str):
    try:
        if os.path.basename(folder) in (HOLD_DIR, DUPLICATES_DIR) and not os.listdir(folder):
            os.rmdir(folder)
    except OSError:
        pass


def combine(img_vec: np.ndarray | None, txt_vec: np.ndarray | None, text_weight: float = 0.5) -> np.ndarray:
    """Blend photo + words, e.g. drop a turtle photo and type 'blue'."""
    if img_vec is None:
        return txt_vec
    if txt_vec is None:
        return img_vec
    v = (1 - text_weight) * img_vec + text_weight * txt_vec
    return v / (np.linalg.norm(v) + 1e-8)


def to_percent(score: float, text_only: bool = False) -> int:
    """Map raw cosine similarity to a friendlier 0-100 'match' value."""
    lo, hi = (0.15, 0.35) if text_only else (0.50, 0.95)
    return int(round(max(0.0, min(1.0, (score - lo) / (hi - lo))) * 100))


def _fmt_eta(sec: float) -> str:
    sec = int(sec)
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m"
    return f"{sec // 3600}h {sec % 3600 // 60}m"
