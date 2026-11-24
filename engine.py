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


# --------------------------------------------------------------------------- index
@dataclass
class ScanResult:
    added: int = 0
    removed: int = 0
    failed: int = 0
    total: int = 0
    cancelled: bool = False


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
                dirnames[:] = [d for d in dirnames if not d.startswith((".", "$"))]
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
        on_disk = self._walk(self.folders(), cancel)
        if cancel.is_set():
            res.cancelled = True
            return res

        with self._conn() as c:
            known = {r[0]: (r[1], r[2]) for r in c.execute("SELECT path, mtime, size FROM images")}
            bad = {r[0]: (r[1], r[2]) for r in c.execute("SELECT path, mtime, size FROM failed")}

            gone = [p for p in known if p not in on_disk]
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
        out = []
        for n, idxs in enumerate(groups):
            if cancel.is_set():
                return []
            if n % 500 == 0:
                progress(n, len(groups), f"Checking item codes {n:,}/{len(groups):,}...")
            sub = self._matrix[idxs]
            sims = sub @ sub.T
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

        out = []
        for members in groups.values():
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

    # ---- file actions (keep the index in sync)
    def rename_file(self, old: str, new_name: str) -> str:
        new_name = new_name.strip()
        if not new_name or any(ch in new_name for ch in '\\/:*?"<>|'):
            raise ValueError("Invalid file name")
        if not os.path.splitext(new_name)[1]:
            new_name += os.path.splitext(old)[1]
        new = os.path.join(os.path.dirname(old), new_name)
        if norm_path(new) == norm_path(old):
            return old
        if os.path.exists(new):
            raise FileExistsError(f"'{new_name}' already exists in that folder")
        os.rename(old, new)
        self._repath(old, new)
        return new

    def move_file(self, old: str, dest_dir: str) -> str:
        os.makedirs(dest_dir, exist_ok=True)
        base, ext = os.path.splitext(os.path.basename(old))
        new = os.path.join(dest_dir, base + ext)
        n = 2
        while os.path.exists(new):
            new = os.path.join(dest_dir, f"{base} ({n}){ext}")
            n += 1
        shutil.move(old, new)
        self._repath(old, new)
        return new

    def delete_file(self, path: str):
        """Send to the Recycle Bin (recoverable), never a permanent delete."""
        from send2trash import send2trash

        if os.path.exists(path):
            send2trash(path)
        with self._conn() as c:
            c.execute("DELETE FROM images WHERE path=?", (path,))
        self.invalidate()

    def _repath(self, old: str, new: str):
        try:
            st = os.stat(new)
            sig = (st.st_mtime, st.st_size)
        except OSError:
            sig = (0, 0)
        with self._conn() as c:
            c.execute("UPDATE OR REPLACE images SET path=?, mtime=?, size=? WHERE path=?", (new, *sig, old))
        self.invalidate()


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
