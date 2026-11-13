"""Core logic shared by the Gdrive-Pair app and the tray indicator.

The app never syncs anything itself. It reads state written by
rclone-sync.sh and systemd, and controls the systemd user units:

  rclone-bisync@<name>.service   one sync (started by timer, watcher, Sync now)
  rclone-bisync@<name>.timer     periodic sync; enabled = not paused
  rclone-watch@<name>.service    local folder watcher; enabled with the timer
  rclone-resync@<name>.service   first sync (baseline); enables the two above
  rclone-allow@<name>.service    one approved sync with the delete limit lifted
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

from . import GUIDE_URL

HOME = Path.home()
CONF_DIR = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "rclone-sync"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", HOME / ".local/state")) / "rclone-sync"
LOCAL_BASE = HOME / "Google Drive"
BIN_DIR = HOME / ".local/bin"
SYNC_SCRIPT = BIN_DIR / "rclone-sync.sh"
UNIT_DIR = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "systemd/user"

NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
FOLDER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,}$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(cmd: list[str], timeout: float | None = 30, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=check)


def systemctl(*args: str, timeout: float = 30) -> subprocess.CompletedProcess:
    return run(["systemctl", "--user", *args], timeout=timeout)


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} TB"


def human_ago(t: float, now: float | None = None) -> str:
    d = max(0, int((now or time.time()) - t))
    if d < 60:
        return "just now"
    if d < 3600:
        return f"{d // 60} min ago"
    if d < 86400:
        return f"{d // 3600} h ago"
    return f"{d // 86400} d ago"


def human_in(t: float, now: float | None = None) -> str:
    d = int(t - (now or time.time()))
    if d <= 30:
        return "soon"
    if d < 3600:
        return f"in {max(1, round(d / 60))} min"
    return f"in {d // 3600} h"


def tilde(path: Path | str) -> str:
    p = str(path)
    h = str(HOME)
    return "~" + p[len(h):] if p == h or p.startswith(h + "/") else p


# ---------------------------------------------------------------------------
# Pairs (config files)
# ---------------------------------------------------------------------------

@dataclass
class Pair:
    name: str
    remote: str            # full rclone path, e.g. gdrive,root_folder_id=ID:
    local: Path
    notify_sync: bool = True
    conf_path: Path = field(default=None)  # type: ignore[assignment]

    @property
    def remote_name(self) -> str:
        return re.split(r"[,:]", self.remote, maxsplit=1)[0]

    @property
    def folder_id(self) -> str | None:
        m = re.search(r"root_folder_id=([A-Za-z0-9_-]+)", self.remote)
        return m.group(1) if m else None

    @property
    def drive_url(self) -> str | None:
        fid = self.folder_id
        return f"https://drive.google.com/drive/folders/{fid}" if fid else None

    # unit names
    def unit(self, kind: str) -> str:
        return {
            "sync": f"rclone-bisync@{self.name}.service",
            "timer": f"rclone-bisync@{self.name}.timer",
            "watch": f"rclone-watch@{self.name}.service",
            "resync": f"rclone-resync@{self.name}.service",
            "allow": f"rclone-allow@{self.name}.service",
        }[kind]

    def units(self) -> list[str]:
        return [self.unit(k) for k in ("sync", "timer", "watch", "resync", "allow")]

    @property
    def log_path(self) -> Path:
        return STATE_DIR / f"{self.name}.log"

    @property
    def history_path(self) -> Path:
        return STATE_DIR / f"{self.name}.history.jsonl"

    @property
    def workdir(self) -> Path:
        return STATE_DIR / "bisync" / self.name

    def has_baseline(self) -> bool:
        return any(self.workdir.glob("*.path1.lst")) and any(self.workdir.glob("*.path2.lst"))


_ASSIGN_RE = re.compile(r"^\s*([A-Z_]+)=(.*?)\s*$")


def _parse_value(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        raw = raw[1:-1]
    return raw.replace("${HOME}", str(HOME)).replace("$HOME", str(HOME))


def read_conf(path: Path) -> dict[str, str]:
    """Read simple KEY=value lines. The script sources the file as bash;
    the app only needs the plain assignments."""
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = _ASSIGN_RE.match(line)
        if m and not m.group(2).startswith("("):
            values[m.group(1)] = _parse_value(m.group(2))
    return values


def load_pairs() -> list[Pair]:
    pairs = []
    if not CONF_DIR.is_dir():
        return pairs
    for path in sorted(CONF_DIR.glob("*.conf")):
        name = path.stem
        if not NAME_RE.match(name):
            continue
        try:
            v = read_conf(path)
        except OSError:
            continue
        if not v.get("REMOTE") or not v.get("LOCAL"):
            continue
        pairs.append(Pair(
            name=name,
            remote=v["REMOTE"],
            local=Path(v["LOCAL"].rstrip("/")),
            notify_sync=v.get("NOTIFY_SYNC", "1") != "0",
            conf_path=path,
        ))
    return pairs


def set_conf_value(path: Path, key: str, value: str) -> None:
    lines = path.read_text().splitlines()
    out, done = [], False
    for line in lines:
        m = _ASSIGN_RE.match(line)
        if m and m.group(1) == key and not line.lstrip().startswith("#"):
            out.append(f"{key}={value}")
            done = True
        else:
            out.append(line)
    if not done:
        out.append(f"{key}={value}")
    tmp = path.with_suffix(".conf.tmp")
    tmp.write_text("\n".join(out) + "\n")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# History and logs
# ---------------------------------------------------------------------------

@dataclass
class Run:
    time: float
    kind: str           # sync | first-sync | allow
    result: str         # ok | fail | blocked
    up: int = 0
    down: int = 0
    del_local: int = 0
    del_drive: int = 0
    reason: str = ""

    @property
    def summary(self) -> str:
        if self.result == "ok":
            parts = []
            if self.up:
                parts.append(f"↑{self.up}")
            if self.down:
                parts.append(f"↓{self.down}")
            if self.del_drive:
                parts.append(f"{self.del_drive} deleted on Drive")
            if self.del_local:
                parts.append(f"{self.del_local} deleted locally")
            text = ", ".join(parts) or "No changes"
        else:
            text = self.reason or "Failed"
        if self.kind == "first-sync":
            text += " (first sync)"
        elif self.kind == "allow":
            text += " (approved deletes)"
        return text


def read_history(pair: Pair, limit: int = 200) -> list[Run]:
    """Newest first."""
    runs: list[Run] = []
    try:
        lines = pair.history_path.read_text().splitlines()
    except OSError:
        return runs
    for line in lines[-limit:]:
        try:
            d = json.loads(line)
            t = datetime.fromisoformat(d["time"]).timestamp()
        except (ValueError, KeyError, TypeError):
            continue
        runs.append(Run(
            time=t, kind=d.get("kind", "sync"), result=d.get("result", "fail"),
            up=int(d.get("up", 0)), down=int(d.get("down", 0)),
            del_local=int(d.get("del_local", 0)), del_drive=int(d.get("del_drive", 0)),
            reason=d.get("reason", ""),
        ))
    runs.reverse()
    return runs


_MARKER_RE = re.compile(r"^(\d{4}/\d\d/\d\d \d\d:\d\d:\d\d) DRIVESYNC-START", re.M)


def _read_tail(path: Path, max_bytes: int) -> str:
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read()
    except OSError:
        return ""
    text = data.decode("utf-8", "replace")
    if size > max_bytes:
        text = text.split("\n", 1)[-1]
    return text


def read_log_tail(pair: Pair, lines: int = 400) -> str:
    text = _read_tail(pair.log_path, 256 * 1024)
    return "\n".join(text.splitlines()[-lines:])


def log_segments(pair: Pair, max_bytes: int = 2 * 1024 * 1024) -> list[tuple[float, str]]:
    """Split the log into runs: [(start_time, text)], oldest first."""
    text = _read_tail(pair.log_path, max_bytes)
    segs = []
    matches = list(_MARKER_RE.finditer(text))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        t = time.mktime(time.strptime(m.group(1), "%Y/%m/%d %H:%M:%S"))
        segs.append((t, text[m.start():end].rstrip()))
    return segs


def log_for_run(history: list[Run], index: int, segments: list[tuple[float, str]]) -> str | None:
    """Find the log segment of history[index] (history is newest first)."""
    run_ = history[index]
    prev_t = history[index + 1].time if index + 1 < len(history) else float("-inf")
    best = None
    for t, text in segments:
        if prev_t < t <= run_.time + 1:
            best = text
    return best


_PROGRESS_RE = re.compile(
    r"Transferred:\s+([\d.]+\s*[KMGTP]?i?B)\s*/\s*([\d.]+\s*[KMGTP]?i?B),\s*(\d+|-)%")


def first_sync_progress(pair: Pair) -> tuple[str, str, int] | None:
    """(done, total, percent) from the latest rclone stats line of this run."""
    text = _read_tail(pair.log_path, 128 * 1024)
    idx = text.rfind("DRIVESYNC-START")
    if idx < 0:
        return None
    found = _PROGRESS_RE.findall(text[idx:])
    if not found:
        return None
    done, total, pct = found[-1]
    return done.replace("i", ""), total.replace("i", ""), 0 if pct == "-" else int(pct)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

class State(Enum):
    OK = "ok"
    SYNCING = "syncing"
    FIRST_SYNC = "first-sync"
    PAUSED = "paused"
    BLOCKED = "blocked"
    ERROR = "error"
    NEEDS_FIRST_SYNC = "needs-first-sync"


@dataclass
class PairStatus:
    pair: Pair
    state: State
    text: str
    last: Run | None = None
    next_time: float | None = None
    progress: tuple[str, str, int] | None = None
    paused: bool = False
    syncing: bool = False


def _unit_props(units: list[str]) -> dict[str, dict[str, str]]:
    if not units:
        return {}
    r = systemctl("show", "--timestamp=unix",
                  "-p", "Id,ActiveState,UnitFileState,NextElapseUSecRealtime,NextElapseUSecMonotonic", *units)
    props: dict[str, dict[str, str]] = {}
    for block in r.stdout.strip().split("\n\n"):
        d = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        if "Id" in d:
            props[d["Id"]] = d
    return props


def _active(p: dict[str, str] | None) -> bool:
    return bool(p) and p.get("ActiveState") in ("active", "activating", "reloading")


_SPAN_UNITS = {"w": 604800, "d": 86400, "h": 3600, "min": 60, "s": 1, "ms": 1e-3, "us": 1e-6}
_SPAN_RE = re.compile(r"([\d.]+)\s*(w|d|h|min|ms|us|s)\b")


def _parse_monotonic(value: str | None) -> float | None:
    """systemd prints monotonic elapse times as a span since boot, e.g.
    '3h 30min 39.3s'. Convert to a wall-clock timestamp."""
    if not value or value in ("0", "infinity"):
        return None
    parts = _SPAN_RE.findall(value)
    if not parts:
        return None
    secs = sum(float(n) * _SPAN_UNITS[u] for n, u in parts)
    return time.time() + (secs - time.monotonic())


def _parse_unix(value: str | None) -> float | None:
    if value and value.startswith("@"):
        try:
            return float(value[1:])
        except ValueError:
            return None
    return None


def get_statuses(pairs: list[Pair] | None = None) -> list[PairStatus]:
    pairs = load_pairs() if pairs is None else pairs
    props = _unit_props([u for p in pairs for u in p.units()])
    now = time.time()
    out = []
    for pair in pairs:
        up = {k: props.get(pair.unit(k)) for k in ("sync", "timer", "watch", "resync", "allow")}
        timer_enabled = (up["timer"] or {}).get("UnitFileState") in ("enabled", "enabled-runtime")
        history = read_history(pair, limit=20)
        last = history[0] if history else None
        tp = up["timer"] or {}
        next_time = None
        if timer_enabled:
            next_time = (_parse_unix(tp.get("NextElapseUSecRealtime"))
                         or _parse_monotonic(tp.get("NextElapseUSecMonotonic")))
        st = PairStatus(pair=pair, state=State.OK, text="", last=last, next_time=next_time,
                        paused=not timer_enabled, syncing=_active(up["sync"]) or _active(up["allow"]))

        if _active(up["resync"]):
            st.state = State.FIRST_SYNC
            st.progress = first_sync_progress(pair)
            if st.progress:
                done, total, pct = st.progress
                st.text = f"First sync {pct}% ({done} / {total})"
            else:
                st.text = "First sync starting…"
        elif not pair.has_baseline():
            st.state = State.NEEDS_FIRST_SYNC
            if last and last.kind == "first-sync" and last.result != "ok":
                st.text = f"First sync failed: {last.reason}"
            else:
                st.text = "First sync needed"
        elif st.syncing:
            st.state = State.SYNCING
            st.text = "Syncing…"
        elif not timer_enabled:
            st.state = State.PAUSED
            st.text = "Paused"
        elif last and last.result == "blocked":
            st.state = State.BLOCKED
            st.text = f"Blocked: {last.reason[:1].lower() + last.reason[1:]}"
        elif last and last.result == "fail":
            st.state = State.ERROR
            st.text = f"Failed {human_ago(last.time, now)}: {last.reason}"
        else:
            st.state = State.OK
            parts = [f"Synced {human_ago(last.time, now)}" if last else "Waiting for first sync"]
            if next_time:
                parts.append(f"next {human_in(next_time, now)}")
            st.text = " · ".join(parts)
        out.append(st)
    return out


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

class ActionError(Exception):
    pass


def _check(r: subprocess.CompletedProcess, what: str) -> None:
    if r.returncode != 0:
        raise ActionError(f"{what} failed: {(r.stderr or r.stdout).strip()}")


def sync_now(pair: Pair) -> None:
    _check(systemctl("start", "--no-block", pair.unit("sync")), "Starting sync")


def pause(pair: Pair) -> None:
    # disable --now stops the timer and watcher; a sync already running is a
    # separate unit and finishes on its own.
    _check(systemctl("disable", "--now", pair.unit("timer"), pair.unit("watch")), "Pausing")


def resume(pair: Pair) -> None:
    _check(systemctl("enable", "--now", pair.unit("timer"), pair.unit("watch")), "Resuming")


def allow_once(pair: Pair) -> None:
    _check(systemctl("start", "--no-block", pair.unit("allow")), "Starting approved sync")


def start_first_sync(pair: Pair) -> None:
    systemctl("reset-failed", pair.unit("resync"))
    _check(systemctl("start", "--no-block", pair.unit("resync")), "Starting first sync")


@dataclass
class Entry:
    """One line of a bisync listing file."""
    is_dir: bool
    size: int
    hash: str      # e.g. "md5:abc…" on Drive, "-" when unknown (local)
    modtime: str


_LISTING_RE = re.compile(r'^(\S)\s+(-?\d+)\s+(\S+)\s+\S+\s+(\S+)\s+"(.*)"$')


def _read_listing(path: Path | None) -> dict[str, Entry]:
    entries: dict[str, Entry] = {}
    if path is None:
        return entries
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return entries
    for line in lines:
        m = _LISTING_RE.match(line)
        if not m:
            continue
        typ, size, hsh, modtime, name = m.groups()
        try:
            name = json.loads(f'"{name}"')   # rclone quotes paths Go-style
        except json.JSONDecodeError:
            pass
        entries[name] = Entry(is_dir=typ == "d", size=int(size), hash=hsh, modtime=modtime)
    return entries


def _listing(pair: Pair, side: str, suffix: str) -> Path | None:
    n = "1" if side == "drive" else "2"
    files = sorted(pair.workdir.glob(f"*.path{n}.lst{suffix}"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def _same_file(old: Entry, new: Entry, old_path: str, new_path: str) -> bool:
    """Is `new` the same file as `old`, just moved or renamed?"""
    if old.is_dir or new.is_dir or old.size != new.size:
        return False
    if old.hash != "-" and new.hash != "-":
        return old.hash == new.hash                       # content hash: allows renames
    return (Path(old_path).name == Path(new_path).name    # no hash (local side): need same
            and old.modtime == new.modtime)               # name and modification time


@dataclass
class ReviewItem:
    kind: str                  # "move" or "delete"
    side: str                  # where it will be applied: "local" or "drive"
    path: str                  # current path on that side
    new_path: str | None = None


def review_changes(pair: Pair) -> list[ReviewItem]:
    """What an approved sync would do with the files that vanished on one side.

    A vanished file that reappears elsewhere on the same side with the same
    content is reported as a move; everything else as a real deletion.
    Slow: runs a dry-run sync. Call from a worker thread."""
    r = run([str(SYNC_SCRIPT), pair.name, "--list-deletes"], timeout=900)
    if r.returncode != 0:
        raise ActionError("Could not check the changes:\n" + (r.stderr.strip() or r.stdout.strip()))
    deleted: dict[str, list[str]] = {"drive": [], "local": []}
    new: dict[str, list[str]] = {"drive": [], "local": []}
    for line in r.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3 or parts[1] not in deleted or not parts[2]:
            continue
        change, side, path = parts
        if change == "del":
            deleted[side].append(path)
        elif change == "new":
            new[side].append(path)

    items: list[ReviewItem] = []
    for changed in ("drive", "local"):
        applied = "local" if changed == "drive" else "drive"
        before = _read_listing(_listing(pair, changed, ""))       # last synced state
        after = _read_listing(_listing(pair, changed, "-dry"))    # state seen by the dry run
        unused = [p for p in new[changed] if p in after and not after[p].is_dir]
        for old_path in deleted[changed]:
            old = before.get(old_path)
            match = None
            if old:
                cands = [p for p in unused if _same_file(old, after[p], old_path, p)]
                # Prefer a candidate with the same file name (a plain move).
                cands.sort(key=lambda p: Path(p).name != Path(old_path).name)
                match = cands[0] if cands else None
            if match:
                unused.remove(match)
                items.append(ReviewItem("move", applied, old_path, match))
            else:
                items.append(ReviewItem("delete", applied, old_path))
    return items


def set_notify_sync(pair: Pair, enabled: bool) -> None:
    set_conf_value(pair.conf_path, "NOTIFY_SYNC", "1" if enabled else "0")
    pair.notify_sync = enabled


def remove_pair(pair: Pair) -> None:
    """Stop and remove the sync setup. Never touches the local folder or Drive."""
    units = pair.units()
    systemctl("disable", "--now", pair.unit("timer"), pair.unit("watch"))
    systemctl("stop", *units, timeout=120)
    # Confirm everything is down before deleting state.
    props = _unit_props(units)
    still = [u for u in units if _active(props.get(u))]
    if still:
        raise ActionError("These units are still running: " + ", ".join(still))
    pair.conf_path.unlink(missing_ok=True)
    for p in (pair.log_path, pair.log_path.with_suffix(".log.1"), pair.history_path,
              STATE_DIR / f"{pair.name}.last-failure"):
        p.unlink(missing_ok=True)
    shutil.rmtree(pair.workdir, ignore_errors=True)
    systemctl("reset-failed", *units)


# ---------------------------------------------------------------------------
# Add-pair wizard helpers
# ---------------------------------------------------------------------------

def list_drive_remotes() -> list[str]:
    r = run(["rclone", "listremotes", "--long"])
    remotes = []
    for line in r.stdout.splitlines():
        name, _, typ = line.partition(":")
        if typ.strip() == "drive":
            remotes.append(name.strip())
    return remotes


def parse_folder_id(text: str) -> str | None:
    text = text.strip()
    for pat in (r"/folders/([A-Za-z0-9_-]+)", r"[?&]id=([A-Za-z0-9_-]+)"):
        m = re.search(pat, text)
        if m:
            return m.group(1)
    return text if FOLDER_ID_RE.match(text) else None


def remote_path(remote_name: str, folder_id: str) -> str:
    return f"{remote_name},root_folder_id={folder_id}:"


_DRIVE_FLAGS = ["--drive-skip-gdocs", "--drive-skip-shortcuts"]


@dataclass
class FolderCheck:
    entries: list[str]     # top-level names (dirs end with /)


def check_folder(remote_name: str, folder_id: str) -> FolderCheck:
    """Verify the folder is reachable and list its top level. Worker thread."""
    r = run(["rclone", "lsjson", remote_path(remote_name, folder_id), "--max-depth", "1",
             "--no-modtime", "--no-mimetype", *_DRIVE_FLAGS], timeout=120)
    if r.returncode != 0:
        err = r.stderr.strip().splitlines()
        msg = err[-1] if err else "unknown error"
        if "notFound" in r.stderr or "not found" in r.stderr.lower() or "404" in r.stderr:
            msg = "Folder not found, or this account has no access to it."
        raise ActionError(msg)
    try:
        items = json.loads(r.stdout or "[]")
    except json.JSONDecodeError as e:
        raise ActionError(f"Unexpected rclone output: {e}") from e
    names = sorted((i["Name"] + ("/" if i.get("IsDir") else "") for i in items),
                   key=lambda s: (not s.endswith("/"), s.lower()))
    return FolderCheck(entries=names)


def folder_size(remote_name: str, folder_id: str) -> tuple[int, int]:
    """(file count, bytes). Can take a while on big folders. Worker thread."""
    r = run(["rclone", "size", "--json", remote_path(remote_name, folder_id), *_DRIVE_FLAGS],
            timeout=1800)
    if r.returncode != 0:
        raise ActionError("Could not measure folder size: " + r.stderr.strip().splitlines()[-1]
                          if r.stderr.strip() else "Could not measure folder size")
    d = json.loads(r.stdout)
    return int(d.get("count", 0)), int(d.get("bytes", 0))


def local_path_for(name: str) -> Path:
    return LOCAL_BASE / name


def validate_name(name: str, pairs: list[Pair] | None = None) -> str | None:
    """Return an error message, or None if the name is usable."""
    if not name:
        return "Enter a name."
    if not NAME_RE.match(name):
        return "Use only letters, digits, '.', '_' and '-'."
    if name.startswith("."):
        return "The name can't start with '.'."
    if (CONF_DIR / f"{name}.conf").exists():
        return f"A pair called “{name}” already exists."
    local = local_path_for(name)
    if local.exists() or local.is_symlink():
        if not local.is_dir() or local.is_symlink():
            return f"{tilde(local)} exists and is not a folder."
        if any(local.iterdir()):
            return f"{tilde(local)} already has files in it. Pick another name."
    for p in pairs or []:
        if p.local == local:
            return f"{tilde(local)} is already used by “{p.name}”."
    return None


def find_pair_for_folder(folder_id: str, pairs: list[Pair]) -> Pair | None:
    return next((p for p in pairs if p.folder_id == folder_id), None)


@dataclass
class DiskCheck:
    free: int
    margin: int
    fits: bool


def disk_check(path: Path, needed: int) -> DiskCheck:
    probe = path
    while not probe.exists():
        probe = probe.parent
    usage = shutil.disk_usage(probe)
    margin = max(1_000_000_000, int(usage.total * 0.05))
    return DiskCheck(free=usage.free, margin=margin, fits=needed + margin <= usage.free)


def create_pair(name: str, remote_name: str, folder_id: str) -> Pair:
    """Write the config and start the first sync as a background job."""
    if validate_name(name, load_pairs()):
        raise ActionError(validate_name(name, load_pairs()) or "Invalid name")
    if not FOLDER_ID_RE.match(folder_id) or not re.match(r"^[A-Za-z0-9._ -]+$", remote_name):
        raise ActionError("Invalid remote or folder ID.")
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    conf = CONF_DIR / f"{name}.conf"
    conf.write_text(
        "# Created by Gdrive-Pair\n"
        f'REMOTE="{remote_path(remote_name, folder_id)}"\n'
        f'LOCAL="$HOME/Google Drive/{name}"\n'
        "NOTIFY_SYNC=1\n"
    )
    pair = next(p for p in load_pairs() if p.name == name)
    start_first_sync(pair)
    return pair


# ---------------------------------------------------------------------------
# Setup checks (for the "missing setup" screens)
# ---------------------------------------------------------------------------

@dataclass
class SetupProblem:
    title: str
    detail: str
    command: str | None = None


def setup_problems() -> list[SetupProblem]:
    problems = []
    if not shutil.which("rclone"):
        problems.append(SetupProblem("rclone is not installed",
                                     "Gdrive-Pair uses rclone to talk to Google Drive.",
                                     "sudo pacman -S rclone"))
    if not shutil.which("inotifywait"):
        problems.append(SetupProblem("inotify-tools is not installed",
                                     "Needed to notice local changes quickly.",
                                     "sudo pacman -S inotify-tools"))
    if not SYNC_SCRIPT.exists() or not (UNIT_DIR / "rclone-bisync@.service").exists():
        problems.append(SetupProblem("Sync scripts are not installed",
                                     "Run the installer from the Gdrive-Pair project folder.",
                                     "./install.sh"))
    if shutil.which("rclone") and not list_drive_remotes():
        problems.append(SetupProblem("No Google Drive remote found",
                                     "Create one with your own client ID (see README, section 2: "
                                     f"{GUIDE_URL}). "
                                     "Choose storage type “drive”.",
                                     "rclone config"))
    return problems
