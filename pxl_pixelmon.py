"""
pxl_pixelmon.py - standalone pixel monitor (a GUI replacement for the terminal Ctrl+P pixel poll).

Run `python pxl_pixelmon.py` on its own (NOT while pxlreactHL.py is live - that app captures Ctrl+P
and F12 device-wide via Interception). This tool uses lightweight global key polling instead.

Flow:
- Ctrl+P captures the mouse coordinate, runs a short settle countdown (move the cursor off the
  pixel), then samples that pixel's reference color and begins monitoring it.
- Monitoring polls the fixed coordinate only while pxl_wincheck reports the target app active.
- When the pixel diverges from its ORIGINAL reference by more than the (UI-editable) tolerance, a
  screen-clamped screenshot centered on the pixel is saved to ./polled_pixel with a correlated log
  line; the "current" swatch updates to the new color. Re-press Ctrl+P to pick a new pixel.
- F12 quits.

The GUI (DearPyGui) render loop runs on the main thread; a single daemon controller thread does the
hotkey polling, settle countdown, wincheck-gated monitoring, and capture. They share a small
lock-guarded MonitorState.
"""

import ctypes
import os
import threading
import time

import mss
import mss.tools
import dearpygui.dearpygui as dpg

from pxl_config import get_settings, load_profile
from pxl_lib import ( get_pixel_color, get_mouse_pos, get_color_difference, colors_different,
                      rgb_to_hex, color_name )
from pxl_wincheck import PxlWinCheck
from ansi import *

# Capture region (pixels), centered on the monitored pixel and clamped to the screen
CAPTURE_W = 400
CAPTURE_H = 600

SETTLE_DELAY = 3.0          # seconds between Ctrl+P and reference sampling (move cursor away)
LOOP_INTERVAL = 0.05        # controller loop period; hotkey + pixel poll cadence

OUTPUT_DIR = "polled_pixel"
LOG_NAME = "monitor_log.txt"

# Virtual-key codes for global hotkey polling (GetAsyncKeyState)
VK_CONTROL = 0x11
VK_P = 0x50
VK_F12 = 0x7B

_user32 = ctypes.windll.user32

# DPG colors
GREEN_C = ( 120, 220, 120, 255 )
YELLOW_C = ( 230, 200, 90, 255 )
RED_C = ( 235, 110, 110, 255 )
GREY_C = ( 150, 150, 150, 255 )
WHITE_C = ( 230, 230, 230, 255 )


def _screen_size():
    """Primary screen size via ctypes; falls back to 1440p if the query fails."""
    try:
        w = _user32.GetSystemMetrics( 0 )   # SM_CXSCREEN
        h = _user32.GetSystemMetrics( 1 )   # SM_CYSCREEN
        if w > 0 and h > 0:
            return w, h
    except Exception:
        pass
    return 2560, 1440


def _clamp_box( cx, cy, w, h, sw, sh ):
    """
    Box of size (w, h) centered on (cx, cy), clamped to the screen: the top-left is pushed to
    non-negative and the width/height are shrunk so the box never runs off the right/bottom edge.
    Returns ( left, top, width, height ), all within [0, sw) x [0, sh).
    """
    left = max( 0, cx - w // 2 )
    top = max( 0, cy - h // 2 )
    width = max( 1, min( w, sw - left ) )
    height = max( 1, min( h, sh - top ) )
    return left, top, width, height


def _key_down( vk ):
    return bool( _user32.GetAsyncKeyState( vk ) & 0x8000 )


def _clear_output_files():
    """
    Delete regular files in OUTPUT_DIR only. Never removes directories, never follows
    paths outside that folder (guards against OUTPUT_DIR becoming '.', '..', or a path).
    """
    if ( not OUTPUT_DIR or OUTPUT_DIR in ( '.', '..' )
         or os.sep in OUTPUT_DIR or '/' in OUTPUT_DIR or '\\' in OUTPUT_DIR ):
        raise ValueError( f"refusing to clear unsafe OUTPUT_DIR={OUTPUT_DIR!r}" )

    abs_dir = os.path.abspath( OUTPUT_DIR )
    if os.path.basename( abs_dir ) != OUTPUT_DIR:
        raise ValueError( f"refusing to clear {abs_dir!r}" )

    os.makedirs( abs_dir, exist_ok = True )
    real_dir = os.path.realpath( abs_dir )
    removed = 0
    for entry in os.scandir( abs_dir ):
        if not entry.is_file( follow_symlinks = False ):
            continue
        real = os.path.realpath( entry.path )
        if os.path.dirname( real ) != real_dir:
            continue
        try:
            os.remove( entry.path )
            removed += 1
        except OSError as exc:
            print( f"{RED}could not remove {CYAN}{entry.name}{RESET}: {exc}" )
    if removed:
        print( f"🗑️ cleared {MAGENTA}{removed}{RESET} file(s) from {CYAN}{OUTPUT_DIR}/{RESET}" )


class MonitorState:
    """Lock-guarded state shared between the controller thread and the DPG render loop."""

    def __init__( self, tolerance ):
        self._lock = threading.Lock()
        self.phase = 'idle'         # 'idle' | 'settling' | 'monitoring'
        self.active = False         # wincheck.check() result
        self.coord = None           # ( x, y )
        self.reference = None       # reference rgb tuple
        self.current = None         # last divergent rgb tuple
        self.ssd = 0                # SSD of last divergent color vs reference
        self.countdown = 0          # whole seconds remaining in the settle phase
        self.event_count = 0
        self.last_file = ''
        self.tolerance = tolerance  # owned by the UI; read by the controller each poll

    def set_tolerance( self, value ):
        with self._lock:
            self.tolerance = max( 0, int( value ) )

    def get_tolerance( self ):
        with self._lock:
            return self.tolerance

    def update( self, **fields ):
        with self._lock:
            for key, value in fields.items():
                setattr( self, key, value )

    def snapshot( self ):
        with self._lock:
            return {
                'phase': self.phase,
                'active': self.active,
                'coord': self.coord,
                'reference': self.reference,
                'current': self.current,
                'ssd': self.ssd,
                'countdown': self.countdown,
                'event_count': self.event_count,
                'last_file': self.last_file,
            }


class PixelMonitorController:
    """
    Owns the monitoring worker: global hotkey polling, the settle countdown, the wincheck-gated
    poll of the selected pixel, and divergence capture. Runs on one daemon thread.
    """

    def __init__( self, state ):
        self.state = state
        self.wincheck = PxlWinCheck( load_profile()[ 'wincheck' ] )
        self.screen = _screen_size()

        self._stop = threading.Event()
        self._thread = None

        # Edge-detection previous states for the global hotkeys
        self._prev_ctrl_p = False
        self._prev_f12 = False

        # Per-arm monitoring state (controller-thread private)
        self._coord = None
        self._reference = None
        self._settle_until = 0.0
        self._event_index = 0

        # Output paths + a dedicated mss instance (this thread only)
        _clear_output_files()
        self._log_path = os.path.join( OUTPUT_DIR, LOG_NAME )
        self._sct = None

    # ----------------------------------------------------------------- lifecycle

    def start( self ):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread( target = self._run, name = 'PixelMonitor', daemon = True )
        self._thread.start()

    def stop( self ):
        self._stop.set()
        if self._thread:
            self._thread.join( timeout = 2.0 )

    @property
    def stopped( self ):
        return self._stop.is_set()

    # -------------------------------------------------------------------- worker

    def _run( self ):
        self._sct = mss.MSS()
        try:
            while not self._stop.is_set():
                self._poll_hotkeys()
                active = self.wincheck.check()

                if self.state.phase == 'settling':
                    self._tick_settle( active )
                elif self.state.phase == 'monitoring':
                    self._tick_monitor( active )
                else:
                    self.state.update( active = active )

                time.sleep( LOOP_INTERVAL )
        finally:
            try:
                self._sct.close()
            except Exception:
                pass

    def _poll_hotkeys( self ):
        ctrl_p = _key_down( VK_CONTROL ) and _key_down( VK_P )
        if ctrl_p and not self._prev_ctrl_p:
            self._arm()
        self._prev_ctrl_p = ctrl_p

        f12 = _key_down( VK_F12 )
        if f12 and not self._prev_f12:
            print( f"⏹️ {RED}quit hotkey{RESET}" )
            self._stop.set()
        self._prev_f12 = f12

    def _arm( self ):
        """Ctrl+P: capture the cursor coordinate now and begin the settle countdown."""
        x, y = get_mouse_pos()
        self._coord = ( x, y )
        self._reference = None
        self._settle_until = time.perf_counter() + SETTLE_DELAY
        self.state.update( phase = 'settling', coord = ( x, y ), reference = None, current = None,
                           ssd = 0, countdown = int( round( SETTLE_DELAY ) ) )
        print( f"👁️ {GREEN}armed{RESET} at ({MAGENTA}{x}{RESET}, {MAGENTA}{y}{RESET}); "
               f"move the cursor away ({MAGENTA}{SETTLE_DELAY:.0f}{RESET}s)" )

    def _tick_settle( self, active ):
        remaining = self._settle_until - time.perf_counter()
        if remaining > 0:
            self.state.update( active = active, countdown = int( remaining ) + 1 )
            return

        # Delay elapsed: sample the reference color (cursor should now be clear of the pixel)
        ref = get_pixel_color( *self._coord )
        self._reference = ref
        self.state.update( phase = 'monitoring', active = active, reference = ref,
                           current = None, ssd = 0, countdown = 0 )
        if ref is not None:
            print( f"👁️ {GREEN}monitoring{RESET} ({MAGENTA}{self._coord[0]}{RESET}, "
                   f"{MAGENTA}{self._coord[1]}{RESET}) ref {CYAN}{rgb_to_hex( ref )}{RESET}" )

    def _tick_monitor( self, active ):
        self.state.update( active = active )
        if not active or self._reference is None:
            return

        observed = get_pixel_color( *self._coord )
        if observed is None:
            return

        tol = self.state.get_tolerance()
        if not colors_different( observed, self._reference, tol ):
            return

        ssd = get_color_difference( observed, self._reference )
        filename = self._capture( observed, ssd )
        self.state.update( current = observed, ssd = ssd,
                           event_count = self._event_index, last_file = filename )

    # ------------------------------------------------------------- capture / log

    def _capture( self, observed, ssd ):
        """Save a clamped screenshot centered on the pixel and append a correlated log line."""
        self._event_index += 1
        idx = self._event_index
        x, y = self._coord
        r, g, b = observed

        left, top, width, height = _clamp_box( x, y, CAPTURE_W, CAPTURE_H, *self.screen )
        now = time.time()
        stamp = time.strftime( "%Y%m%d_%H%M%S", time.localtime( now ) ) + f"_{int( now * 1000 ) % 1000:03d}"
        filename = f"{idx:04d}_{stamp}_{r}-{g}-{b}.png"
        path = os.path.join( OUTPUT_DIR, filename )

        try:
            img = self._sct.grab( { 'left': left, 'top': top, 'width': width, 'height': height } )
            mss.tools.to_png( img.rgb, img.size, output = path )
        except Exception as exc:
            print( f"{RED}capture failed: {exc}{RESET}" )
            filename = f"(save failed) {filename}"

        rr, gr, br = self._reference
        clock = time.strftime( "%H:%M:%S", time.localtime( now ) ) + f".{int( now * 1000 ) % 1000:03d}"
        line = ( f"[{idx:04d}] {clock} ({x},{y}) ref ({rr},{gr},{br}) -> "
                 f"({r},{g},{b}) ssd={ssd}  file={filename}\n" )
        try:
            with open( self._log_path, "a", encoding = "utf-8" ) as fh:
                fh.write( line )
        except OSError as exc:
            print( f"{RED}log write failed: {exc}{RESET}" )

        print( f"📸 {YELLOW}[{idx:04d}]{RESET} {color_name( observed )} "
               f"{CYAN}{rgb_to_hex( observed )}{RESET} ssd {MAGENTA}{ssd}{RESET} -> {CYAN}{filename}{RESET}" )
        return filename


class PixelMonitorGUI:
    """DearPyGui window that mirrors MonitorState each frame (render loop on the main thread)."""

    def __init__( self, state, controller ):
        self.state = state
        self.controller = controller

    def _on_tolerance( self, sender, app_data ):
        self.state.set_tolerance( app_data )

    def build( self ):
        with dpg.window( tag = 'pm_root' ):
            with dpg.group( horizontal = True ):
                dpg.add_text( 'INACTIVE', tag = 'pm_active', color = GREY_C )
                dpg.add_text( '', tag = 'pm_phase', color = GREY_C )

            dpg.add_separator()
            dpg.add_text( 'no pixel selected', tag = 'pm_coord', color = WHITE_C )
            dpg.add_input_int( label = 'tolerance', tag = 'pm_tol',
                               default_value = self.state.get_tolerance(),
                               min_value = 0, min_clamped = True, step = 100, width = 160,
                               callback = self._on_tolerance )

            dpg.add_separator()
            with dpg.group( horizontal = True ):
                with dpg.group():
                    dpg.add_text( 'sampled', color = GREY_C )
                    dpg.add_color_button( default_value = ( 40, 40, 40, 255 ), tag = 'pm_ref_sw',
                                          width = 140, height = 110, no_border = True )
                    dpg.add_text( '--', tag = 'pm_ref_txt', color = WHITE_C )
                with dpg.group():
                    dpg.add_text( 'current', color = GREY_C )
                    dpg.add_color_button( default_value = ( 40, 40, 40, 255 ), tag = 'pm_cur_sw',
                                          width = 140, height = 110, no_border = True )
                    dpg.add_text( '--', tag = 'pm_cur_txt', color = WHITE_C )
            dpg.add_text( '', tag = 'pm_ssd', color = GREY_C )

            dpg.add_separator()
            dpg.add_text( 'events: 0', tag = 'pm_events', color = WHITE_C )
            dpg.add_text( '', tag = 'pm_lastfile', color = GREY_C )
            dpg.add_text( 'Ctrl+P sample  \u00b7  F12 quit', color = GREY_C )

        dpg.set_primary_window( 'pm_root', True )

    def refresh( self ):
        snap = self.state.snapshot()

        if snap[ 'active' ]:
            dpg.set_value( 'pm_active', 'ACTIVE' )
            dpg.configure_item( 'pm_active', color = GREEN_C )
        else:
            dpg.set_value( 'pm_active', 'INACTIVE' )
            dpg.configure_item( 'pm_active', color = GREY_C )

        phase = snap[ 'phase' ]
        if phase == 'settling':
            dpg.set_value( 'pm_phase', f"settling {snap[ 'countdown' ]}..." )
            dpg.configure_item( 'pm_phase', color = YELLOW_C )
        elif phase == 'monitoring':
            dpg.set_value( 'pm_phase', 'monitoring' )
            dpg.configure_item( 'pm_phase', color = GREEN_C )
        else:
            dpg.set_value( 'pm_phase', 'idle' )
            dpg.configure_item( 'pm_phase', color = GREY_C )

        coord = snap[ 'coord' ]
        dpg.set_value( 'pm_coord', f"pixel ({coord[0]}, {coord[1]})" if coord else 'no pixel selected' )

        ref = snap[ 'reference' ]
        if ref is not None:
            dpg.set_value( 'pm_ref_sw', ( *ref, 255 ) )
            dpg.set_value( 'pm_ref_txt', f"{ref}  {rgb_to_hex( ref )}" )
        else:
            dpg.set_value( 'pm_ref_sw', ( 40, 40, 40, 255 ) )
            dpg.set_value( 'pm_ref_txt', '--' )

        cur = snap[ 'current' ]
        if cur is not None:
            dpg.set_value( 'pm_cur_sw', ( *cur, 255 ) )
            dpg.set_value( 'pm_cur_txt', f"{cur}  {rgb_to_hex( cur )}" )
            dpg.set_value( 'pm_ssd', f"ssd {snap[ 'ssd' ]} (vs reference)" )
        else:
            dpg.set_value( 'pm_cur_sw', ( 40, 40, 40, 255 ) )
            dpg.set_value( 'pm_cur_txt', '--' )
            dpg.set_value( 'pm_ssd', '' )

        dpg.set_value( 'pm_events', f"events: {snap[ 'event_count' ]}" )
        if snap[ 'last_file' ]:
            dpg.set_value( 'pm_lastfile', f"last: {snap[ 'last_file' ]}" )


def run( max_frames = None ):
    tolerance = get_settings()[ 'color' ][ 'default_tolerance' ]
    state = MonitorState( tolerance )
    controller = PixelMonitorController( state )
    gui = PixelMonitorGUI( state, controller )

    dpg.create_context()
    dpg.create_viewport( title = 'pxlreact pixel monitor', width = 340, height = 430,
                         always_on_top = True, resizable = True )
    gui.build()
    dpg.setup_dearpygui()
    dpg.show_viewport()

    controller.start()
    try:
        frame = 0
        while dpg.is_dearpygui_running() and not controller.stopped:
            gui.refresh()
            dpg.render_dearpygui_frame()
            frame += 1
            if max_frames is not None and frame >= max_frames:
                break
    finally:
        controller.stop()
        try:
            dpg.destroy_context()
        except Exception:
            pass


if __name__ == "__main__":
    run()
