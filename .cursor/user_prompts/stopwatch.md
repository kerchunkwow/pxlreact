# Prompt: Build a Standalone Overlay Stopwatch

Fill in the bracketed **Specs** below, then give this entire file to an LLM. The Specs tell it how to target your environment and defaults; everything else defines the tool.

## Specs (fill in)

- Python / OS / Shell: `[3.12.10]` / `[Windows 11]` / `[PowerShell 7.5.2]`
- Keys start/pause/resume/reset/exit: `[f9 / f9 / f9 / f10 / f11]`
- Default color + time steps (sec -> color): `[lime]` + `[1.0 -> magenta, 5.0 -> yellow]`
- Font / window / transparency: `[Consolas 16 bold]` / `[160x40 @ (100,100)]` / `[0.8]`

## Task

Build a single-file `stopwatch.py`: an always-on-top overlay stopwatch. Target the runtime in Specs. Dependencies: stdlib + `pynput` only (`pip install pynput`); no other local modules.

**Window (tkinter)**
- Borderless (`overrideredirect(True)`), `-topmost`, `-alpha` transparency; configurable size, position, bg color.
- Bold monospaced label showing `MM:SS.mmm`; refresh via `root.after(interval_ms, ...)`.
- Draggable from anywhere: bind `<Button-1>` + `<B1-Motion>` on window and label.

**Hotkeys (global, work unfocused)**
- Daemon `pynput.keyboard.Listener`. Listener never touches tkinter; it pushes actions to a `queue.Queue` drained inside the tkinter refresh loop (thread-safe).
- Timing: `time.perf_counter()`; on begin/resume set `start_time = perf_counter() - elapsed` to preserve paused time.

**Actions (state-gated): start, pause, resume, reset, exit**
- `start`: only if stopped and elapsed == 0 -> run from zero.
- `resume`: only if stopped and elapsed > 0 -> continue.
- `pause`: only if running -> stop counting, keep elapsed.
- `reset`: any state -> stop + zero.
- `exit`: any state -> stop listener, close window.
- One key may bind multiple actions; per press fire exactly one — first valid for current state, priority `exit > reset > pause > resume > start`. (So start/pause/resume on one key toggles: begin -> pause -> resume -> ...).

**Color**: default color from 0; ordered `{seconds: color}` map, switch once elapsed >= threshold (later wins); recompute each refresh.

**Auto-reset (default off)**: optional seconds -> auto reset when reached; optional loop flag -> immediately restart from zero after each auto-reset.

**Hold-timer mode (default off)**: press-and-hold start key to time (press = start, release = pause) via the listener's release callback (routed through the queue), then auto-reset after a configurable delay ms (0 disables).

**Config**: ALL settings are clearly sectioned globals at top of file (no CLI/argparse): 5 keybinds, hold-timer toggle + delay ms, window geometry/appearance/transparency, refresh interval, default color, time->color map, auto-reset seconds + loop flag. Keys are friendly strings — single char (`"a"`) or named (`"f9"`, `"space"`, `"esc"`); resolve via `getattr(keyboard.Key, s, None)` else `keyboard.KeyCode.from_char(s)`.

**Console**: on start, print a short ANSI-colored control summary generated from the configured keys (don't hard-code key names); inline the few ANSI codes used; one line per state change max.

**Defaults**: use the values from Specs; auto-reset off; loop off; hold-timer off.

Output the full `stopwatch.py`; run with `python stopwatch.py`.

