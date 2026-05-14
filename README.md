# CWLogViewer

CWLogViewer is a terminal viewer for large Clockwork-style logs with timestamped lines. It is optimized for quick seeking, interactive filtering, and staying usable on very large files without building a full index.

It can also replay the event log to show a live machine/property state panel for the selected timestamp.

## Features

- Timestamp seek with `--goto`
- Session load/create with `--session`
- Include/exclude regex filters
- Persistent filter history and active filter state in `~/.cwlog`
- Startup session naming that records the current file position and active filters
- Session picker for loading or creating sessions at startup and from the `s` command
- Less-style navigation keys
- Follow/tail mode for watching appended log lines
- Local-time rendering toggle
- Live state panel reconstructed from the log
- Full-screen state inspector for the current machine/property family

## Requirements

- Python 3
- A terminal with `curses` support

## Usage

```bash
python3 cwlog_view.py /path/to/logfile
python3 cwlog_view.py /path/to/logfile --goto 20260514T033456.507596Z
python3 cwlog_view.py /path/to/logfile --session morning-shift
```

`--seek-debug` prints timestamp seek diagnostics before opening the UI.

## Key Bindings

- `g` go to the top of the file
- `G` go to the bottom of the file
- `j` jump to a timestamp
- `F` toggle follow/tail mode
- `t` toggle local-time rendering
- `m` toggle the live state panel
- `M` toggle the state inspector
- `s` save the current view as a named session
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

When local-time rendering is enabled, `j` accepts local wall-clock times. Otherwise it accepts UTC timestamps.
When follow mode is enabled, any browsing key breaks out of follow mode and returns to normal browsing.
At startup, if saved sessions exist, the viewer shows a picker. Choose an existing session or pick "New session..." to create one.
The state panel is on by default and shows the reconstructed state for the selected timestamp. Use `m` to hide/show it and `M` for a larger inspector view.

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
- Local-time preference
- State panel preference
- Preferred jump timestamp for reopening on another file

Saved sessions can be picked from the startup picker or the `s` command.

## Notes

- Filters are restored on launch, so you do not need to re-enter them every time.
- If a filter regex is invalid, the viewer reports the regex error in the status line.
- Follow mode is intended for tailing the end of the file while it grows; use `F` again or any browsing key to leave it.
- If a session is opened on a different file, the viewer jumps to the saved timestamp when available, otherwise it falls back to around `06:00` on the file's first day.
- The state panel is derived from the log itself, so the first lookup on a large file can take noticeable time before caches warm up.
