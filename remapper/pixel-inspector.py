"""
Magnifying pixel inspector overlay, opened from remapper on Ctrl+Alt+C.

Polls a square of screen pixels centered on the cursor and draws them magnified in a borderless
always-on-top window. Remapper owns Interception, so it forwards ESC / right-click into this
module; a GetAsyncKeyState fallback covers standalone runs.

The whole window is one DearPyGui drawlist, so drawing coordinates are window coordinates. Every
position (window size, status text, cell rectangles, the cursor highlight, the readout, and
mouse-to-cell lookup) comes from that one layout: the status row, then GRID_SIZE x GRID_SIZE cells
of CELL_PX, then the readout row.

Usage (from the repo root, venv active):
    .pxlenv\\Scripts\\python.exe remapper\\pixel-inspector.py
"""

from pathlib import Path
import ctypes
import json
import os
import sys
import threading
import time
import winreg

HERE = Path( __file__ ).resolve().parent
ROOT = HERE.parent
if str( ROOT ) not in sys.path:
    sys.path.insert( 0, str( ROOT ) )

import dearpygui.dearpygui as dpg

from ansi import *
from pxl_lib import PIXELS, color_name, get_mouse_pos, get_pixel_color, rgb_to_hex, virtual_screen

# ================================================================================================
# User configuration
# ================================================================================================

# Grid: GRID_SIZE x GRID_SIZE screen pixels centered on the cursor, each drawn CELL_PX square.
# GRID_SIZE must be odd and >= 3 so one cell is exactly the pixel under the cursor.
# The window is GRID_SIZE * CELL_PX wide; below ~120 px the readout text is clipped.
GRID_SIZE = 9
CELL_PX = 24            # >= 4

# Milliseconds between samples / redraws while live.
POLL_MS = 16

# Readout and status text. FONT_FACE is an installed Windows font family; FONT_WEIGHT is
# "normal" or "bold". Size is in points (converted with the system DPI).
FONT_FACE = "Consolas"
FONT_SIZE_PT = 8
FONT_WEIGHT = "normal"

# Colors, RGBA 0-255.
HIGHLIGHT_COLOR = ( 255, 0, 255, 255 )      # 1 px stroke on the cursor pixel while live
LIVE_COLOR = ( 120, 220, 120, 255 )
FROZEN_COLOR = ( 230, 200, 90, 255 )
COPIED_COLOR = ( 120, 220, 120, 255 )
TEXT_COLOR = ( 230, 230, 230, 255 )
MUTED_COLOR = ( 150, 150, 150, 255 )
BACKGROUND_COLOR = ( 18, 18, 20, 255 )
EMPTY_CELL_COLOR = ( 20, 20, 22, 255 )      # cells with no sample (off-desktop or before first read)

# ================================================================================================
# Internal constants (not tuning knobs)
# ================================================================================================

WINDOW_TITLE = "pxl-inspector"      # also how _noactivate finds the window handle
DEFAULT_WINDOW_POS = ( 40, 40 )     # used when the saved position file is missing or unreadable
POSITION_FILE = HERE / "pixel_inspector.json"
TEXT_PAD_PX = 2                     # gap above/below and left of each text row
COPIED_FLASH_S = 1.0                # how long the status row shows COPIED

# GetAsyncKeyState virtual-key codes (standalone input fallback and window drag).
VK_LBUTTON = 0x01
VK_RBUTTON = 0x02
VK_ESCAPE = 0x1B

# Extended window styles: never take focus, stay off the taskbar, stay on top.
GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020

# Clipboard: Unicode text format, movable global memory block.
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

FONT_REGISTRY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"

_user32 = ctypes.windll.user32


def _check_config():
    if not ( isinstance( GRID_SIZE, int ) and GRID_SIZE >= 3 and GRID_SIZE % 2 == 1 ):
        raise ValueError( f"GRID_SIZE must be an odd integer >= 3 (got {GRID_SIZE!r})" )
    if not ( isinstance( CELL_PX, int ) and CELL_PX >= 4 ):
        raise ValueError( f"CELL_PX must be an integer >= 4 (got {CELL_PX!r})" )
    if not ( isinstance( POLL_MS, int ) and POLL_MS >= 1 ):
        raise ValueError( f"POLL_MS must be a positive integer (got {POLL_MS!r})" )
    if FONT_WEIGHT not in ( "normal", "bold" ):
        raise ValueError( f'FONT_WEIGHT must be "normal" or "bold" (got {FONT_WEIGHT!r})' )
    if not ( isinstance( FONT_SIZE_PT, ( int, float ) ) and FONT_SIZE_PT > 0 ):
        raise ValueError( f"FONT_SIZE_PT must be positive (got {FONT_SIZE_PT!r})" )


_check_config()


def _key_down( vk ):
    return bool( _user32.GetAsyncKeyState( vk ) & 0x8000 )


def _font_px():
    """FONT_SIZE_PT in pixels at the system DPI."""
    try:
        dpi = int( _user32.GetDpiForSystem() ) or 96
    except Exception:
        dpi = 96
    return max( 6, round( FONT_SIZE_PT * dpi / 72 ) )


def _font_file():
    """Installed TTF/OTF path for FONT_FACE / FONT_WEIGHT, or None."""
    entry = f"{FONT_FACE} Bold" if FONT_WEIGHT == "bold" else FONT_FACE
    fonts_dir = Path( os.environ.get( "WINDIR", r"C:\Windows" ) ) / "Fonts"
    for hive in ( winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE ):
        try:
            key = winreg.OpenKey( hive, FONT_REGISTRY )
        except OSError:
            continue
        with key:
            for suffix in ( " (TrueType)", " (OpenType)" ):
                try:
                    value, _ = winreg.QueryValueEx( key, entry + suffix )
                except OSError:
                    continue
                path = Path( value )
                if not path.is_absolute():
                    path = fonts_dir / path
                if path.is_file():
                    return path
    return None


def _copy_text( text ):
    """Put Unicode text on the Windows clipboard. Returns True on success."""
    encoded = text.encode( "utf-16-le" ) + b"\x00\x00"
    kernel32 = ctypes.windll.kernel32
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ ctypes.c_void_p ]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ ctypes.c_void_p ]
    kernel32.GlobalFree.argtypes = [ ctypes.c_void_p ]
    user32 = ctypes.windll.user32
    user32.SetClipboardData.argtypes = [ ctypes.c_uint, ctypes.c_void_p ]
    user32.SetClipboardData.restype = ctypes.c_void_p

    if not user32.OpenClipboard( None ):
        return False
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc( GMEM_MOVEABLE, len( encoded ) )
        if not handle:
            return False
        locked = kernel32.GlobalLock( handle )
        ctypes.memmove( locked, encoded, len( encoded ) )
        kernel32.GlobalUnlock( handle )
        if not user32.SetClipboardData( CF_UNICODETEXT, handle ):
            kernel32.GlobalFree( handle )
            return False
        return True
    finally:
        user32.CloseClipboard()


def _pixel_payload( x, y, rgb ):
    r, g, b = ( int( c ) for c in rgb[ :3 ] )
    color = ( r, g, b )
    return {
        "x": int( x ),
        "y": int( y ),
        "hex": rgb_to_hex( color ),
        "rgb": [ r, g, b ],
        "name": color_name( color ),
    }


def _load_pos( path ):
    try:
        raw = json.loads( Path( path ).read_text( encoding = "utf-8" ) )
        return int( raw[ "x" ] ), int( raw[ "y" ] )
    except ( OSError, json.JSONDecodeError, KeyError, TypeError, ValueError ):
        return DEFAULT_WINDOW_POS


def _save_pos( path, x, y ):
    try:
        Path( path ).write_text(
            json.dumps( { "x": int( x ), "y": int( y ) }, indent = 2 ) + "\n",
            encoding = "utf-8" )
    except OSError as exc:
        print( f"{YELLOW}inspector: could not save position ({exc}){RESET}" )


def _noactivate( title ):
    """Keep the overlay from stealing foreground so remaps stay gated on the game."""
    _user32.FindWindowW.restype = ctypes.c_void_p
    hwnd = _user32.FindWindowW( None, title )
    if not hwnd:
        return
    if ctypes.sizeof( ctypes.c_void_p ) == 8:
        get_long = _user32.GetWindowLongPtrW
        set_long = _user32.SetWindowLongPtrW
        get_long.restype = ctypes.c_void_p
        set_long.restype = ctypes.c_void_p
        get_long.argtypes = [ ctypes.c_void_p, ctypes.c_int ]
        set_long.argtypes = [ ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p ]
    else:
        get_long = _user32.GetWindowLongW
        set_long = _user32.SetWindowLongW
    style = int( get_long( hwnd, GWL_EXSTYLE ) or 0 )
    set_long( hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST )
    _user32.SetWindowPos(
        hwnd, HWND_TOPMOST, 0, 0, 0, 0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_FRAMECHANGED )


class PixelInspector:
    """
    DearPyGui magnifier on a daemon thread. Remapper calls start/stop/handle_right_click;
    the render loop samples PIXELS.grab_region while unpaused and the cursor is outside the window.

    Cell (row, col) is screen pixel (cursor_x - radius + col, cursor_y - radius + row) at sample
    time, and is drawn at window (col * cell_px, grid_top + row * cell_px). Window coordinates
    plus the viewport position give screen coordinates (the viewport is undecorated, so its
    client origin is its position).
    """

    def __init__( self, pos_path = None, owned_input = False ):
        self.n = GRID_SIZE
        self.radius = GRID_SIZE // 2
        self.cell_px = CELL_PX
        self.poll_s = POLL_MS / 1000.0
        self.font_px = _font_px()
        self.label_h = self.font_px + 2 * TEXT_PAD_PX
        self.grid_px = self.n * self.cell_px
        self.grid_top = self.label_h
        self.info_top = self.grid_top + self.grid_px
        self.vp_w = self.grid_px
        self.vp_h = self.info_top + self.label_h
        self._pos_path = Path( pos_path ) if pos_path else POSITION_FILE
        self._owned_input = bool( owned_input )

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._paused = False
        self._samples = None
        self._vp_pos = ( 0, 0 )
        self._copied_until = 0.0
        self._copied_label = ""
        self._pending_copy = None
        self._drag_off = None
        self._prev_left = False
        self._prev_right = False
        self._prev_esc = False

    @property
    def active( self ):
        return self._thread is not None and self._thread.is_alive() and not self._stop.is_set()

    def start( self ):
        if self._thread and self._thread.is_alive():
            if not self._stop.is_set():
                return
            self._thread.join( timeout = 2.0 )
        self._stop.clear()
        with self._lock:
            self._paused = False
            self._samples = None
        self._drag_off = None
        self._copied_until = 0.0
        self._thread = threading.Thread( target = self._run, name = "PixelInspector", daemon = True )
        self._thread.start()
        print( f"ℹ️ {GREEN}inspector{RESET}: {CYAN}esc{RESET} quit; "
               f"right-click freeze; right-click cell copy" )

    def stop( self ):
        self._stop.set()

    def join( self, timeout = 2.0 ):
        if self._thread:
            self._thread.join( timeout = timeout )

    # ---------------------------------------------------------------- geometry

    def contains( self, x, y ):
        with self._lock:
            vx, vy = self._vp_pos
        return vx <= x < vx + self.vp_w and vy <= y < vy + self.vp_h

    def _rect_hits_overlay( self, left, top, width, height ):
        with self._lock:
            ox, oy = self._vp_pos
        return not ( left + width <= ox or ox + self.vp_w <= left
                     or top + height <= oy or oy + self.vp_h <= top )

    def _cell_at( self, x, y ):
        """(row, col) of the cell under screen point (x, y), or None outside the grid."""
        with self._lock:
            vx, vy = self._vp_pos
        lx = x - vx
        ly = y - vy - self.grid_top
        if not ( 0 <= lx < self.grid_px and 0 <= ly < self.grid_px ):
            return None
        return ly // self.cell_px, lx // self.cell_px

    def _cell_rect( self, row, col ):
        """Cell bounds in window coordinates."""
        x = col * self.cell_px
        y = self.grid_top + row * self.cell_px
        return ( x, y ), ( x + self.cell_px, y + self.cell_px )

    # ---------------------------------------------------------------- input

    def toggle_pause( self ):
        with self._lock:
            self._paused = not self._paused
            paused = self._paused
        return paused

    def handle_right_click( self ):
        """Freeze toggle outside the window; copy the indicated sample cell inside it."""
        x, y = get_mouse_pos()
        if not self.contains( x, y ):
            paused = self.toggle_pause()
            state = f"{YELLOW}frozen{RESET}" if paused else f"{GREEN}live{RESET}"
            print( f"ℹ️ {GREEN}inspector{RESET}: {state}" )
            return
        cell = self._cell_at( x, y )
        if cell is None:
            cell = ( self.radius, self.radius )
        self._copy_cell( *cell )

    def _copy_cell( self, row, col ):
        with self._lock:
            samples = self._samples
        if not samples:
            return
        sx, sy, rgb = samples[ row ][ col ]
        if rgb is None:
            return
        payload = _pixel_payload( sx, sy, rgb )
        self._pending_copy = json.dumps( payload, indent = 2 )
        self._copied_until = time.perf_counter() + COPIED_FLASH_S
        self._copied_label = f"{payload['x']},{payload['y']}  {payload['hex']}"

    # ---------------------------------------------------------------- sampling

    def _cells( self, origin_x, origin_y, lookup, prefer_get ):
        samples = []
        for row in range( self.n ):
            line = []
            for col in range( self.n ):
                sx = origin_x + col
                sy = origin_y + row
                rgb = None if prefer_get else lookup.get( ( sx, sy ) )
                if rgb is None:
                    rgb = get_pixel_color( sx, sy )
                line.append( ( sx, sy, rgb ) )
            samples.append( line )
        return samples

    def _sample( self ):
        cx, cy = get_mouse_pos()
        origin_x = cx - self.radius
        origin_y = cy - self.radius
        if self._rect_hits_overlay( origin_x, origin_y, self.n, self.n ):
            return
        with self._lock:
            if self._paused:
                return
        grabbed = PIXELS.grab_region( origin_x, origin_y, self.n, self.n )
        lookup = {}
        dead = True
        if grabbed is not None:
            gl, gt, rows = grabbed
            for ry, row in enumerate( rows ):
                for rx, rgb in enumerate( row ):
                    lookup[ ( gl + rx, gt + ry ) ] = rgb
                    if rgb != ( 0, 0, 0 ):
                        dead = False
        samples = self._cells( origin_x, origin_y, lookup, prefer_get = dead )
        with self._lock:
            if not self._paused:
                self._samples = samples

    def _info_for( self, samples, row, col ):
        if not samples:
            return "--"
        sx, sy, rgb = samples[ row ][ col ]
        if rgb is None:
            return f"{sx},{sy}  --"
        r, g, b = ( int( c ) for c in rgb[ :3 ] )
        return f"{sx},{sy}  {rgb_to_hex( ( r, g, b ) )}"

    # ---------------------------------------------------------------- window

    def _bind_font( self ):
        path = _font_file()
        if path is None:
            print( f"{YELLOW}inspector: font {CYAN}{FONT_FACE} {FONT_WEIGHT}{RESET}{YELLOW} "
                   f"not installed; using the DearPyGui default{RESET}" )
            return
        with dpg.font_registry():
            font = dpg.add_font( str( path ), self.font_px )
        dpg.bind_font( font )

    def _build( self, x, y ):
        with dpg.theme() as theme:
            with dpg.theme_component( dpg.mvAll ):
                dpg.add_theme_style( dpg.mvStyleVar_WindowPadding, 0, 0 )
                dpg.add_theme_style( dpg.mvStyleVar_ItemSpacing, 0, 0 )
                dpg.add_theme_style( dpg.mvStyleVar_WindowBorderSize, 0 )
                dpg.add_theme_style( dpg.mvStyleVar_ScrollbarSize, 0 )
                dpg.add_theme_style( dpg.mvStyleVar_WindowMinSize, self.vp_w, self.vp_h )
                dpg.add_theme_color( dpg.mvThemeCol_WindowBg, BACKGROUND_COLOR )
        dpg.bind_theme( theme )
        self._bind_font()

        dpg.create_viewport(
            title = WINDOW_TITLE,
            width = self.vp_w,
            height = self.vp_h,
            x_pos = x,
            y_pos = y,
            min_width = self.vp_w,
            max_width = self.vp_w,
            min_height = self.vp_h,
            max_height = self.vp_h,
            always_on_top = True,
            resizable = False,
            decorated = False,
            clear_color = [ c / 255.0 for c in BACKGROUND_COLOR ],
        )

        with dpg.window( tag = "pi_root", no_title_bar = True, no_scrollbar = True,
                         no_scroll_with_mouse = True, no_resize = True, no_move = True,
                         no_collapse = True, no_close = True,
                         width = self.vp_w, height = self.vp_h ):
            with dpg.drawlist( width = self.vp_w, height = self.vp_h, tag = "pi_canvas" ):
                dpg.draw_text( ( TEXT_PAD_PX, TEXT_PAD_PX ), "LIVE", color = LIVE_COLOR,
                               size = self.font_px, tag = "pi_status" )
                for row in range( self.n ):
                    for col in range( self.n ):
                        pmin, pmax = self._cell_rect( row, col )
                        dpg.draw_rectangle( pmin, pmax, color = ( 0, 0, 0, 0 ),
                                            fill = EMPTY_CELL_COLOR, thickness = 0,
                                            tag = f"pi_c_{row}_{col}" )
                pmin, pmax = self._cell_rect( self.radius, self.radius )
                dpg.draw_rectangle( pmin, pmax, color = HIGHLIGHT_COLOR,
                                    fill = ( 0, 0, 0, -255 ), thickness = 1,
                                    show = False, tag = "pi_mark" )
                dpg.draw_text( ( TEXT_PAD_PX, self.info_top + TEXT_PAD_PX ), "--",
                               color = TEXT_COLOR, size = self.font_px, tag = "pi_info" )

        dpg.set_primary_window( "pi_root", True )

        with dpg.handler_registry():
            dpg.add_mouse_click_handler( button = dpg.mvMouseButton_Left,
                                         callback = self._on_left_down )
            dpg.add_mouse_release_handler( button = dpg.mvMouseButton_Left,
                                           callback = self._on_left_up )

    def _run( self ):
        x, y = _load_pos( self._pos_path )
        vs_l, vs_t, vs_w, vs_h = virtual_screen()
        x = min( max( x, vs_l ), vs_l + vs_w - self.vp_w )
        y = min( max( y, vs_t ), vs_t + vs_h - self.vp_h )
        with self._lock:
            self._vp_pos = ( x, y )
        try:
            dpg.create_context()
            self._build( x, y )
            dpg.setup_dearpygui()
            dpg.show_viewport()
            dpg.set_viewport_width( self.vp_w )
            dpg.set_viewport_height( self.vp_h )
            _noactivate( WINDOW_TITLE )
            self._sample()

            while dpg.is_dearpygui_running() and not self._stop.is_set():
                started = time.perf_counter()
                self._poll_fallback_keys()
                self._tick_drag()
                self._sample()
                self._refresh()
                dpg.render_dearpygui_frame()
                elapsed = time.perf_counter() - started
                if elapsed < self.poll_s:
                    time.sleep( self.poll_s - elapsed )
        except Exception as exc:
            print( f"{RED}inspector failed: {exc}{RESET}" )
        finally:
            try:
                pos = dpg.get_viewport_pos()
                _save_pos( self._pos_path, pos[ 0 ], pos[ 1 ] )
            except Exception:
                pass
            try:
                dpg.destroy_context()
            except Exception:
                pass
            print( f"ℹ️ {YELLOW}inspector off{RESET}" )

    def _on_left_down( self, sender, app_data ):
        mx, my = get_mouse_pos()
        if not self.contains( mx, my ):
            return
        try:
            vx, vy = dpg.get_viewport_pos()
        except Exception:
            return
        self._drag_off = ( mx - vx, my - vy )

    def _on_left_up( self, sender, app_data ):
        self._drag_off = None

    def _tick_drag( self ):
        left = _key_down( VK_LBUTTON )
        mx, my = get_mouse_pos()
        if left and not self._prev_left and self.contains( mx, my ) and self._drag_off is None:
            try:
                vx, vy = dpg.get_viewport_pos()
                self._drag_off = ( mx - vx, my - vy )
            except Exception:
                pass
        if not left:
            self._drag_off = None
        elif self._drag_off is not None:
            try:
                dpg.set_viewport_pos( [ mx - self._drag_off[ 0 ], my - self._drag_off[ 1 ] ] )
            except Exception:
                pass
        self._prev_left = left

    def _poll_fallback_keys( self ):
        """Standalone ESC / right-click. Remapper swallows those strokes and calls us directly."""
        if self._owned_input:
            return
        esc = _key_down( VK_ESCAPE )
        if esc and not self._prev_esc:
            self.stop()
        self._prev_esc = esc
        rbtn = _key_down( VK_RBUTTON )
        if rbtn and not self._prev_right:
            self.handle_right_click()
        self._prev_right = rbtn

    def _mark( self, cell ):
        """Thin stroke on the live readout cell. Hidden while frozen."""
        if cell is None:
            dpg.configure_item( "pi_mark", show = False )
            return
        pmin, pmax = self._cell_rect( *cell )
        dpg.configure_item( "pi_mark", pmin = pmin, pmax = pmax, show = True )

    def _refresh( self ):
        pending = self._pending_copy
        if pending is not None:
            self._pending_copy = None
            if not _copy_text( pending ):
                print( f"{RED}inspector: clipboard copy failed{RESET}" )

        try:
            vp = dpg.get_viewport_pos()
        except Exception:
            return
        with self._lock:
            self._vp_pos = ( int( vp[ 0 ] ), int( vp[ 1 ] ) )
            samples = self._samples
            paused = self._paused

        if samples:
            for row in range( self.n ):
                for col in range( self.n ):
                    rgb = samples[ row ][ col ][ 2 ]
                    if rgb is None:
                        fill = EMPTY_CELL_COLOR
                    else:
                        fill = ( int( rgb[ 0 ] ), int( rgb[ 1 ] ), int( rgb[ 2 ] ), 255 )
                    dpg.configure_item( f"pi_c_{row}_{col}", fill = fill )

        mx, my = get_mouse_pos()
        hover = self._cell_at( mx, my )
        shown = hover if hover is not None else ( self.radius, self.radius )
        self._mark( None if paused else shown )

        if time.perf_counter() < self._copied_until:
            dpg.configure_item( "pi_status", text = "COPIED", color = COPIED_COLOR )
            dpg.configure_item( "pi_info", text = self._copied_label, color = COPIED_COLOR )
            return

        if paused:
            dpg.configure_item( "pi_status", text = "FROZEN", color = FROZEN_COLOR )
        else:
            dpg.configure_item( "pi_status", text = "LIVE", color = LIVE_COLOR )

        dpg.configure_item( "pi_info", text = self._info_for( samples, *shown ),
                            color = TEXT_COLOR if samples else MUTED_COLOR )


def run( pos_path = None ):
    inspector = PixelInspector( pos_path = pos_path )
    inspector.start()
    try:
        while inspector.active:
            time.sleep( 0.05 )
    except KeyboardInterrupt:
        inspector.stop()
    inspector.join()


if __name__ == "__main__":
    run()
