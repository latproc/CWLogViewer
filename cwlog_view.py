#!/usr/bin/env python3
"""
cwlog_view_fast.py - fast cursor-style viewer for large Clockwork timestamp logs.

Designed for lines starting with timestamps like:
  20260514T033449.699048Z\tMachine\tState\t...

Why this version exists:
  - avoids datetime.strptime and regex for every line while indexing
  - avoids reading/filtering the whole file on every screen redraw
  - keeps only byte offsets + integer timestamps in memory

Usage:
  ./cwlog_view_fast.py ../sampling/log-20260514.txt
  ./cwlog_view_fast.py ../sampling/log-20260514.txt --goto 20260514T033456.507

Keys:
  q              quit
  h              help
  ↑/↓ or k/j      move cursor
  PgUp/PgDn      page
  g              jump to timestamp, e.g. 20260514T033456 or 20260514T033456.507
  /              search forward regex
  ?              search backward regex
  n/N            next/previous search result
  i              edit include regex; old value is pre-filled for appending with |thing
  x              edit exclude regex; old value is pre-filled for appending with |thing
  c              clear include/exclude filters
  w              set filtered time window seconds around cursor timestamp
  r              toggle raw context mode around selected event
  R              set raw context seconds
"""

from __future__ import annotations

import argparse
import bisect
import curses
import os
import re
import sys
import time
from array import array
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional, Pattern

US_PER_SEC = 1_000_000
NO_TS = -1


def _digits_to_int(buf: bytes, start: int, end: int) -> Optional[int]:
    v = 0
    if end > len(buf):
        return None
    for i in range(start, end):
        c = buf[i]
        if c < 48 or c > 57:
            return None
        v = v * 10 + (c - 48)
    return v


def _days_from_civil(y: int, m: int, d: int) -> int:
    """Days since 1970-01-01. Howard Hinnant civil calendar algorithm."""
    y -= 1 if m <= 2 else 0
    era = (y if y >= 0 else y - 399) // 400
    yoe = y - era * 400
    mp = m + (-3 if m > 2 else 9)
    doy = (153 * mp + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def parse_ts_us_bytes(line: bytes) -> int:
    """Parse YYYYMMDDTHHMMSS[.ffffff]Z prefix to epoch microseconds. Return -1 if no timestamp."""
    # Minimum: 20260514T033456Z = 16 bytes including Z, or . before Z.
    if len(line) < 15 or line[8:9] != b"T":
        return NO_TS
    y = _digits_to_int(line, 0, 4)
    mo = _digits_to_int(line, 4, 6)
    d = _digits_to_int(line, 6, 8)
    hh = _digits_to_int(line, 9, 11)
    mm = _digits_to_int(line, 11, 13)
    ss = _digits_to_int(line, 13, 15)
    if None in (y, mo, d, hh, mm, ss):
        return NO_TS
    assert y is not None and mo is not None and d is not None and hh is not None and mm is not None and ss is not None
    if not (1 <= mo <= 12 and 1 <= d <= 31 and 0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 60):
        return NO_TS
    pos = 15
    micros = 0
    if pos < len(line) and line[pos:pos + 1] == b".":
        pos += 1
        digits = 0
        while pos < len(line) and 48 <= line[pos] <= 57 and digits < 6:
            micros = micros * 10 + (line[pos] - 48)
            pos += 1
            digits += 1
        while pos < len(line) and 48 <= line[pos] <= 57:
            # Ignore precision beyond microseconds.
            pos += 1
        while digits < 6:
            micros *= 10
            digits += 1
    # Accept Z, tab, space, or end after timestamp. Logs use Z\t.
    try:
        days = _days_from_civil(y, mo, d)
    except Exception:
        return NO_TS
    return (((days * 24 + hh) * 60 + mm) * 60 + ss) * US_PER_SEC + micros


def parse_ts_us_text(text: str) -> int:
    return parse_ts_us_bytes(text.strip().encode("ascii", errors="ignore"))


def fmt_ts_us(ts_us: int) -> str:
    if ts_us == NO_TS:
        return "no-ts"
    # Only used for display; one datetime-like conversion per draw is fine.
    from datetime import datetime, timezone
    sec, us = divmod(ts_us, US_PER_SEC)
    return datetime.fromtimestamp(sec, timezone.utc).strftime("%Y%m%dT%H%M%S") + f".{us:06d}Z"


@dataclass
class Index:
    offsets: array      # unsigned long long byte offsets, one per line
    ts_by_line: array   # signed long long timestamp microseconds, -1 if missing
    ts_values: array    # sorted timestamp microseconds for timestamped lines
    ts_lines: array     # line index for each ts_values entry
    size_bytes: int

    @property
    def line_count(self) -> int:
        return len(self.offsets)


def build_index(path: str, progress: bool = True) -> Index:
    offsets = array("Q")
    ts_by_line = array("q")
    ts_values = array("q")
    ts_lines = array("Q")

    size = os.path.getsize(path)
    last_report = time.monotonic()
    start_time = last_report

    with open(path, "rb", buffering=1024 * 1024) as f:
        while True:
            off = f.tell()
            line = f.readline()
            if not line:
                break
            idx = len(offsets)
            offsets.append(off)
            ts = parse_ts_us_bytes(line[:40])
            ts_by_line.append(ts)
            if ts != NO_TS:
                # Clockwork logs are normally monotonic. If not, we sort after indexing.
                ts_values.append(ts)
                ts_lines.append(idx)

            now = time.monotonic()
            if progress and now - last_report >= 1.0:
                mb = off / (1024 * 1024)
                pct = (off / size * 100.0) if size else 0.0
                elapsed = max(0.001, now - start_time)
                rate = mb / elapsed
                print(f"\rIndexing: {pct:5.1f}% {mb:,.1f} MiB {len(offsets):,} lines {rate:,.1f} MiB/s", end="", file=sys.stderr, flush=True)
                last_report = now

    if progress:
        mb = size / (1024 * 1024)
        elapsed = max(0.001, time.monotonic() - start_time)
        print(f"\rIndexed: 100.0% {mb:,.1f} MiB {len(offsets):,} lines {mb/elapsed:,.1f} MiB/s       ", file=sys.stderr)

    # If timestamp order is not monotonic, sort timestamp lookup arrays.
    if len(ts_values) > 1:
        monotonic = all(ts_values[i] <= ts_values[i + 1] for i in range(len(ts_values) - 1))
        if not monotonic:
            pairs = sorted(zip(ts_values, ts_lines), key=lambda p: p[0])
            ts_values = array("q", (p[0] for p in pairs))
            ts_lines = array("Q", (p[1] for p in pairs))

    return Index(offsets, ts_by_line, ts_values, ts_lines, size)


class LogFile:
    def __init__(self, path: str, index: Index):
        self.path = path
        self.index = index
        self.f = open(path, "rb", buffering=1024 * 1024)
        self.cache: OrderedDict[int, str] = OrderedDict()
        self.cache_limit = 4000

    def close(self) -> None:
        self.f.close()

    def line(self, idx: int) -> str:
        if idx < 0 or idx >= self.index.line_count:
            return ""
        cached = self.cache.get(idx)
        if cached is not None:
            self.cache.move_to_end(idx)
            return cached
        self.f.seek(self.index.offsets[idx])
        text = self.f.readline().decode("utf-8", errors="replace").rstrip("\n")
        self.cache[idx] = text
        if len(self.cache) > self.cache_limit:
            self.cache.popitem(last=False)
        return text

    def find_nearest_ts_line(self, ts_us: int) -> int:
        vals = self.index.ts_values
        lines = self.index.ts_lines
        if not vals:
            return 0
        pos = bisect.bisect_left(vals, ts_us)
        if pos <= 0:
            return int(lines[0])
        if pos >= len(vals):
            return int(lines[-1])
        before = vals[pos - 1]
        after = vals[pos]
        return int(lines[pos - 1] if abs(before - ts_us) <= abs(after - ts_us) else lines[pos])

    def lines_in_time_range(self, lo_us: int, hi_us: int) -> range:
        vals = self.index.ts_values
        lines = self.index.ts_lines
        if not vals:
            return range(0)
        start = bisect.bisect_left(vals, lo_us)
        end = bisect.bisect_right(vals, hi_us)
        # For normal monotonic logs, line indices are also monotonic. Return range of line numbers,
        # not just timestamped-line entries, so raw mode includes non-timestamp lines too if any.
        if start >= end:
            return range(0)
        lo_line = int(lines[start])
        hi_line = int(lines[end - 1]) + 1
        return range(max(0, lo_line), min(self.index.line_count, hi_line))


class Viewer:
    def __init__(self, stdscr, log: LogFile):
        self.stdscr = stdscr
        self.log = log
        self.cursor_line = 0
        self.include_pat: Optional[Pattern[str]] = None
        self.exclude_pat: Optional[Pattern[str]] = None
        self.search_pat: Optional[Pattern[str]] = None
        self.window_seconds = 60.0
        self.raw_mode = False
        self.raw_seconds = 5.0
        self.status_msg = ""
        self._view_cache_key = None
        self._view_cache: list[int] = []

    def prompt(self, label: str, default: str = "") -> Optional[str]:
        """Small editable prompt with the existing value pre-filled.

        Curses getstr() cannot pre-fill text, which made include/exclude filters
        painful to extend. This line editor starts with default text and places the
        cursor at the end so pressing i/x lets you append, for example:
            |A_CutterDeck_ModeDisplay
        Keys: Enter accept, Esc cancel, Backspace/Delete edit, Ctrl-U clear,
        Left/Right/Home/End move.
        """
        self.stdscr.nodelay(False)
        curses.curs_set(1)
        h, w = self.stdscr.getmaxyx()
        prefix = f"{label}: "
        max_edit = max(1, w - len(prefix) - 1)
        buf = list(default)
        pos = len(buf)

        def redraw_prompt() -> None:
            self.stdscr.move(h - 1, 0)
            self.stdscr.clrtoeol()
            self.stdscr.addnstr(h - 1, 0, prefix, max(0, w - 1), curses.A_REVERSE)
            # Keep cursor visible by horizontally scrolling long filters.
            left = 0
            if pos >= max_edit:
                left = pos - max_edit + 1
            visible = ''.join(buf[left:left + max_edit])
            self.stdscr.addnstr(h - 1, len(prefix), visible, max_edit, curses.A_REVERSE)
            cursor_x = len(prefix) + (pos - left)
            self.stdscr.move(h - 1, min(w - 1, cursor_x))
            self.stdscr.refresh()

        try:
            while True:
                redraw_prompt()
                ch = self.stdscr.getch()
                if ch in (10, 13, curses.KEY_ENTER):
                    return ''.join(buf).strip()
                if ch in (27,):
                    return None
                if ch in (curses.KEY_LEFT,):
                    pos = max(0, pos - 1)
                elif ch in (curses.KEY_RIGHT,):
                    pos = min(len(buf), pos + 1)
                elif ch in (curses.KEY_HOME, 1):  # Home / Ctrl-A
                    pos = 0
                elif ch in (curses.KEY_END, 5):   # End / Ctrl-E
                    pos = len(buf)
                elif ch in (21,):  # Ctrl-U
                    buf.clear()
                    pos = 0
                elif ch in (curses.KEY_BACKSPACE, 127, 8):
                    if pos > 0:
                        del buf[pos - 1]
                        pos -= 1
                elif ch in (curses.KEY_DC,):
                    if pos < len(buf):
                        del buf[pos]
                elif 32 <= ch <= 126:
                    buf.insert(pos, chr(ch))
                    pos += 1
        finally:
            curses.curs_set(0)

    def set_status(self, msg: str) -> None:
        self.status_msg = msg

    def compile_regex(self, text: str) -> Optional[Pattern[str]]:
        if not text:
            return None
        try:
            return re.compile(text)
        except re.error as e:
            self.set_status(f"regex error: {e}")
            return None

    def line_matches(self, idx: int) -> bool:
        text = self.log.line(idx)
        if self.include_pat and not self.include_pat.search(text):
            return False
        if self.exclude_pat and self.exclude_pat.search(text):
            return False
        return True

    def current_ts_us(self) -> int:
        if 0 <= self.cursor_line < self.log.index.line_count:
            return int(self.log.index.ts_by_line[self.cursor_line])
        return NO_TS

    def compute_view_indices(self) -> list[int]:
        n = self.log.index.line_count
        if n == 0:
            return []
        ts = self.current_ts_us()
        mode = "raw" if self.raw_mode else "filter"
        seconds = self.raw_seconds if self.raw_mode else self.window_seconds
        key = (self.cursor_line, ts, mode, seconds, self.include_pat.pattern if self.include_pat else None, self.exclude_pat.pattern if self.exclude_pat else None)
        if key == self._view_cache_key:
            return self._view_cache

        if ts == NO_TS:
            around = 1000 if not self.raw_mode else 200
            line_range = range(max(0, self.cursor_line - around), min(n, self.cursor_line + around + 1))
        else:
            delta = int(seconds * US_PER_SEC)
            line_range = self.log.lines_in_time_range(ts - delta, ts + delta)

        if self.raw_mode:
            indices = list(line_range)
        else:
            indices = [i for i in line_range if self.line_matches(i)]
            if not indices:
                indices = [self.cursor_line]

        self._view_cache_key = key
        self._view_cache = indices
        return indices

    def invalidate_view_cache(self) -> None:
        self._view_cache_key = None

    def move_in_view(self, delta: int) -> None:
        indices = self.compute_view_indices()
        if not indices:
            return
        try:
            pos = indices.index(self.cursor_line)
        except ValueError:
            pos = bisect.bisect_left(indices, self.cursor_line)
            if pos >= len(indices):
                pos = len(indices) - 1
        pos = max(0, min(len(indices) - 1, pos + delta))
        self.cursor_line = indices[pos]
        self.invalidate_view_cache()

    def page_move(self, delta_pages: int) -> None:
        h, _ = self.stdscr.getmaxyx()
        self.move_in_view(delta_pages * max(1, h - 4))

    def search(self, direction: int, new_pattern: bool = False) -> None:
        if new_pattern or self.search_pat is None:
            s = self.prompt("Search regex", self.search_pat.pattern if self.search_pat else "")
            if s is None:
                return
            pat = self.compile_regex(s)
            if pat is None:
                return
            self.search_pat = pat
        pat = self.search_pat
        if pat is None:
            return
        n = self.log.index.line_count
        idx = self.cursor_line + direction
        while 0 <= idx < n:
            text = self.log.line(idx)
            if pat.search(text) and self.line_matches(idx):
                self.cursor_line = idx
                self.invalidate_view_cache()
                self.set_status(f"found line {idx + 1}")
                return
            idx += direction
        self.set_status("not found")

    def draw_help(self) -> None:
        h, w = self.stdscr.getmaxyx()
        lines = [
            "cwlog_view_fast help",
            "",
            "q quit | h help | up/down or k/j move | PgUp/PgDn page",
            "g jump timestamp | / search forward | ? search backward | n/N next/previous",
            "i include regex | x exclude regex | c clear filters",
            "w set filtered time window seconds | r raw context toggle | R set raw context seconds",
            "",
            "Filtered mode shows only matching lines inside ±window seconds around cursor.",
            "Raw mode shows every line around the selected event, ignoring include/exclude filters.",
            "",
            "Tip include regex:",
            "  M_GrabCutterDeckHome|M_GrabCutterDeckP2P|A_CutterDeck_ServoModeSelection|A_CutterDeck_ControlWord|A_CutterDeck_VD3EError",
            "",
            "Press any key to return.",
        ]
        self.stdscr.clear()
        for y, line in enumerate(lines[: h - 1]):
            self.stdscr.addnstr(y, 0, line, w - 1)
        self.stdscr.refresh()
        self.stdscr.getch()

    def draw(self) -> None:
        self.stdscr.erase()
        h, w = self.stdscr.getmaxyx()
        indices = self.compute_view_indices()
        if not indices:
            indices = [self.cursor_line] if self.log.index.line_count else []
        try:
            pos = indices.index(self.cursor_line)
        except ValueError:
            pos = bisect.bisect_left(indices, self.cursor_line)
            if pos >= len(indices):
                pos = max(0, len(indices) - 1)
            if indices:
                self.cursor_line = indices[pos]

        max_lines = max(1, h - 2)
        top_pos = max(0, min(max(0, len(indices) - max_lines), pos - max_lines // 2))
        visible = indices[top_pos: top_pos + max_lines]
        mode = "RAW" if self.raw_mode else "FILTER"
        inc = self.include_pat.pattern if self.include_pat else "-"
        exc = self.exclude_pat.pattern if self.exclude_pat else "-"
        ts = fmt_ts_us(self.current_ts_us())
        header = (
            f"{mode} line {self.cursor_line + 1}/{self.log.index.line_count} ts {ts} "
            f"win={self.window_seconds:g}s raw={self.raw_seconds:g}s inc={inc} exc={exc}"
        )
        self.stdscr.addnstr(0, 0, header, w - 1, curses.A_REVERSE)
        for row, idx in enumerate(visible, start=1):
            prefix = ">" if idx == self.cursor_line else " "
            text = self.log.line(idx)
            line = f"{prefix}{idx + 1:8d} {text}"
            attr = curses.A_REVERSE if idx == self.cursor_line else curses.A_NORMAL
            self.stdscr.addnstr(row, 0, line, w - 1, attr)
        footer = self.status_msg or "h help | q quit"
        self.stdscr.addnstr(h - 1, 0, footer, w - 1, curses.A_REVERSE)
        self.stdscr.refresh()

    def run(self) -> None:
        curses.curs_set(0)
        self.stdscr.keypad(True)
        self.draw()
        while True:
            ch = self.stdscr.getch()
            self.status_msg = ""
            if ch in (ord("q"), 27):
                break
            if ch in (curses.KEY_DOWN, ord("j")):
                self.move_in_view(1)
            elif ch in (curses.KEY_UP, ord("k")):
                self.move_in_view(-1)
            elif ch == curses.KEY_NPAGE:
                self.page_move(1)
            elif ch == curses.KEY_PPAGE:
                self.page_move(-1)
            elif ch == ord("h"):
                self.draw_help()
            elif ch == ord("g"):
                s = self.prompt("Jump timestamp YYYYMMDDTHHMMSS[.uuuuuu]Z", "")
                if s:
                    ts = parse_ts_us_text(s)
                    if ts == NO_TS:
                        self.set_status("invalid timestamp")
                    else:
                        self.cursor_line = self.log.find_nearest_ts_line(ts)
                        self.invalidate_view_cache()
                        self.set_status(f"jumped to line {self.cursor_line + 1}")
            elif ch == ord("/"):
                self.search(1, True)
            elif ch == ord("?"):
                self.search(-1, True)
            elif ch == ord("n"):
                self.search(1, False)
            elif ch == ord("N"):
                self.search(-1, False)
            elif ch == ord("i"):
                s = self.prompt("Include regex; edit/append with |thing, Ctrl-U clears", self.include_pat.pattern if self.include_pat else "")
                if s is not None:
                    self.include_pat = self.compile_regex(s or "")
                    self.invalidate_view_cache()
            elif ch == ord("x"):
                s = self.prompt("Exclude regex; edit/append with |thing, Ctrl-U clears", self.exclude_pat.pattern if self.exclude_pat else "")
                if s is not None:
                    self.exclude_pat = self.compile_regex(s or "")
                    self.invalidate_view_cache()
            elif ch == ord("c"):
                self.include_pat = None
                self.exclude_pat = None
                self.invalidate_view_cache()
                self.set_status("filters cleared")
            elif ch == ord("w"):
                s = self.prompt("Window seconds", str(self.window_seconds))
                if s is not None:
                    try:
                        self.window_seconds = max(0.1, float(s))
                        self.invalidate_view_cache()
                    except ValueError:
                        self.set_status("invalid seconds")
            elif ch == ord("r"):
                self.raw_mode = not self.raw_mode
                self.invalidate_view_cache()
                self.set_status("raw mode on" if self.raw_mode else "filtered mode on")
            elif ch == ord("R"):
                s = self.prompt("Raw context seconds", str(self.raw_seconds))
                if s is not None:
                    try:
                        self.raw_seconds = max(0.1, float(s))
                        self.invalidate_view_cache()
                    except ValueError:
                        self.set_status("invalid seconds")
            self.draw()


def main() -> int:
    ap = argparse.ArgumentParser(description="Fast cursor-style viewer for large Clockwork timestamp logs")
    ap.add_argument("logfile")
    ap.add_argument("--goto", help="initial timestamp YYYYMMDDTHHMMSS[.uuuuuu]Z")
    ap.add_argument("--no-progress", action="store_true")
    args = ap.parse_args()
    if not os.path.exists(args.logfile):
        print(f"not found: {args.logfile}", file=sys.stderr)
        return 2

    idx = build_index(args.logfile, progress=not args.no_progress)
    log = LogFile(args.logfile, idx)
    try:
        def _run(stdscr):
            v = Viewer(stdscr, log)
            if args.goto:
                ts = parse_ts_us_text(args.goto)
                if ts != NO_TS:
                    v.cursor_line = log.find_nearest_ts_line(ts)
            v.run()
        curses.wrapper(_run)
    finally:
        log.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
