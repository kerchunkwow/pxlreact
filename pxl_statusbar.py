"""
pxl_statusbar.py renders the in-process runtime status bar: a small always-on-top DearPyGui window
that replaces the terminal crawl during gameplay.

Layout (top to bottom):
- headline bar: a framed strip showing the most recently fired ability with a right-side timestamp;
  the frame flashes red briefly when a press arrives during a cast and is dropped
- context line: ACTIVE/INACTIVE and live CASTING state
- rotations: one header per rotation with live per-action readiness rows (cooldown + color checks)
- reactions: live readiness rows (cooldown + color gates) plus last-fire swatch/timing

Readiness rows are name | cooldown | per-gate indicators. Cooldown text appears only while cooling
(empty when ready — row color already signals readiness). Gate glyphs (+/x/?) keep their own colors
independent of the cooldown coloring on the name.

The bar runs its own render loop on a daemon thread (DPG 2.x is thread-safe) and reads StatusHub
snapshots each frame; the 40 Hz poll loop on the main thread is untouched. Live cooldowns are
computed from shared Action / PxlReaction state (no pixel reads); color gates DO read pixels, so
they refresh at the lower `color_check_hz` rate and only while the app context is active, keeping
GDI contention with the poll loop negligible.

Viewport chrome, font, and semantic colors come from settings.toml [gui]. This module is imported
only when gui.statusbar_enabled is true, so headless runs never touch dearpygui. Closing the status
bar window kills only the bar; the core keeps running.
"""

import os
import threading
import time

import dearpygui.dearpygui as dpg

from ansi import *

HEADLINE_HEIGHT = 44


def _rgba( rgb ):
    """Convert a normalized [r,g,b] list to a DearPyGui RGBA tuple."""
    return ( rgb[ 0 ], rgb[ 1 ], rgb[ 2 ], 255 )


class StatusBar:

    def __init__( self, hub, gui_cfg ):
        self.hub = hub
        self.fps = gui_cfg[ 'fps' ]
        self.color_interval = 1.0 / gui_cfg[ 'color_check_hz' ]
        self.pos = gui_cfg[ 'pos' ]
        self.size = gui_cfg[ 'size' ]
        self.title = gui_cfg[ 'title' ]
        self.always_on_top = gui_cfg[ 'always_on_top' ]
        self.resizable = gui_cfg[ 'resizable' ]
        self.decorated = gui_cfg[ 'decorated' ]
        self.clear_color = gui_cfg[ 'clear_color' ]
        self.font_path = gui_cfg[ 'font_path' ]
        self.font_size = gui_cfg[ 'font_size' ]

        self.color_ready = _rgba( gui_cfg[ 'color_ready' ] )
        self.color_cooldown = _rgba( gui_cfg[ 'color_cooldown' ] )
        self.color_blocked = _rgba( gui_cfg[ 'color_blocked' ] )
        self.color_text = _rgba( gui_cfg[ 'color_text' ] )
        self.color_muted = _rgba( gui_cfg[ 'color_muted' ] )
        self.color_alert = _rgba( gui_cfg[ 'color_alert' ] )

        self._stop = threading.Event()
        self._thread = None

        # Cached row signatures; a change (profile reload) triggers a row rebuild
        self._reaction_sig = None
        self._rotation_sig = None

        # id(ColorCondition|gate) -> last passes() result, refreshed at color_check_hz while active
        self._color_cache = {}
        self._last_color_refresh = 0.0

        self._flash_theme = None
        self._flash_bound = False

    def start( self ):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread( target = self._run, name = 'StatusBar', daemon = True )
        self._thread.start()

    def stop( self ):
        self._stop.set()
        if self._thread:
            self._thread.join( timeout = 2.0 )

    # ------------------------------------------------------------------ render

    def _bind_font( self ):
        """Load and bind an optional custom font; invalid paths warn and keep the DPG default."""
        path = ( self.font_path or "" ).strip()
        if not path:
            return
        if not os.path.isfile( path ):
            print( f"{YELLOW}status bar: font not found ({CYAN}{path}{RESET}); using default{RESET}" )
            return
        try:
            with dpg.font_registry():
                font = dpg.add_font( path, self.font_size )
            dpg.bind_font( font )
        except Exception as exc:
            print( f"{YELLOW}status bar: could not load font ({CYAN}{path}{RESET}): {exc}{RESET}" )

    def _run( self, max_frames = None ):
        try:
            dpg.create_context()
            self._bind_font()
            dpg.create_viewport(
                title = self.title,
                width = self.size[ 0 ],
                height = self.size[ 1 ],
                x_pos = self.pos[ 0 ],
                y_pos = self.pos[ 1 ],
                always_on_top = self.always_on_top,
                resizable = self.resizable,
                decorated = self.decorated,
                clear_color = self.clear_color,
            )

            # Alert border theme, bound to the headline bar while a dropped-press flash is active
            with dpg.theme() as self._flash_theme:
                with dpg.theme_component( dpg.mvChildWindow ):
                    dpg.add_theme_color( dpg.mvThemeCol_Border, self.color_alert )

            with dpg.window( tag = 'sb_root' ):
                # Headline: most recently fired ability, timestamp hugging the right edge
                with dpg.child_window( tag = 'sb_headline', height = HEADLINE_HEIGHT, border = True ):
                    with dpg.table( header_row = False, policy = dpg.mvTable_SizingStretchProp ):
                        dpg.add_table_column( init_width_or_weight = 1.0 )
                        dpg.add_table_column( init_width_or_weight = 70, width_fixed = True )
                        with dpg.table_row():
                            dpg.add_text( '--', tag = 'sb_ability', color = self.color_text )
                            dpg.add_text( '', tag = 'sb_ability_t', color = self.color_muted )

                with dpg.group( horizontal = True ):
                    dpg.add_text( 'INACTIVE', tag = 'sb_active', color = self.color_muted )
                    dpg.add_text( '', tag = 'sb_cast', color = self.color_alert )
                dpg.add_separator()
                dpg.add_text( 'rotations', color = self.color_muted )
                dpg.add_group( tag = 'sb_rotations' )
                dpg.add_separator()
                dpg.add_text( 'reactions', color = self.color_muted )
                dpg.add_group( tag = 'sb_reactions' )

            dpg.set_primary_window( 'sb_root', True )
            dpg.setup_dearpygui()
            dpg.show_viewport()

            frame = 0
            interval = 1.0 / self.fps
            while dpg.is_dearpygui_running() and not self._stop.is_set():
                started = time.perf_counter()
                self._refresh()
                dpg.render_dearpygui_frame()
                frame += 1
                if max_frames is not None and frame >= max_frames:
                    break
                elapsed = time.perf_counter() - started
                if elapsed < interval:
                    time.sleep( interval - elapsed )
        finally:
            try:
                dpg.destroy_context()
            except Exception:
                pass

    def _refresh( self ):
        snap = self.hub.snapshot()

        self._refresh_headline( snap )

        if snap[ 'paused' ]:
            dpg.set_value( 'sb_active', 'PAUSED' )
            dpg.configure_item( 'sb_active', color = self.color_alert )
        elif snap[ 'active' ]:
            dpg.set_value( 'sb_active', 'ACTIVE' )
            dpg.configure_item( 'sb_active', color = self.color_ready )
        else:
            dpg.set_value( 'sb_active', 'INACTIVE' )
            dpg.configure_item( 'sb_active', color = self.color_muted )
        dpg.set_value( 'sb_cast', 'CASTING' if snap[ 'casting' ] else '' )

        # Color gates (rotation conditions + reaction readiness) share one throttle
        now = time.perf_counter()
        refresh_colors = snap[ 'active' ] and ( now - self._last_color_refresh ) >= self.color_interval
        if refresh_colors:
            self._last_color_refresh = now

        self._refresh_rotations( snap, refresh_colors )
        self._refresh_reactions( snap, refresh_colors )

    # ---------------------------------------------------------------- helpers

    def _cd_text( self, remaining ):
        """Cooldown label: empty when ready, compact number while cooling."""
        return '' if remaining == 0.0 else f'{remaining:4.1f}'

    def _cd_color( self, remaining, colors_ok ):
        """Name/cd color from cooldown + whether all known gates currently pass."""
        if remaining == 0.0 and colors_ok:
            return self.color_ready
        if remaining > 0.0:
            return self.color_cooldown
        return self.color_blocked

    def _gate_display( self, ok ):
        """Return (glyph, color) for one color-gate result (None = not yet sampled)."""
        if ok is None:
            return '?', self.color_muted
        if ok:
            return '+', self.color_ready
        return 'x', self.color_alert

    # ---------------------------------------------------------------- headline

    def _refresh_headline( self, snap ):
        last = snap[ 'last_ability' ]
        if last is not None:
            name, stamp = last
            dpg.set_value( 'sb_ability', name )
            dpg.set_value( 'sb_ability_t', stamp )

        # Bind/unbind the alert-border theme only on transitions (theme binding is not free per frame)
        flash = snap[ 'flash' ]
        if flash != self._flash_bound:
            dpg.bind_item_theme( 'sb_headline', self._flash_theme if flash else 0 )
            self._flash_bound = flash

    # ---------------------------------------------------------------- rotations

    def _refresh_rotations( self, snap, refresh_colors ):
        view = snap[ 'rotation_view' ]
        # Signature includes gate counts so a profile change that only alters color_check length rebuilds
        sig = tuple(
            ( rname, tuple( len( a.color_checks ) for a in rotation.actions ) )
            for rname, rotation in view
        )
        if sig != self._rotation_sig:
            self._rotation_sig = sig
            self._color_cache = {}
            dpg.delete_item( 'sb_rotations', children_only = True )
            for rname, rotation in view:
                dpg.add_text( rname, parent = 'sb_rotations', color = self.color_text )
                for i, action in enumerate( rotation.actions ):
                    with dpg.group( horizontal = True, parent = 'sb_rotations',
                                    tag = f'sb_rot_{rname}_{i}' ):
                        dpg.add_text( '', tag = f'sb_rot_{rname}_{i}_n', color = self.color_muted,
                                      indent = 12 )
                        dpg.add_text( '', tag = f'sb_rot_{rname}_{i}_cd', color = self.color_muted )
                        for g in range( len( action.color_checks ) ):
                            dpg.add_text( '', tag = f'sb_rot_{rname}_{i}_g{g}',
                                          color = self.color_muted )

        for rname, rotation in view:
            for i, action in enumerate( rotation.actions ):
                remaining = action.cooldown_remaining()
                colors_ok = True
                for g, cond in enumerate( action.color_checks ):
                    if refresh_colors:
                        self._color_cache[ id( cond ) ] = cond.passes()
                    ok = self._color_cache.get( id( cond ) )
                    if ok is not True:
                        colors_ok = False
                    glyph, gcolor = self._gate_display( ok )
                    tag_g = f'sb_rot_{rname}_{i}_g{g}'
                    dpg.set_value( tag_g, glyph )
                    dpg.configure_item( tag_g, color = gcolor )

                row_color = self._cd_color( remaining, colors_ok )
                dpg.set_value( f'sb_rot_{rname}_{i}_n', f"{action.name:<20}" )
                dpg.configure_item( f'sb_rot_{rname}_{i}_n', color = row_color )
                dpg.set_value( f'sb_rot_{rname}_{i}_cd', self._cd_text( remaining ) )
                dpg.configure_item( f'sb_rot_{rname}_{i}_cd', color = row_color )

    # --------------------------------------------------------------- reactions

    def _refresh_reactions( self, snap, refresh_colors ):
        view = snap[ 'reaction_view' ]
        history = snap[ 'reactions' ]
        sig = tuple(
            ( name, len( reaction.color_gates() ) )
            for name, reaction in view
        )
        if sig != self._reaction_sig:
            self._reaction_sig = sig
            dpg.delete_item( 'sb_reactions', children_only = True )
            for name, reaction in view:
                n_gates = len( reaction.color_gates() )
                with dpg.group( horizontal = True, parent = 'sb_reactions' ):
                    dpg.add_color_button( default_value = ( 40, 40, 40, 255 ), tag = f'sb_rx_{name}_c',
                                          width = 18, height = 18, no_border = True )
                    dpg.add_text( '', tag = f'sb_rx_{name}_n', color = self.color_muted )
                    dpg.add_text( '', tag = f'sb_rx_{name}_cd', color = self.color_muted )
                    for g in range( n_gates ):
                        dpg.add_text( '', tag = f'sb_rx_{name}_g{g}', color = self.color_muted )
                    dpg.add_text( '', tag = f'sb_rx_{name}_t', color = self.color_muted )

        for name, reaction in view:
            remaining = reaction.cooldown_remaining()
            colors_ok = True
            for g, gate in enumerate( reaction.color_gates() ):
                if refresh_colors:
                    self._color_cache[ id( gate ) ] = gate.color_passes()
                ok = self._color_cache.get( id( gate ) )
                if ok is not True:
                    colors_ok = False
                glyph, gcolor = self._gate_display( ok )
                tag_g = f'sb_rx_{name}_g{g}'
                dpg.set_value( tag_g, glyph )
                dpg.configure_item( tag_g, color = gcolor )

            row_color = self._cd_color( remaining, colors_ok )
            dpg.set_value( f'sb_rx_{name}_n', f"{name:<12}" )
            dpg.configure_item( f'sb_rx_{name}_n', color = row_color )
            dpg.set_value( f'sb_rx_{name}_cd', self._cd_text( remaining ) )
            dpg.configure_item( f'sb_rx_{name}_cd', color = row_color )

            row = history.get( name )
            if row and row[ 'time' ]:
                delta = row[ 'delta' ]
                if delta and delta != '--':
                    # hub stores e.g. "0.31s"; drop unit for a compact "+0.31"
                    d = delta[ :-1 ] if delta.endswith( 's' ) else delta
                    stamp = f"{row[ 'time' ]}  +{d}"
                else:
                    stamp = row[ 'time' ]
                dpg.set_value( f'sb_rx_{name}_t', stamp )
                dpg.configure_item( f'sb_rx_{name}_t', color = self.color_text )
            else:
                dpg.set_value( f'sb_rx_{name}_t', '—' )
                dpg.configure_item( f'sb_rx_{name}_t', color = self.color_muted )
            if row and row[ 'color' ] is not None:
                r, g, b = row[ 'color' ]
                dpg.set_value( f'sb_rx_{name}_c', ( r, g, b, 255 ) )
