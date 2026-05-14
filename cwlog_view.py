#!/usr/bin/env python3
"""
cwlog_view.py - cursor-style Clockwork/log viewer for large timestamped logs.

Designed for lines starting with timestamps like:
  20260514T033449.699048Z\tMachine\tState\t...

Features:
  - indexes a large log once using byte offsets, not full line bodies
  - jump to timestamp
  - include/exclude regex filters
  - filtered view around a time point
  - raw context mode around the selected event, ignoring filters
  - search forward/backward

Usage:
  ./cwlog_view.py ../sampling/log-20260514.txt

Keys:
  q              quit
  h              help
  ↑/↓ or k/j      move cursor
  PgUp/PgDn      page
  g              jump to timestamp, e.g. 20260514T033456 or 20260514T033456.507
  /              search forward regex
  ?              search backward regex
  i              set include regex; only matching lines are shown
  x              set exclude regex; matching lines are hidden
  c              clear include/exclude filters
  w              set time window seconds around cursor timestamp
  r              toggle raw context mode around selected event
  R              set raw context seconds
  n/N            next/previous search result using current search regex
"""

from __future__ import annotations

import argparse
import bisect
import curses
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Pattern

TS_RE = re.compile(rb"^(\d{8})T(\d{6})(?:\.(\d{1,6}))?Z")
TS_TEXT_RE = re.compile(r"^(\d{8})T(\d{6})(?:\.(\d{1,6}))?Z?")


def parse_ts_bytes(line: bytes) -> Optional[float]:
    m = TS_RE.match(line)
    if not m:
        return None
    date_s = m.group(1).decode("ascii")
    time_s = m.group(2).decode("ascii")
    micros_s = (m.group(3) or b"0").decode("ascii").ljust(6, "0")[:6]
    try:
        dt = datetime.strptime(date_s + time_s + micros_s, "%Y%m%d%H%M%S%f")
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc).timestamp()


def parse_ts_text(text: str) -> Optional[float]:
    text = text.strip()
    m = TS_TEXT_RE.match(text)
    if not m:
        return None
    date_s, time_s, micros_s = m.group(1), m.group(2), (m.group(3) or "0").ljust(6, "0")[:6]
    try:
        dt = datetime.strptime(date_s + time_s + micros_s, "%Y%m%d%H%M%S%f")
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc).timestamp()


def fmt_ts(ts: Optional[float]) -> str:
    if ts is None:
        return "no-ts"
    dt = datetime.fromtimestamp(ts, timezone.utc)
    return dt.strftime("%Y%m%dT%H%M%S.%fZ")


@dataclass
class Index:
    offsets: list[int]
    timestamps: list[Optional[float]]
    ts_pairs: list[tuple[float, int]]
    size_bytes: int

    @property
    def line_count(self) -> int:
        return len(self.offsets)


def build_index(path: str) -> Index:
    offsets: list[int] = []
    timestamps: list[Optional[float]] = []
    ts_pairs: list[tuple[float, int]] = []

    with open(path, "rb") as f:
        while True:
            off = f.tell()
            line = f.readline()
            if not line:
                break
            offsets.append(off)
            ts = parse_ts_bytes(line)
            timestamps.append(ts)
            if ts is not None:
                ts_pairs.append((ts, len(offsets) - 1))

    size_bytes = os.path.getsize(path)
    ts_pairs.sort(key=lambda p: p[0])
    return Index(offsets, timestamps, ts_pairs, size_bytes)


class LogFile:
    def __init__(self, path: str, index: Index):
        self.path = path
        self.index = index
        self.f = open(path, "rb")

    def close(self) -> None:
        self.f.close()

    def line(self, idx: int) -> str:
        if idx < 0 or idx >= self.index.line_count:
            return ""
        self.f.seek(self.index.offsets[idx])
        return self.f.readline().decode("utf-8", errors="replace").rstrip("\n")

    def find_nearest_ts_line(self, ts: float) -> int:
        pairs = self.index.ts_pairs
        if not pairs:
            return 0
        pos = bisect.bisect_left([p[0] for p in pairs], ts)
        if pos <= 0:
            return pairs[0][1]
        if pos >= len(pairs):
            return pairs[-1][1]
        before = pairs[pos - 1]
        after = pairs[pos]
        return before[1] if abs(before[0] - ts) <= abs(after[0] - ts) else after[1]


class Viewer:
    def __init__(self, stdscr, log: LogFile):
        self.stdscr = stdscr
        self.log = log
        self.cursor_line = 0
        self.top_line = 0
        self.include_pat: Optional[Pattern[str]] = None
        self.exclude_pat: Optional[Pattern[str]] = None
        self.search_pat: Optional[Pattern[str]] = None
        self.window_seconds: Optional[float] = 60.0
        self.raw_mode = False
        self.raw_seconds = 5.0
        self.filtered_cache_center: Optional[int] = None
        self.filtered_cache: list[int] = []
        self.filtered_cursor_pos = 0
        self.status_msg = ""

    def prompt(self, label: str, default: str = "") -> Optional[str]:
        curses.echo()
        self.stdscr.nodelay(False)
        h, w = self.stdscr.getmaxyx()
        self.stdscr.move(h - 1, 0)
        self.stdscr.clrtoeol()
        prompt = f"{label} [{default}]: " if default else f"{label}: "
        self.stdscr.addnstr(h - 1, 0, prompt, max(0, w - 1), curses.A_REVERSE)
        self.stdscr.refresh()
        try:
            data = self.stdscr.getstr(h - 1, min(len(prompt), w - 1), max(1, w - len(prompt) - 1))
        except KeyboardInterrupt:
            curses.noecho()
            return None
        curses.noecho()
        s = data.decode("utf-8", errors="replace").strip()
        return s if s else default

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

    def current_ts(self) -> Optional[float]:
        if 0 <= self.cursor_line < self.log.index.line_count:
            return self.log.index.timestamps[self.cursor_line]
        return None

    def compute_view_indices(self) -> list[int]:
        n = self.log.index.line_count
        if n == 0:
            return []

        if self.raw_mode:
            center_ts = self.current_ts()
            if center_ts is None:
                start = max(0, self.cursor_line - 100)
                end = min(n, self.cursor_line + 101)
                return list(range(start, end))
            lo_ts, hi_ts = center_ts - self.raw_seconds, center_ts + self.raw_seconds
            return self.lines_in_time_range(lo_ts, hi_ts, apply_filter=False)

        # Filtered mode: show include/exclude-matching lines in a time window around cursor.
        if self.window_seconds is None:
            # Fallback: scan the whole file. Useful, but can be slow on very large logs.
            return [i for i in range(n) if self.line_matches(i)]

        center_ts = self.current_ts()
        if center_ts is None:
            # If selected line has no timestamp, use raw nearby line range and filters.
            start = max(0, self.cursor_line - 2000)
            end = min(n, self.cursor_line + 2001)
            return [i for i in range(start, end) if self.line_matches(i)]

        return self.lines_in_time_range(center_ts - self.window_seconds, center_ts + self.window_seconds, apply_filter=True)

    def lines_in_time_range(self, lo_ts: float, hi_ts: float, apply_filter: bool) -> list[int]:
        pairs = self.log.index.ts_pairs
        if not pairs:
            return []
        ts_list = [p[0] for p in pairs]
        start = bisect.bisect_left(ts_list, lo_ts)
        end = bisect.bisect_right(ts_list, hi_ts)
        out: list[int] = []
        for _, idx in pairs[start:end]:
            if not apply_filter or self.line_matches(idx):
                out.append(idx)
        return out

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
                self.set_status(f"found line {idx + 1}")
                return
            idx += direction
        self.set_status("not found")

    def draw_help(self) -> None:
        h, w = self.stdscr.getmaxyx()
        lines = [
            "cwlog_view help",
            "",
            "q quit | h help | up/down or k/j move | PgUp/PgDn page",
            "g jump timestamp | / search forward | ? search backward | n/N next/previous",
            "i include regex | x exclude regex | c clear filters",
            "w set filtered time window seconds | r raw context toggle | R set raw context seconds",
            "",
            "Mode notes:",
            "  Filtered mode shows only lines matching include/exclude inside ±window seconds.",
            "  Raw mode shows every line around the selected event, ignoring filters.",
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
        visible = indices[top_pos : top_pos + max_lines]

        mode = "RAW" if self.raw_mode else "FILTER"
        inc = self.include_pat.pattern if self.include_pat else "-"
        exc = self.exclude_pat.pattern if self.exclude_pat else "-"
        ts = fmt_ts(self.current_ts())
        header = (
            f"{mode} line {self.cursor_line + 1}/{self.log.index.line_count} ts {ts} "
            f"win={self.window_seconds if self.window_seconds is not None else 'all'}s raw={self.raw_seconds}s "
            f"inc={inc} exc={exc}"
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
            elif ch in (curses.KEY_DOWN, ord("j")):
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
                    ts = parse_ts_text(s)
                    if ts is None:
                        self.set_status("invalid timestamp")
                    else:
                        self.cursor_line = self.log.find_nearest_ts_line(ts)
                        self.set_status(f"jumped to line {self.cursor_line + 1}")
            elif ch == ord("/"):
                self.search(1, new_pattern=True)
            elif ch == ord("?"):
                self.search(-1, new_pattern=True)
            elif ch == ord("n"):
                self.search(1, new_pattern=False)
            elif ch == ord("N"):
                self.search(-1, new_pattern=False)
            elif ch == ord("i"):
                s = self.prompt("Include regex; blank clears", self.include_pat.pattern if self.include_pat else "")
                self.include_pat = self.compile_regex(s or "") if s is not None else self.include_pat
            elif ch == ord("x"):
                s = self.prompt("Exclude regex; blank clears", self.exclude_pat.pattern if self.exclude_pat else "")
                self.exclude_pat = self.compile_regex(s or "") if s is not None else self.exclude_pat
            elif ch == ord("c"):
                self.include_pat = None
                self.exclude_pat = None
                self.set_status("filters cleared")
            elif ch == ord("w"):
                s = self.prompt("Window seconds; blank/all for all filtered file", str(self.window_seconds or "all"))
                if s is not None:
                    if s.lower() in ("", "all", "none", "0"):
                        self.window_seconds = None
                    else:
                        try:
                            self.window_seconds = max(0.1, float(s))
                        except ValueError:
                            self.set_status("invalid seconds")
            elif ch == ord("r"):
                self.raw_mode = not self.raw_mode
                self.set_status("raw mode on" if self.raw_mode else "filtered mode on")
            elif ch == ord("R"):
                s = self.prompt("Raw context seconds", str(self.raw_seconds))
                if s is not None:
                    try:
                        self.raw_seconds = max(0.1, float(s))
                    except ValueError:
                        self.set_status("invalid seconds")
            self.draw()


def main() -> int:
    ap = argparse.ArgumentParser(description="Cursor-style viewer for large Clockwork timestamp logs")
    ap.add_argument("logfile")
    ap.add_argument("--goto", help="initial timestamp YYYYMMDDTHHMMSS[.uuuuuu]Z")
    args = ap.parse_args()

    if not os.path.exists(args.logfile):
        print(f"not found: {args.logfile}", file=sys.stderr)
        return 2

    print(f"Indexing {args.logfile}...", file=sys.stderr)
    idx = build_index(args.logfile)
    print(f"Indexed {idx.line_count} lines, {len(idx.ts_pairs)} timestamped lines, {idx.size_bytes} bytes", file=sys.stderr)

    log = LogFile(args.logfile, idx)
    try:
        def _run(stdscr):
            v = Viewer(stdscr, log)
            if args.goto:
                ts = parse_ts_text(args.goto)
                if ts is not None:
                    v.cursor_line = log.find_nearest_ts_line(ts)
            v.run()
        curses.wrapper(_run)
    finally:
        log.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
