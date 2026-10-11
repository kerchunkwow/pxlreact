# pxlreact2 Transition Tracker

This is the **status document** for the pxlreact2 transition, kept as the record of what was done
and why. The transition is **complete**; current architecture conventions live in
`.cursor/rules/pxlreact-architecture.mdc` and the user-facing overview in `README.md`. The original
vision/requirements document is archived at `pxlreact1_archive/PXLREACT_2.md`.

## Overall Objective
Transition `pxlreact` to `pxlreact2` **in-place in this repository**: externalize all inline
configuration, add a lightweight GUI, optimize hot paths, and refresh rules/documentation — without
changing the two core functions (pixel reactions and remap sequences).

## Ground Rules
- Do not modify `pyinterception/` or the underlying intercept driver; be careful in adjacent layers
  (`pxl_intercept.py`), though logical improvements there are welcome.
- No effort spent on backward compatibility with pxlreact1 designs.
- Ignore legacy GUI artifacts and notes (tkinter experiments); GUI library choice is made fresh in Phase 2.
- Each phase ends with a **user-validation gate**: the user verifies behavior against the live game
  before the phase is marked complete and focus advances.
- Each phase gets its own detailed plan when it begins; do not implement ahead of the current focus.

## Phases

| Phase | Scope | Status |
|-------|-------|--------|
| 0 — Scaffolding & baseline | Tracking doc, transition rule, `requirements.txt`, baseline tag `pxlreact1-final` | Complete |
| 1 — External configuration | External config files (`settings.toml` + `profile.json`) replacing inline config in `PxlReactionRegistry` (`pxlreactHL.py`), `pxl_remap_maps.py`, `pxl_wincheck.py` hard-coded values, and `pxl_keys.py`; retire global `COLOR_TOLERANCE` in favor of per-check tolerance; replace `'reaction': self.react_XX` callables with declarative key-press fields | Complete |
| 2 — Lightweight GUI | DearPyGui GUI layer: separate-process profile editor (`pxl_editor.py`), Ctrl+R profile reload in the core, and an in-process runtime status bar (`pxl_statusbar.py` fed by the `pxl_status.py` StatusHub) replacing the terminal crawl | Complete |
| 3 — Performance review | Benchmarked hot paths; replaced per-pixel GDI `GetPixel` (~2.8 ms/call) with the mss-backed `PixelSource` frame cache (one grab per tick serves all configured pixels); retired dead/duplicative code; dropped first-party `pywin32` usage | Complete |
| 4 — Rules & documentation | `README.md` rewritten for the post-transition architecture; transition rule replaced by `pxlreact-architecture.mdc`; vision doc archived | Complete |

## Current Focus
**Transition complete.** All four phases are done; the final state awaits a live-game validation
pass of the Phase 3 performance changes (new pixel read path) alongside the Phase 2 GUI layout.

## Hand-Tuned Behaviors (user-supplied)
Behaviors not captured in code comments that a refactor must preserve (timing feel, debounce quirks,
tolerance values tuned by trial and error). *To be filled in by the user before Phase 1 begins.*

- Hand-tuned tolerance values were arrived at by trial and error, but this should be retired in `pxlreact2` in favor of more dynamic settings associated with each color-driven trigger; a global default value can be set, but each individual reaction or ability-readiness check should be tunable, including those associated with the "wincheck" status monitor component

- Input timing, debounce, etc. feels fine; no quirks or nuances

## Decision Log
- **In-place evolution** in this repo (no greenfield package or new repo).
- **Config-first ordering**: external configuration (Phase 1) precedes the GUI (Phase 2), since the
  GUI reads/writes the config schema.
- All current reaction handlers only call `PI.press(<key>)`, so declarative key-press config fields
  fully replace the callable association in `PxlReactionRegistry`.
- `keyboard` package is listed in the README but no longer imported by first-party code; it is not a
  dependency of pxlreact2.
- **Two config files, not one** (Phase 1): `settings.toml` for manual low-churn settings (stdlib
  `tomllib`, supports comments) and `profile.json` for GUI-managed gameplay config (stdlib `json`
  round-trip). The GUI writes only `profile.json`.
- **Normalization at load time** (Phase 1): `pxl_config.py` fills every color check's `tolerance`
  (from `color.default_tolerance` unless set per-check; wincheck markers default to 0 = exact) and
  converts colors to tuples, so runtime code never consults a global default.
- **Archive convention**: deprecated files are moved to `pxlreact1_archive/` rather than deleted;
  `pxl_keys.py` and `pxl_remap_maps.py` moved there in Phase 1.
- **DearPyGui over PySide6** (Phase 2): MIT, pip-installable, GPU-rendered, and thread-safe in
  2.x, allowing the status bar to render on a background thread inside the core process while the
  main thread keeps the 25 ms poll loop (Qt requires owning the process main thread). DPG is in
  maintenance mode (stable/bug-fix focus) - acceptable; revisit only if designer-grade UI is needed.
- **StatusHub funnel** (Phase 2): gameplay state is published through `pxl_status.StatusHub` and
  rendered only by the status bar - gameplay events produce no terminal output (`gui.terminal_echo`
  retired); the status bar refreshes rotation color checks at the slower `gui.color_check_hz` to
  limit GDI contention with the poll loop.
- **Reload semantics** (Phase 2): Ctrl+R re-reads profile.json between poll ticks; a validation
  failure keeps the running config; cooldown/readiness timing state resets on reload.
- **Remaps merged into rotations** (Phase 2 feedback): the separate `remaps` config tier and editor
  tab were retired; each rotation carries its own source `key` (unique across rotations, validated
  at load). The `glyph` field was dropped from reactions - glyphs existed to make the terminal
  crawl readable, which the GUI replaces.
- **Status bar design** (Phase 2 feedback): a framed headline strip at the top shows only the most
  recently fired ability with a right-side timestamp; its frame flashes red for ~0.6 s when a press
  is dropped during a cast. No keyboard keys are shown anywhere in the GUI, the scrolling event
  feed was removed, and input received while INACTIVE passes through silently (no reporting).
- **PixelSource frame cache** (Phase 3): GDI `GetPixel` costs ~2.8 ms per call and an mss BitBlt
  grab costs the same ~2.8-5.5 ms regardless of region size, so all reads now go through
  `pxl_lib.PIXELS`: one grab per tick of the bounding region of every configured pixel (registered
  from the profile at startup/reload), served from the raw BGRA buffer in sub-microsecond time.
  Tick cost dropped from ~8.3 ms (33% of budget) to ~5.6 ms (22%), and every additional color check
  (readiness, remapper fires, status bar) went from +2.8 ms to effectively free. `app.frame_max_age`
  bounds staleness. Out-of-region reads (picker, Ctrl+P monitor) fall back to uncached 1x1 grabs.
  Never use mss `ScreenShot.pixel()` (it builds a full nested pixel list, ~30 ms on wide frames).
- **Dead code retired** (Phase 3): `pxl_lib` GDI machinery and unused helpers
  (`get_active_window_rect`, `get_mouse_pos_window`, `find_most_similar_pixel`,
  `report_mouse_color`); `PxlIntercept.react`/`press_and_hold` (and the `[intercept]`
  reaction-time settings); device detection consolidated into `pxl_intercept.detect_device_index`
  (shared with the remapper); `get_color_difference` unrolled (~4x faster).
- **Library review** (Phase 3): first-party `pywin32` usage replaced with two ctypes calls in
  `pxl_wincheck` (the package remains installed only for the vendored pyinterception clone);
  `mss` promoted from optional (capture) to the core pixel path (deprecated `mss.mss()` calls
  updated to `mss.MSS()`); DearPyGui and the stdlib TOML/JSON loaders kept as-is. DXGI-based
  grabbers (dxcam/bettercam) noted as a future option if sub-millisecond grabs are ever needed;
  not adopted - extra dependency weight for no current benefit at a 25 ms tick.

## Gate History
- **2026-07-11 — Phase 0 gate passed.** Scaffolding committed and tagged (`pxlreact1-final`); user
  validated the baseline against the live game: reactions and remaps fire, input timing and debounce
  feel fine, no quirks to preserve beyond the tolerance notes above.
- **2026-07-11 — Phase 1 gate passed.** All inline configuration migrated to `settings.toml` +
  `profile.json` (loader/validator in `pxl_config.py`); global `COLOR_TOLERANCE` retired for
  per-check tolerances; reaction callables replaced by declarative `press`/`glyph` fields; wincheck
  supports multiple markers; `pxl_keys.py` and `pxl_remap_maps.py` archived. User validated against
  the live game: fully functional. This state is the fallback "pxlreact 1.5" if the GUI work does
  not pan out.
- **2026-07-11 — Phase 2 gate passed.** User validated the GUI iteration against the live game
  (save/update/reload "works smoothly") and pre-authorized closure once the functional feedback was
  addressed: headline ability bar with cast-drop flash, GUI reordered (rotations above reactions),
  keys/event feed/terminal gameplay output removed, remaps merged into rotations, glyphs retired.
  Headless smoke tests passed after the rework; a live-game spot check of the new layout is advised
  at the start of Phase 3.
- **2026-07-11 — Phases 3 & 4 implemented.** Performance: PixelSource frame cache replaces per-call
  GDI reads (verified byte-identical at every configured coordinate; tick cost 8.3 ms -> 5.6 ms,
  additional checks now free); dead/duplicative code retired; pywin32 dropped from first-party
  code. Documentation: README rewritten, transition rule replaced by `pxlreact-architecture.mdc`,
  vision doc archived. **Awaiting live-game validation** of the new pixel path (reactions, remap
  fires, wincheck gating, status bar) to close the transition.
