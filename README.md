# PxlReact

PxlReact is a lightweight Windows automation utility for gaming that:

- **Monitors screen pixels** for color changes and triggers configured key presses (reactions)
- **Remaps keyboard input** at the driver level: one physical key resolves an ordered rotation of
  abilities and sends the first one that is ready (cooldown + on-screen color checks)

Input is injected through the Interception driver, so the target application receives events as if
they came from hardware. All gameplay behavior is externally configured (no inline config in code),
and an optional DearPyGui status bar and profile editor round out the runtime.

---

## Architecture

| Module | Role |
|--------|------|
| `pxlreactHL.py` | Main entry point: loads config, wires subsystems, runs the ~25 ms pixel poll loop |
| `pxl_config.py` | Loads, normalizes, and validates `settings.toml` + `profile.json` |
| `pxl_lib.py` | Pixel/color utilities: the shared `PixelSource` frame cache, color math, `ColorCondition`, `CastLock`, `PixelMonitor` |
| `pxl_wincheck.py` | Session gating: foreground window title + marker pixels, checked live at each fire point |
| `pxl_remap.py` | Keyboard capture and remapping (rotations); owns the command hotkeys |
| `pxl_intercept.py` | Interception-based key injection with humanized delays |
| `pxl_status.py` | `StatusHub`: thread-safe runtime state store (no GUI imports) |
| `pxl_statusbar.py` | In-process DearPyGui status bar (imported only when enabled) |
| `pxl_editor.py` | Separate-process DearPyGui editor for `profile.json` |
| `pxl_capture.py` | Optional debug mode: PNG snapshots of the region around a firing reaction |
| `ansi.py` | ANSI color shorthand for terminal output |

Retired pxlreact1 files live in `pxlreact1_archive/`. The transition record is in
`PXLREACT2_STATUS.md`.

### Performance notes

- All pixel reads go through `pxl_lib.PIXELS` (a `PixelSource`): one `mss` screen grab per tick
  covers the bounding region of every configured pixel (~5.5 ms), and every read within the tick
  is served from that frame in sub-microsecond time. Coordinates outside the region (pixel picker,
  Ctrl+P monitor) fall back to an uncached 1×1 grab.
- `frame_max_age` in `settings.toml` controls how long a grabbed frame keeps serving reads; keep
  it below `tick_interval` so each tick grabs fresh.
- The status bar refreshes rotation color checks at the slower `gui.color_check_hz`, and those
  reads are cache hits when the poll loop has grabbed recently.

### Threading model

- **Main thread**: the poll loop (pixel reactions, profile reload between ticks).
- **PxlRemapper thread**: blocking Interception capture loop; sends substitutes on the same thread.
- **StatusBar thread** (optional): DearPyGui render loop reading `StatusHub` snapshots.
- **PxlIntercept pool**: short-lived key press tasks.
- Shared state is coordinated through locks (`PixelSource` frame cache, `StatusHub`, `TriggerLog`,
  `CastLock`); `mss` instances are per-thread via thread-local storage.

---

## Setup (Windows + PowerShell)

Requirements: Windows 11, Python 3.12, the [Interception driver](https://github.com/oblitum/Interception#readme),
and this repository (which vendors [pyinterception](https://github.com/kennyhml/pyinterception) in
`pyinterception/` — do not modify it).

```powershell
cd C:\dev\pxlreact
python -m venv .pxlenv
.\.pxlenv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Set your device hardware IDs in `settings.toml` under `[devices]` (match substrings of the HWIDs
reported at startup).

## Run

```powershell
python pxlreactHL.py        # the core app
python pxl_editor.py        # the profile editor (separate process, can run alongside)
```

Hotkeys (owned by the remapper's capture loop, active anywhere):

- `F12` / `ESC` — quit cleanly
- `Ctrl+P` — toggle the pixel monitor: reports the color under the cursor whenever it changes
  (for discovering coordinates and `ignore_colors` values)
- `Ctrl+R` — reload `profile.json` into the running app (applied between poll ticks; an invalid
  profile is rejected and the running config keeps working)

---

## Configuration

Configuration is split across two files; nothing gameplay-related is defined in code.

### `settings.toml` — low-churn application settings (edit manually)

- `[app]` — `tick_interval` (poll rate), `frame_max_age` (pixel frame cache lifetime)
- `[color]` — `default_tolerance`: SSD (sum of squared differences) tolerance used by any color
  check that does not set its own
- `[devices]` — keyboard/mouse hardware IDs for Interception device matching
- `[intercept]` / `[remapper]` — injection pool size and humanized press/hold delay ranges
- `[gui]` — status bar enable, fps, `color_check_hz`, viewport position/size, reload key, plus
  appearance knobs (`title`, `decorated`, `always_on_top`, `resizable`, `clear_color`, optional
  `font_path`/`font_size`, and semantic `color_*` RGB triples)
- `[trigger_log]` — record the pixel color responsible for each reaction firing; persists across
  sessions and prints a collapsed per-color report at exit (high-count benign tints are obvious
  `ignore_colors` candidates)
- `[capture]` — debug mode: save a PNG of the region around a reaction's pixel just before it fires

### `profile.json` — gameplay configuration (managed by `pxl_editor.py` or by hand)

- **wincheck** — target window title plus marker pixels that must ALL match (each with its own
  tolerance, default 0 = exact) for the app to act; guards against loading screens and overlays
- **reactions** — monitored pixels: coordinates, firing condition (`react_if_not_color` /
  `react_if_color` with per-reaction tolerance), `confirm` debounce, readiness (cooldown and/or
  indicator-pixel color checks and/or another reaction's cooldown settle window, ANDed),
  `ignore_colors` for benign tints, the key to `press`, and an optional `cast_time` that arms the
  shared cast lock. A ready check `{ "type": "reaction", "reaction": "peer_name", "settle_ratio": 0.15 }`
  passes only while the peer is on cooldown and at least `settle_ratio` of its timed cooldown has
  already elapsed (default 0.15) — used to chain thresholds without burning two charges on one hit
- **actions** — the building blocks of rotations: key (or mouse button `left`/`right`/`middle`),
  cooldown, cast time, and pixel color checks that must all pass
- **rotations** — each rotation binds a physical source `key` and an ordered `actions` list; when
  the key is pressed in the active game context, the first ready action fires. Presses that arrive
  while a cast is in progress are dropped (the status bar frame flashes)

The editor validates on save (via the same loader the core uses) and writes atomically, so an
invalid profile can never reach disk; apply changes to a running core with `Ctrl+R`.

---

## Notes & Limitations

- Personal-use tool for a specific Windows desktop; no cross-platform or multi-user concerns.
- Color-based triggers are sensitive to overlays, post-processing, and gamma changes — retune
  colors/tolerances (the trigger log and capture mode exist for exactly this).
- The core runs fully headless with `gui.statusbar_enabled = false`; no module imports DearPyGui
  unless the GUI is enabled.
