#!/usr/bin/env python3
"""
cwlog_view_seek.py - fast terminal viewer for Clockwork logs.

Designed for huge timestamp-sorted logs with lines like:
  20260514T033456.507596Z\tA_Name\tVALUE\t...

No full-file index is built.  --goto uses a byte-level timestamp binary search,
so opening near a time point should be quick even for multi-GB logs.
"""

from __future__ import annotations

import argparse
import curses
import datetime as _dt
import json
import os
import re
import sys
from dataclasses import dataclass
from typing import BinaryIO, Optional

TS_RE = re.compile(r"^(\d{8}T\d{6})(?:\.(\d{1,6}))?Z?")
LOCAL_TS_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(?:\.(\d{1,6}))?(?:\s+([A-Za-z_/\+\-0-9:]+))?$"
)


CONFIG_PATH = os.path.expanduser("~/.cwlog")
MAX_HISTORY = 80
MAX_SESSIONS = 50


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return data
    except FileNotFoundError:
        return {}
    except Exception:
        # Do not let a corrupt history file stop log viewing.
        return {}


def save_config(data: dict) -> None:
    try:
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, CONFIG_PATH)
    except Exception:
        pass


def clean_history(values) -> list[str]:
    if not isinstance(values, list):
        return []
    out: list[str] = []
    for v in values:
        if isinstance(v, str) and v and v not in out:
            out.append(v)
    return out[:MAX_HISTORY]


def clean_sessions(values) -> list[dict]:
    if not isinstance(values, list):
        return []
    out: list[dict] = []
    for v in values:
        if isinstance(v, dict):
            out.append(v)
    return out[:MAX_SESSIONS]


def add_history(values: list[str], value: str) -> list[str]:
    value = value.strip()
    if not value:
        return values
    return [value] + [v for v in values if v != value][: MAX_HISTORY - 1]


def normalize_ts(s: str) -> str:
    s = s.strip()
    m = TS_RE.match(s)
    if not m:
        raise ValueError(f"Bad timestamp: {s!r}; expected e.g. 20260514T033456.507596Z")
    frac = (m.group(2) or "").ljust(6, "0")[:6]
    return f"{m.group(1)}.{frac}Z"


def ts_to_dt(ts: str) -> _dt.datetime:
    ts = normalize_ts(ts)
    return _dt.datetime.strptime(ts, "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=_dt.timezone.utc)


def dt_to_ts(dt: _dt.datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%S.%fZ")


def ts_to_local_str(ts: str) -> str:
    return ts_to_dt(ts).astimezone().strftime("%Y-%m-%d %H:%M:%S.%f %Z")


def local_tzinfo() -> _dt.tzinfo:
    tz = _dt.datetime.now().astimezone().tzinfo
    return tz or _dt.timezone.utc


def parse_jump_ts(s: str, assume_local: bool = False) -> str:
    s = s.strip()
    if not s:
        raise ValueError("empty timestamp")
    try:
        return normalize_ts(s)
    except ValueError:
        pass

    raw = s.strip()
    m = LOCAL_TS_RE.match(raw)
    if not m:
        raise ValueError(
            f"Bad timestamp: {s!r}; expected UTC like 20260514T033456.507596Z or local like 2026-05-14 13:34:56"
        )

    frac = (m.group(3) or "").ljust(6, "0")[:6]
    local_text = f"{m.group(1)} {m.group(2)}.{frac}"
    local_dt = _dt.datetime.strptime(local_text, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=local_tzinfo())
    return local_dt.astimezone(_dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def first_timestamp_str(path: str) -> Optional[str]:
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        _, line = first_timestamped_line(f, size)
    if not line:
        return None
    return extract_ts_bytes(line).decode("ascii") if extract_ts_bytes(line) else None


def extract_ts_bytes(line: bytes) -> Optional[bytes]:
    if len(line) < 16:
        return None
    first = line.split(b"\t", 1)[0]
    try:
        return normalize_ts(first.decode("ascii", "ignore")).encode("ascii")
    except Exception:
        return None


def extract_ts_str(line: str) -> Optional[str]:
    first = line.split("\t", 1)[0]
    try:
        return normalize_ts(first)
    except Exception:
        return None


def line_start_at_or_after(f: BinaryIO, pos: int, size: int) -> tuple[int, bytes]:
    if pos <= 0:
        f.seek(0)
    else:
        f.seek(min(pos, size))
        if pos < size:
            f.readline()  # discard partial line
    off = f.tell()
    if off >= size:
        return size, b""
    return off, f.readline()


def line_start_before(f: BinaryIO, pos: int, size: int, chunk: int = 65536) -> int:
    """Return byte offset of the line containing/before pos."""
    if pos <= 0:
        return 0
    pos = min(pos, size)
    end = pos
    while True:
        start = max(0, end - chunk)
        f.seek(start)
        data = f.read(end - start)
        # ignore a trailing newline at end-1, find previous newline before it
        search = data[:-1] if data.endswith(b"\n") else data
        idx = search.rfind(b"\n")
        if idx >= 0:
            return start + idx + 1
        if start == 0:
            return 0
        end = start


def read_line_at(f: BinaryIO, off: int, size: int) -> tuple[int, bytes]:
    if off < 0:
        off = 0
    if off >= size:
        return size, b""
    f.seek(off)
    return off, f.readline()


def first_timestamped_line(f: BinaryIO, size: int) -> tuple[int, bytes]:
    f.seek(0)
    while True:
        off = f.tell()
        line = f.readline()
        if not line:
            return size, b""
        if extract_ts_bytes(line) is not None:
            return off, line


def last_timestamped_line(f: BinaryIO, size: int) -> tuple[int, bytes]:
    pos = size
    while pos > 0:
        off = line_start_before(f, pos, size)
        _, line = read_line_at(f, off, size)
        if line and extract_ts_bytes(line) is not None:
            return off, line
        if off == 0:
            break
        pos = off - 1
    return size, b""


def scan_first_ge_from(f: BinaryIO, start_off: int, target: bytes, limit_off: int | None = None) -> int:
    """Scan forward from start_off and return first timestamped line >= target."""
    f.seek(start_off)
    size = os.fstat(f.fileno()).st_size
    while True:
        off = f.tell()
        if limit_off is not None and off > limit_off:
            return min(off, size)
        line = f.readline()
        if not line:
            return size
        ts = extract_ts_bytes(line)
        if ts is None:
            continue
        if ts >= target:
            return off


def refine_near(f: BinaryIO, approx_off: int, target: bytes, size: int, span: int = 8 * 1024 * 1024) -> int:
    """Search a window around the approximate offset to correct line-boundary/binary-search drift."""
    if approx_off >= size:
        start = line_start_before(f, size, size)
        # If target is after the last line, return last line rather than EOF so the UI has context.
        _, last_line = read_line_at(f, start, size)
        if last_line and (extract_ts_bytes(last_line) or b"") < target:
            return start
    start = line_start_before(f, max(0, approx_off - span), size)
    end = min(size, approx_off + span)
    return scan_first_ge_from(f, start, target, end)


def find_ts_offset(path: str, target_ts: str, debug: bool = False) -> int:
    target = normalize_ts(target_ts).encode("ascii")
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        first_off, first_line = first_timestamped_line(f, size)
        last_off, last_line = last_timestamped_line(f, size)
        first_ts = extract_ts_bytes(first_line) if first_line else None
        last_ts = extract_ts_bytes(last_line) if last_line else None

        if debug:
            print(f"target={target.decode()} size={size}", file=sys.stderr)
            print(f"first_off={first_off} first_ts={(first_ts or b'').decode('ascii','ignore')}", file=sys.stderr)
            print(f"last_off={last_off} last_ts={(last_ts or b'').decode('ascii','ignore')}", file=sys.stderr)

        if first_ts and target <= first_ts:
            return first_off
        if last_ts and target >= last_ts:
            return last_off

        lo, hi = first_off, last_off
        best = last_off
        seen: set[tuple[int, int]] = set()
        # Binary search over byte offsets. Use line offsets for bounds so progress is stable.
        for _ in range(80):
            if lo >= hi:
                break
            state = (lo, hi)
            if state in seen:
                break
            seen.add(state)
            mid = (lo + hi) // 2
            off, line = line_start_at_or_after(f, mid, size)
            if not line:
                hi = mid
                continue
            ts = extract_ts_bytes(line)
            if ts is None:
                lo = max(lo + 1, off + len(line))
                continue
            if debug and _ < 8:
                print(f"iter={_} lo={lo} hi={hi} mid={mid} off={off} ts={ts.decode()}", file=sys.stderr)
            if ts < target:
                lo = max(lo + 1, off + len(line))
            else:
                best = off
                hi = off

        refined = refine_near(f, best, target, size)
        if debug:
            _, line = read_line_at(f, refined, size)
            print(f"best={best} refined={refined} line_ts={(extract_ts_bytes(line) or b'').decode('ascii','ignore')}", file=sys.stderr)
        return refined


@dataclass
class Row:
    off: int
    text: str


class Viewer:
    def __init__(
        self,
        path: str,
        goto: Optional[str] = None,
        seek_debug: bool = False,
        session_name: Optional[str] = None,
    ):
        self.path = path
        self.size = os.path.getsize(path)
        self.center_off = 0
        self.include_pat = ""
        self.exclude_pat = ""
        self.include_re: Optional[re.Pattern[str]] = None
        self.exclude_re: Optional[re.Pattern[str]] = None
        self.window_secs = 60.0
        self.raw_secs = 5.0
        self.raw_mode = False
        self.config = load_config()
        self.include_history = clean_history(self.config.get("include_history", []))
        self.exclude_history = clean_history(self.config.get("exclude_history", []))
        self.include_pat = self.config.get("include_pat", "") if isinstance(self.config.get("include_pat", ""), str) else ""
        self.exclude_pat = self.config.get("exclude_pat", "") if isinstance(self.config.get("exclude_pat", ""), str) else ""
        self.local_time = bool(self.config.get("local_time", False))
        self.sessions = clean_sessions(self.config.get("sessions", []))
        self.rows: list[Row] = []
        self.cursor = 0
        self.message = ""
        self.last_search = ""
        self.last_search_forward = True
        self.follow_mode = False
        self.session_name = (session_name or "").strip()
        if self.session_name:
            session = self.find_session(self.session_name)
            self.apply_session(session, goto, seek_debug)
            if session is None and not goto:
                default_ts = self._session_default_ts()
                if default_ts:
                    self.center_off = find_ts_offset(path, default_ts, seek_debug)
        elif goto:
            self.center_off = find_ts_offset(path, goto, seek_debug)
        self.compile_filters()

    def compile_filters(self) -> None:
        try:
            self.include_re = re.compile(self.include_pat) if self.include_pat else None
            self.exclude_re = re.compile(self.exclude_pat) if self.exclude_pat else None
            self.message = ""
        except re.error as e:
            self.message = f"Regex error: {e}"

    def save_filter_state(self) -> None:
        self.config["include_pat"] = self.include_pat
        self.config["exclude_pat"] = self.exclude_pat
        self.config["include_history"] = self.include_history
        self.config["exclude_history"] = self.exclude_history
        self.config["local_time"] = self.local_time
        save_config(self.config)

    def save_ui_state(self) -> None:
        self.config["include_pat"] = self.include_pat
        self.config["exclude_pat"] = self.exclude_pat
        self.config["include_history"] = self.include_history
        self.config["exclude_history"] = self.exclude_history
        self.config["local_time"] = self.local_time
        self.config["sessions"] = self.sessions
        save_config(self.config)

    def remember_include(self, value: str) -> None:
        self.include_history = add_history(self.include_history, value)
        self.save_filter_state()

    def remember_exclude(self, value: str) -> None:
        self.exclude_history = add_history(self.exclude_history, value)
        self.save_filter_state()

    def save_filters(self) -> None:
        self.save_filter_state()

    def find_session(self, name: str) -> Optional[dict]:
        name = name.strip()
        if not name:
            return None
        for s in self.sessions:
            if s.get("name") == name:
                return s
        return None

    def _session_target_ts(self, session: dict) -> Optional[str]:
        for key in ("goto_ts", "center_ts"):
            val = session.get(key)
            if isinstance(val, str) and val:
                try:
                    return normalize_ts(val)
                except ValueError:
                    continue
        return None

    def _session_default_ts(self) -> Optional[str]:
        first_ts = first_timestamp_str(self.path)
        if not first_ts:
            return None
        return f"{first_ts[:8]}T060000.000000Z"

    def apply_session(self, session: Optional[dict], goto: Optional[str], seek_debug: bool) -> None:
        if not session:
            if goto:
                self.center_off = find_ts_offset(self.path, goto, seek_debug)
            return

        include_pat = session.get("include_pat")
        exclude_pat = session.get("exclude_pat")
        if isinstance(include_pat, str):
            self.include_pat = include_pat
        if isinstance(exclude_pat, str):
            self.exclude_pat = exclude_pat
        if isinstance(session.get("window_secs"), (int, float)):
            self.window_secs = float(session["window_secs"])
        if isinstance(session.get("raw_secs"), (int, float)):
            self.raw_secs = float(session["raw_secs"])
        if isinstance(session.get("raw_mode"), bool):
            self.raw_mode = session["raw_mode"]
        if isinstance(session.get("local_time"), bool):
            self.local_time = session["local_time"]

        if goto:
            self.center_off = find_ts_offset(self.path, goto, seek_debug)
            return

        if session.get("path") == self.path and isinstance(session.get("center_off"), int):
            self.center_off = max(0, min(int(session["center_off"]), max(0, self.size)))
            return

        target_ts = self._session_target_ts(session) or self._session_default_ts()
        if target_ts:
            self.center_off = find_ts_offset(self.path, target_ts, seek_debug)
        else:
            self.center_off = 0

    def record_session(self, name: str) -> None:
        name = name.strip()
        if not name:
            return
        entry = {
            "name": name,
            "path": self.path,
            "saved_at": _dt.datetime.now(tz=_dt.timezone.utc).isoformat(),
            "center_off": self.center_off,
            "center_ts": self.center_ts(),
            "goto_ts": self.center_ts(),
            "include_pat": self.include_pat,
            "exclude_pat": self.exclude_pat,
            "window_secs": self.window_secs,
            "raw_secs": self.raw_secs,
            "raw_mode": self.raw_mode,
            "local_time": self.local_time,
        }
        sessions = [entry] + [s for s in self.sessions if s.get("name") != name]
        self.sessions = sessions[:MAX_SESSIONS]
        self.save_ui_state()

    def row_matches(self, text: str) -> bool:
        if self.include_re and not self.include_re.search(text):
            return False
        if self.exclude_re and self.exclude_re.search(text):
            return False
        return True

    def set_center(self, off: int, message: str) -> None:
        self.center_off = max(0, min(off, max(0, self.size)))
        self.cursor = 0
        self.rows = []
        self.message = message

    def center_ts(self) -> Optional[str]:
        with open(self.path, "rb") as f:
            _, bline = read_line_at(f, self.center_off, self.size)
        if not bline:
            return None
        return extract_ts_str(bline.decode("utf-8", "replace"))

    def top_offset(self) -> int:
        return 0

    def bottom_offset(self) -> int:
        if self.size <= 0:
            return 0
        with open(self.path, "rb") as f:
            return line_start_before(f, self.size, self.size)

    def follow_offset(self) -> int:
        return self.bottom_offset()

    def enter_follow(self) -> None:
        self.follow_mode = True
        self.center_off = self.follow_offset()
        self.cursor = 0
        self.message = "Follow mode"

    def stop_follow(self) -> None:
        if self.follow_mode:
            self.follow_mode = False
            self.message = "Follow stopped"

    def build_rows(self, height: int) -> None:
        self.compile_filters()
        ts = self.center_ts()
        max_body = max(1, height)
        # Keep the selected timestamp visible even in very dense logs.
        # We collect rows on both sides of center_off rather than scanning
        # from the start of the whole time window, which can otherwise fill
        # the display before reaching the target timestamp.
        max_rows = max(300, min(2000, max_body * 40))
        before_target = max_rows // 2
        after_target = max_rows - before_target

        def decode_line(bline: bytes) -> str:
            return bline.decode("utf-8", "replace").rstrip("\n")

        def add_if_ok(dst: list[Row], off: int, bline: bytes, lo_ts: str | None, hi_ts: str | None) -> bool:
            text = decode_line(bline)
            lts = extract_ts_str(text)
            if lts is None:
                # Non timestamped continuation lines are allowed in raw mode,
                # otherwise only if they match filters.
                if self.raw_mode or self.row_matches(text):
                    dst.append(Row(off, text))
                return True
            if lo_ts is not None and lts < lo_ts:
                return False
            if hi_ts is not None and lts > hi_ts:
                return False
            if self.raw_mode or self.row_matches(text):
                dst.append(Row(off, text))
            return True

        rows_before: list[Row] = []
        rows_after: list[Row] = []
        center_row: list[Row] = []
        truncated_before = False
        truncated_after = False

        if ts:
            cdt = ts_to_dt(ts)
            secs = self.raw_secs if self.raw_mode else self.window_secs
            lo_ts = dt_to_ts(cdt - _dt.timedelta(seconds=secs))
            hi_ts = dt_to_ts(cdt + _dt.timedelta(seconds=secs))

            with open(self.path, "rb") as f:
                # Selected/center line first.
                _, bcenter = read_line_at(f, self.center_off, self.size)
                if bcenter:
                    add_if_ok(center_row, self.center_off, bcenter, lo_ts, hi_ts)

                # Walk backwards line by line until time/window or row cap.
                pos = self.center_off
                while pos > 0 and len(rows_before) < before_target:
                    prev = line_start_before(f, pos - 1, self.size)
                    if prev == pos:
                        break
                    _, bline = read_line_at(f, prev, self.size)
                    if not bline:
                        break
                    keep_scanning = add_if_ok(rows_before, prev, bline, lo_ts, hi_ts)
                    if not keep_scanning:
                        break
                    if prev == 0:
                        break
                    pos = prev
                # If we stopped due to cap, record that there may be more rows.
                if pos > 0 and len(rows_before) >= before_target:
                    truncated_before = True

                # Walk forwards from the line after center.
                f.seek(self.center_off)
                f.readline()
                while len(rows_after) < after_target:
                    off = f.tell()
                    bline = f.readline()
                    if not bline:
                        break
                    text = decode_line(bline)
                    lts = extract_ts_str(text)
                    if lts is not None and lts > hi_ts:
                        break
                    if lts is not None and lts < lo_ts:
                        continue
                    if self.raw_mode or self.row_matches(text):
                        rows_after.append(Row(off, text))
                if len(rows_after) >= after_target:
                    truncated_after = True

            rows_before.reverse()
            rows: list[Row] = []
            if truncated_before:
                rows.append(Row(-1, "--- earlier rows omitted; narrow the time window/filter or page/search backward ---"))
            rows.extend(rows_before)
            if center_row:
                rows.extend(center_row)
            rows.extend(rows_after)
            if truncated_after:
                rows.append(Row(-1, "--- later rows omitted; narrow the time window/filter or page/search forward ---"))
        else:
            # Non-timestamp fallback: show nearby lines.
            rows = []
            with open(self.path, "rb") as f:
                start = line_start_before(f, self.center_off, self.size)
                f.seek(start)
                while len(rows) < max_rows:
                    off = f.tell()
                    bline = f.readline()
                    if not bline:
                        break
                    text = decode_line(bline)
                    if self.raw_mode or self.row_matches(text):
                        rows.append(Row(off, text))

        old_off = self.rows[self.cursor].off if self.rows and 0 <= self.cursor < len(self.rows) else self.center_off
        self.rows = rows
        # Put cursor on current center if visible; otherwise closest offset.
        self.cursor = 0
        if self.rows:
            best_i, best_d = 0, 10**30
            for i, r in enumerate(self.rows):
                if r.off < 0:
                    continue
                d = abs(r.off - old_off)
                if d < best_d:
                    best_i, best_d = i, d
            self.cursor = best_i

    def selected_offset(self) -> int:
        if self.rows and 0 <= self.cursor < len(self.rows) and self.rows[self.cursor].off >= 0:
            return self.rows[self.cursor].off
        return self.center_off

    def move_cursor(self, delta: int) -> None:
        if not self.rows:
            return
        self.cursor = max(0, min(len(self.rows) - 1, self.cursor + delta))
        if self.rows[self.cursor].off >= 0:
            self.center_off = self.rows[self.cursor].off

    def jump_time(self, ts: str) -> None:
        jump_ts = parse_jump_ts(ts, assume_local=self.local_time)
        self.set_center(find_ts_offset(self.path, jump_ts), "")
        if self.local_time and not ts.strip().endswith("Z"):
            self.message = f"Jumped to local {ts.strip()}"
        else:
            self.message = f"Jumped to {normalize_ts(jump_ts)}"

    def jump_top(self) -> None:
        self.set_center(self.top_offset(), "Top of file")

    def jump_bottom(self) -> None:
        self.set_center(self.bottom_offset(), "Bottom of file")

    def tick_follow(self) -> None:
        if self.follow_mode:
            self.set_center(self.follow_offset(), "Follow mode")

    def toggle_local_time(self) -> None:
        self.local_time = not self.local_time
        self.message = "Local time on" if self.local_time else "Local time off"
        self.save_filter_state()

    def display_ts(self) -> str:
        ts = self.center_ts()
        if not ts:
            return "no-ts"
        return ts_to_local_str(ts) if self.local_time else ts

    def format_row_text(self, text: str) -> str:
        if not self.local_time:
            return text
        ts = extract_ts_str(text)
        if ts is None:
            return text
        prefix, sep, rest = text.partition("\t")
        return f"{ts_to_local_str(ts)}{sep}{rest}" if sep else ts_to_local_str(ts)

    def search_forward(self, pat: str) -> bool:
        rx = re.compile(pat)
        start = self.selected_offset()
        with open(self.path, "rb") as f:
            f.seek(start)
            f.readline()  # skip current line
            while True:
                off = f.tell()
                bline = f.readline()
                if not bline:
                    return False
                text = bline.decode("utf-8", "replace").rstrip("\n")
                if rx.search(text):
                    self.set_center(off, f"Found forward: {pat}")
                    return True

    def search_backward(self, pat: str) -> bool:
        rx = re.compile(pat)
        pos = self.selected_offset()
        chunk = 1024 * 1024
        carry = b""
        with open(self.path, "rb") as f:
            end = pos
            while end > 0:
                start = max(0, end - chunk)
                f.seek(start)
                data = f.read(end - start) + carry
                lines = data.splitlines(True)
                # First line may be partial unless start == 0.
                if start != 0 and lines:
                    carry = lines[0]
                    lines = lines[1:]
                else:
                    carry = b""
                offsets = []
                cur = start + (len(carry) if start != 0 else 0)
                for ln in lines:
                    offsets.append(cur)
                    cur += len(ln)
                for off, ln in reversed(list(zip(offsets, lines))):
                    text = ln.decode("utf-8", "replace").rstrip("\n")
                    if rx.search(text):
                        self.set_center(off, f"Found backward: {pat}")
                        return True
                end = start
        return False


def prompt(stdscr, label: str, initial: str = "", history: Optional[list[str]] = None) -> Optional[str]:
    """Read an editable prompt line.

    Up/Down cycles through supplied history.  The current value is used as the
    editable starting point, so include/exclude filters can be extended by
    appending `|something`.
    """
    curses.curs_set(1)
    h, w = stdscr.getmaxyx()
    s = initial
    pos = len(s)
    hist = history or []
    hist_index: Optional[int] = None
    draft = initial
    hint = "  Enter=accept Esc=cancel Ctrl-U=clear"
    if hist:
        hint += " Up/Down=history"
    while True:
        stdscr.move(h - 1, 0)
        stdscr.clrtoeol()
        display = f"{label}: {s}{hint}"
        base_len = len(f"{label}: ")
        if len(display) > w - 1:
            # Keep cursor-end visible while preserving the actual editable text.
            visible_text_width = max(1, w - 1 - base_len)
            left = max(0, pos - visible_text_width + 1)
            shown_s = s[left : left + visible_text_width]
            display = f"{label}: {shown_s}"
            cursor_x = base_len + (pos - left)
        else:
            cursor_x = base_len + pos
        stdscr.addstr(h - 1, 0, display[: w - 1], curses.A_REVERSE)
        stdscr.move(h - 1, min(w - 1, cursor_x))
        ch = stdscr.getch()
        if ch in (10, 13):
            curses.curs_set(0)
            return s
        if ch in (27,):
            curses.curs_set(0)
            return None
        if ch == curses.KEY_UP and hist:
            if hist_index is None:
                draft = s
                hist_index = 0
            else:
                hist_index = min(len(hist) - 1, hist_index + 1)
            s = hist[hist_index]
            pos = len(s)
        elif ch == curses.KEY_DOWN and hist:
            if hist_index is None:
                continue
            if hist_index <= 0:
                hist_index = None
                s = draft
            else:
                hist_index -= 1
                s = hist[hist_index]
            pos = len(s)
        elif ch in (curses.KEY_BACKSPACE, 127, 8):
            if pos > 0:
                s = s[: pos - 1] + s[pos:]
                pos -= 1
                hist_index = None
        elif ch == curses.KEY_LEFT:
            pos = max(0, pos - 1)
        elif ch == curses.KEY_RIGHT:
            pos = min(len(s), pos + 1)
        elif ch == curses.KEY_HOME:
            pos = 0
        elif ch == curses.KEY_END:
            pos = len(s)
        elif ch == 21:  # Ctrl-U
            s = ""
            pos = 0
            hist_index = None
        elif 0 <= ch < 256 and chr(ch).isprintable():
            s = s[:pos] + chr(ch) + s[pos:]
            pos += 1
            hist_index = None


def session_label(session: dict) -> str:
    name = str(session.get("name", "")).strip() or "(unnamed)"
    path = os.path.basename(str(session.get("path", "")) or "")
    ts = str(session.get("center_ts") or session.get("goto_ts") or "").strip()
    when = ts[:19] if ts else "no-ts"
    parts = [name]
    if path:
        parts.append(f"[{path}]")
    parts.append(when)
    return " ".join(parts)


def pick_session(
    stdscr,
    title: str,
    sessions: list[dict],
    initial_name: str = "",
) -> Optional[str]:
    curses.curs_set(0)
    h, w = stdscr.getmaxyx()
    items: list[tuple[str, str]] = [("", "New session...")]
    seen: set[str] = set()
    for s in sessions:
        name = str(s.get("name", "")).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        items.append((name, session_label(s)))

    if not items:
        return None

    index = 0
    for i, (name, _) in enumerate(items):
        if name and name == initial_name:
            index = i
            break

    top = 0
    while True:
        stdscr.erase()
        header = f"{title}  Enter=select  n=new  Esc=cancel"
        stdscr.addstr(0, 0, header[: w - 1], curses.A_REVERSE)
        if len(items) > 1:
            stdscr.addstr(1, 0, "Use Up/Down or j/k to choose a session."[: w - 1], curses.A_DIM)
        else:
            stdscr.addstr(1, 0, "No saved sessions yet; create a new one."[: w - 1], curses.A_DIM)

        visible = max(1, h - 3)
        if index < top:
            top = index
        elif index >= top + visible:
            top = index - visible + 1

        for y, idx in enumerate(range(top, min(len(items), top + visible)), start=2):
            name, label = items[idx]
            prefix = "> " if idx == index else "  "
            text = prefix + label
            attr = curses.A_REVERSE if idx == index else curses.A_NORMAL
            stdscr.addstr(y, 0, text[: w - 1], attr)

        stdscr.refresh()
        ch = stdscr.getch()
        if ch in (27, ord("q")):
            return None
        if ch in (curses.KEY_UP, ord("k")):
            index = max(0, index - 1)
        elif ch in (curses.KEY_DOWN, ord("j")):
            index = min(len(items) - 1, index + 1)
        elif ch == curses.KEY_PPAGE:
            index = max(0, index - visible)
        elif ch == curses.KEY_NPAGE:
            index = min(len(items) - 1, index + visible)
        elif ch in (10, 13):
            return items[index][0]
        elif ch in (ord("n"), ord("N")):
            return ""


def draw(stdscr, v: Viewer) -> None:
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    v.build_rows(h - 3)
    mode = "RAW" if v.raw_mode else "FILTER"
    ts = v.display_ts()
    follow = " FOLLOW" if v.follow_mode else ""
    local = " LOCAL" if v.local_time else ""
    session = f" session={v.session_name!r}" if v.session_name else ""
    header = f"{mode}{follow}{local} {os.path.basename(v.path)} @ {ts}{session}  win={v.window_secs:g}s raw={v.raw_secs:g}s  i={v.include_pat!r} x={v.exclude_pat!r}"
    stdscr.addstr(0, 0, header[: w - 1], curses.A_REVERSE)
    help_line = "g top  G bottom  j time  F follow  t local  s save session  / ? search  n/N repeat  i include  x exclude  c clear  r raw  w/R seconds  ↑/↓ move; prompt ↑/↓ history  q quit"
    stdscr.addstr(1, 0, help_line[: w - 1], curses.A_DIM)

    max_body = h - 3
    if v.rows:
        # Scroll so cursor is visible near middle.
        start = max(0, min(v.cursor - max_body // 2, max(0, len(v.rows) - max_body)))
        for y, idx in enumerate(range(start, min(len(v.rows), start + max_body)), start=2):
            row = v.rows[idx]
            attr = curses.A_REVERSE if idx == v.cursor else curses.A_NORMAL
            prefix = "> " if idx == v.cursor else "  "
            stdscr.addstr(y, 0, (prefix + v.format_row_text(row.text))[: w - 1], attr)
    else:
        stdscr.addstr(2, 0, "No rows in current window/filter"[: w - 1])

    if v.message:
        stdscr.addstr(h - 1, 0, v.message[: w - 1], curses.A_REVERSE)
    stdscr.refresh()


def main_curses(stdscr, v: Viewer) -> None:
    curses.curs_set(0)
    stdscr.keypad(True)

    def load_session(name: str) -> None:
        session = v.find_session(name)
        v.session_name = name
        if session:
            v.apply_session(session, None, False)
        else:
            default_ts = v._session_default_ts()
            if default_ts:
                v.center_off = find_ts_offset(v.path, default_ts)
        v.record_session(name)

    def save_session(name: str) -> None:
        v.session_name = name
        v.record_session(name)

    if not v.session_name:
        choice = pick_session(stdscr, "choose session", v.sessions)
        if choice is not None:
            if choice:
                load_session(choice)
            else:
                session_name = prompt(stdscr, "session name", "")
                if session_name is not None:
                    name = session_name.strip()
                    if name:
                        load_session(name)
    elif v.session_name:
        load_session(v.session_name)
    while True:
        v.tick_follow()
        stdscr.timeout(500 if v.follow_mode else -1)
        draw(stdscr, v)
        ch = stdscr.getch()
        if ch == -1:
            continue
        if ch in (ord("q"), 3):
            break
        if v.follow_mode and ch not in (ord("F"), ord("q"), 3):
            v.stop_follow()
        if ch == curses.KEY_UP:
            v.move_cursor(-1)
        elif ch == curses.KEY_DOWN:
            v.move_cursor(1)
        elif ch == curses.KEY_PPAGE:
            v.move_cursor(-20)
        elif ch == curses.KEY_NPAGE:
            v.move_cursor(20)
        elif ch == ord("g"):
            v.jump_top()
        elif ch == ord("G"):
            v.jump_bottom()
        elif ch == ord("j"):
            initial = v.display_ts() if v.local_time else (v.center_ts() or "")
            label = "goto local time" if v.local_time else "goto timestamp"
            s = prompt(stdscr, label, initial)
            if s:
                try:
                    v.jump_time(s)
                except Exception as e:
                    v.message = str(e)
        elif ch == ord("i"):
            s = prompt(stdscr, "include regex", v.include_pat, v.include_history)
            if s is not None:
                v.include_pat = s
                v.compile_filters()
                if s.strip():
                    v.remember_include(s)
                else:
                    v.save_filters()
        elif ch == ord("x"):
            s = prompt(stdscr, "exclude regex", v.exclude_pat, v.exclude_history)
            if s is not None:
                v.exclude_pat = s
                v.compile_filters()
                if s.strip():
                    v.remember_exclude(s)
                else:
                    v.save_filters()
        elif ch == ord("c"):
            v.include_pat = ""
            v.exclude_pat = ""
            v.compile_filters()
            v.save_filters()
        elif ch == ord("s"):
            choice = pick_session(stdscr, "save session", v.sessions, v.session_name)
            if choice is not None:
                if choice:
                    save_session(choice)
                    v.message = f"Saved session: {choice}"
                else:
                    s = prompt(stdscr, "save session name", v.session_name)
                    if s is not None:
                        name = s.strip()
                        if name:
                            save_session(name)
                            v.message = f"Saved session: {name}"
        elif ch == ord("r"):
            v.raw_mode = not v.raw_mode
        elif ch == ord("t"):
            v.toggle_local_time()
        elif ch == ord("w"):
            s = prompt(stdscr, "filtered window seconds", str(v.window_secs))
            if s:
                try:
                    v.window_secs = float(s)
                except ValueError:
                    v.message = "bad number"
        elif ch == ord("R"):
            s = prompt(stdscr, "raw context seconds", str(v.raw_secs))
            if s:
                try:
                    v.raw_secs = float(s)
                except ValueError:
                    v.message = "bad number"
        elif ch == ord("F"):
            if v.follow_mode:
                v.stop_follow()
            else:
                v.enter_follow()
        elif ch == ord("/"):
            s = prompt(stdscr, "search forward regex", v.last_search)
            if s:
                try:
                    v.last_search = s
                    v.last_search_forward = True
                    if not v.search_forward(s):
                        v.message = f"Not found: {s}"
                except re.error as e:
                    v.message = f"Regex error: {e}"
        elif ch == ord("?"):
            s = prompt(stdscr, "search backward regex", v.last_search)
            if s:
                try:
                    v.last_search = s
                    v.last_search_forward = False
                    if not v.search_backward(s):
                        v.message = f"Not found backward: {s}"
                except re.error as e:
                    v.message = f"Regex error: {e}"
        elif ch == ord("n") and v.last_search:
            try:
                ok = v.search_forward(v.last_search) if v.last_search_forward else v.search_backward(v.last_search)
                if not ok:
                    v.message = f"Not found: {v.last_search}"
            except re.error as e:
                v.message = f"Regex error: {e}"
        elif ch == ord("N") and v.last_search:
            try:
                ok = v.search_backward(v.last_search) if v.last_search_forward else v.search_forward(v.last_search)
                if not ok:
                    v.message = f"Not found: {v.last_search}"
            except re.error as e:
                v.message = f"Regex error: {e}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Fast seek-based Clockwork log viewer")
    ap.add_argument("logfile")
    ap.add_argument("--goto", help="timestamp to jump to, e.g. 20260514T033456.507596Z")
    ap.add_argument("--session", help="session name to load or create at startup")
    ap.add_argument("--seek-debug", action="store_true", help="print seek diagnostics before opening curses")
    args = ap.parse_args()
    if not os.path.isfile(args.logfile):
        print(f"No such file: {args.logfile}", file=sys.stderr)
        return 2
    v = Viewer(args.logfile, args.goto, args.seek_debug, args.session)
    curses.wrapper(main_curses, v)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
