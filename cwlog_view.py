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
from typing import BinaryIO, Iterable, Optional

TS_RE = re.compile(r"^(\d{8}T\d{6})(?:\.(\d{1,6}))?Z?")


CONFIG_PATH = os.path.expanduser("~/.cwlog")
MAX_HISTORY = 80


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
    def __init__(self, path: str, goto: Optional[str] = None, seek_debug: bool = False):
        self.path = path
        self.size = os.path.getsize(path)
        self.center_off = 0
        if goto:
            self.center_off = find_ts_offset(path, goto, seek_debug)
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
        self.rows: list[Row] = []
        self.cursor = 0
        self.message = ""
        self.last_search = ""
        self.last_search_forward = True

    def compile_filters(self) -> None:
        try:
            self.include_re = re.compile(self.include_pat) if self.include_pat else None
            self.exclude_re = re.compile(self.exclude_pat) if self.exclude_pat else None
            self.message = ""
        except re.error as e:
            self.message = f"Regex error: {e}"

    def save_filter_history(self) -> None:
        self.config["include_history"] = self.include_history
        self.config["exclude_history"] = self.exclude_history
        save_config(self.config)

    def remember_include(self, value: str) -> None:
        self.include_history = add_history(self.include_history, value)
        self.save_filter_history()

    def remember_exclude(self, value: str) -> None:
        self.exclude_history = add_history(self.exclude_history, value)
        self.save_filter_history()

    def row_matches(self, text: str) -> bool:
        if self.include_re and not self.include_re.search(text):
            return False
        if self.exclude_re and self.exclude_re.search(text):
            return False
        return True

    def center_ts(self) -> Optional[str]:
        with open(self.path, "rb") as f:
            _, bline = read_line_at(f, self.center_off, self.size)
        if not bline:
            return None
        return extract_ts_str(bline.decode("utf-8", "replace"))

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
        self.center_off = find_ts_offset(self.path, ts)
        self.cursor = 0
        self.message = f"Jumped to {normalize_ts(ts)}"

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
                    self.center_off = off
                    self.message = f"Found forward: {pat}"
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
                        self.center_off = off
                        self.message = f"Found backward: {pat}"
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


def draw(stdscr, v: Viewer) -> None:
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    v.build_rows(h - 3)
    mode = "RAW" if v.raw_mode else "FILTER"
    ts = v.center_ts() or "no-ts"
    header = f"{mode} {os.path.basename(v.path)} @ {ts}  win={v.window_secs:g}s raw={v.raw_secs:g}s  i={v.include_pat!r} x={v.exclude_pat!r}"
    stdscr.addstr(0, 0, header[: w - 1], curses.A_REVERSE)
    help_line = "g goto  / ? search  n/N repeat  i include  x exclude  c clear  r raw  w/R seconds  ↑/↓ move; prompt ↑/↓ history  q quit"
    stdscr.addstr(1, 0, help_line[: w - 1], curses.A_DIM)

    max_body = h - 3
    if v.rows:
        # Scroll so cursor is visible near middle.
        start = max(0, min(v.cursor - max_body // 2, max(0, len(v.rows) - max_body)))
        for y, idx in enumerate(range(start, min(len(v.rows), start + max_body)), start=2):
            row = v.rows[idx]
            attr = curses.A_REVERSE if idx == v.cursor else curses.A_NORMAL
            prefix = "> " if idx == v.cursor else "  "
            stdscr.addstr(y, 0, (prefix + row.text)[: w - 1], attr)
    else:
        stdscr.addstr(2, 0, "No rows in current window/filter"[: w - 1])

    if v.message:
        stdscr.addstr(h - 1, 0, v.message[: w - 1], curses.A_REVERSE)
    stdscr.refresh()


def main_curses(stdscr, v: Viewer) -> None:
    curses.curs_set(0)
    stdscr.keypad(True)
    while True:
        draw(stdscr, v)
        ch = stdscr.getch()
        if ch in (ord("q"), 3):
            break
        elif ch == curses.KEY_UP:
            v.move_cursor(-1)
        elif ch == curses.KEY_DOWN:
            v.move_cursor(1)
        elif ch == curses.KEY_PPAGE:
            v.move_cursor(-20)
        elif ch == curses.KEY_NPAGE:
            v.move_cursor(20)
        elif ch == ord("g"):
            s = prompt(stdscr, "goto timestamp", v.center_ts() or "")
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
        elif ch == ord("x"):
            s = prompt(stdscr, "exclude regex", v.exclude_pat, v.exclude_history)
            if s is not None:
                v.exclude_pat = s
                v.compile_filters()
                if s.strip():
                    v.remember_exclude(s)
        elif ch == ord("c"):
            v.include_pat = ""
            v.exclude_pat = ""
            v.compile_filters()
        elif ch == ord("r"):
            v.raw_mode = not v.raw_mode
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
    ap.add_argument("--seek-debug", action="store_true", help="print seek diagnostics before opening curses")
    args = ap.parse_args()
    if not os.path.isfile(args.logfile):
        print(f"No such file: {args.logfile}", file=sys.stderr)
        return 2
    v = Viewer(args.logfile, args.goto, args.seek_debug)
    curses.wrapper(main_curses, v)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
