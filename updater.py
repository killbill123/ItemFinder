"""
ItemFinder auto-updater (GitHub Releases).

Flow: check latest release -> newer version? -> download "...Setup.exe"
-> verify SHA-256 -> run the installer silently -> installer restarts the app.

Privacy: the only thing sent is a plain request for the latest release
information and the installer download. No photo data, file names or
usage information ever leaves the PC.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from dataclasses import dataclass
from typing import Callable

import version

API_BASE = os.environ.get("ITEMFINDER_UPDATE_API", "https://api.github.com")
USER_AGENT = f"ItemFinder-Updater/{version.__version__}"
CHECK_EVERY_SECONDS = 20 * 3600


@dataclass
class UpdateInfo:
    version: str
    notes: str
    url: str
    size: int
    sha256: str | None
    page: str
    name: str


# --------------------------------------------------------------------------- helpers
def parse_version(v: str) -> tuple[int, ...]:
    nums = [int(x) for x in re.findall(r"\d+", v)[:3]]
    return tuple(nums + [0] * (3 - len(nums)))


def is_installed_build() -> bool:
    """Auto-install only works in the packaged/installed app."""
    return bool(getattr(sys, "frozen", False)) and sys.platform.startswith("win")


def enabled() -> bool:
    return bool(version.UPDATE_REPO)


def _get(url: str, accept: str = "application/vnd.github+json", timeout: int = 20):
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": USER_AGENT})
    return urllib.request.urlopen(req, timeout=timeout)


# --------------------------------------------------------------------------- settings
def _settings_path() -> str:
    import engine

    return os.path.join(engine.app_data_dir(), "update_settings.json")


def load_settings() -> dict:
    try:
        with open(_settings_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(s: dict):
    try:
        with open(_settings_path(), "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
    except Exception:
        pass


def check_due() -> bool:
    s = load_settings()
    if s.get("auto_check", True) is False:
        return False
    return time.time() - float(s.get("last_check", 0)) > CHECK_EVERY_SECONDS


# --------------------------------------------------------------------------- check
def check(ignore_skipped: bool = False) -> UpdateInfo | None:
    """Return UpdateInfo if a newer release exists, else None. Raises on network errors."""
    if not enabled():
        return None
    with _get(f"{API_BASE}/repos/{version.UPDATE_REPO}/releases/latest") as r:
        data = json.load(r)

    s = load_settings()
    s["last_check"] = time.time()
    save_settings(s)

    if data.get("draft") or data.get("prerelease"):
        return None
    latest = str(data.get("tag_name", "")).lstrip("vV")
    if not latest or parse_version(latest) <= parse_version(version.__version__):
        return None
    if not ignore_skipped and s.get("skip_version") == latest:
        return None

    assets = data.get("assets") or []
    setup = next((a for a in assets if a.get("name", "").lower().endswith("setup.exe")), None)
    if setup is None:
        return None

    sha = None
    digest = setup.get("digest") or ""  # GitHub supplies "sha256:<hex>" for new uploads
    if digest.lower().startswith("sha256:"):
        sha = digest.split(":", 1)[1].strip().lower()
    if sha is None:  # fall back to a "<name>.sha256" asset
        side = next((a for a in assets if a.get("name") == setup["name"] + ".sha256"), None)
        if side:
            with _get(side["browser_download_url"], accept="application/octet-stream") as r:
                sha = r.read().decode().split()[0].strip().lower()

    return UpdateInfo(
        version=latest,
        notes=(data.get("body") or "").strip(),
        url=setup["browser_download_url"],
        size=int(setup.get("size") or 0),
        sha256=sha,
        page=data.get("html_url", ""),
        name=setup["name"],
    )


def skip_version(v: str):
    s = load_settings()
    s["skip_version"] = v
    save_settings(s)


# --------------------------------------------------------------------------- download
def download(
    info: UpdateInfo,
    progress: Callable[[int, int], None] | None = None,
    cancel: threading.Event | None = None,
) -> str:
    progress = progress or (lambda d, t: None)
    cancel = cancel or threading.Event()
    folder = os.path.join(tempfile.gettempdir(), "ItemFinder-update")
    os.makedirs(folder, exist_ok=True)
    dest = os.path.join(folder, os.path.basename(info.name))
    part = dest + ".part"
    h = hashlib.sha256()
    done = 0
    with _get(info.url, accept="application/octet-stream", timeout=60) as r, open(part, "wb") as f:
        total = int(r.headers.get("Content-Length") or info.size or 0)
        while True:
            if cancel.is_set():
                f.close()
                os.remove(part)
                raise InterruptedError("Download cancelled")
            chunk = r.read(1024 * 256)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            done += len(chunk)
            progress(done, total)
    if info.sha256 and h.hexdigest().lower() != info.sha256:
        os.remove(part)
        raise ValueError("Downloaded update is corrupted (checksum mismatch). Please try again.")
    if info.size and done != info.size:
        os.remove(part)
        raise ValueError("Download was incomplete. Please try again.")
    os.replace(part, dest)
    return dest


# --------------------------------------------------------------------------- install
def launch_installer(path: str):
    """Start the installer silently and detached. The caller must quit the app
    right after this; the installer waits for it to close, updates the files
    and starts ItemFinder again."""
    args = [path, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-"]
    flags = 0
    if sys.platform.startswith("win"):
        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(args, creationflags=flags, close_fds=True)
