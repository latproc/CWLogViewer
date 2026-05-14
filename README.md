# CWLogViewer

CWLogViewer is a terminal viewer for large Clockwork-style logs with timestamped lines. It is optimized for quick seeking, interactive filtering, and staying usable on very large files without building a full index.

## Features

- Timestamp seek with `--goto`
- Include/exclude regex filters
- Persistent filter history and active filter state in `~/.cwlog`
- Startup session naming that records the current file position and active filters
- Less-style navigation keys
- Follow/tail mode for watching appended log lines

## Requirements

- Python 3
- A terminal with `curses` support

## Usage

```bash
python3 cwlog_view.py /path/to/logfile
python3 cwlog_view.py /path/to/logfile --goto 20260514T033456.507596Z
```

`--seek-debug` prints timestamp seek diagnostics before opening the UI.

## Key Bindings

- `g` go to the top of the file
- `G` go to the bottom of the file
- `F` toggle follow/tail mode
- `/` search forward
- `?` search backward
- `n` repeat the last search in the same direction
- `N` repeat the last search in the opposite direction
- `i` set include regex
- `x` set exclude regex
- `c` clear filters
- `r` toggle raw mode
- `w` set the filtered time window
- `R` set the raw context window
- `Up` and `Down` move the cursor
- `PageUp` and `PageDown` move faster
- `q` quit

When follow mode is enabled, any browsing key breaks out of follow mode and returns to normal browsing.

## Saved State

The viewer stores its state in `~/.cwlog` as JSON.

Saved data includes:

- Active include/exclude regexes
- Include/exclude history
- Named session records

Named session records capture:

- Session name
- Log file path
- Current offset and timestamp
- Active filters
- Window sizes
- Raw mode state

## Notes

- Filters are restored on launch, so you do not need to re-enter them every time.
- If a filter regex is invalid, the viewer reports the regex error in the status line.
- Follow mode is intended for tailing the end of the file while it grows; use `F` again or any browsing key to leave it.
