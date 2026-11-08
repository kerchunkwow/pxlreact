"""
MM8 party status capture. The mm plugin binds it to Ctrl+Alt+<[mm8_capture] key> (mm.toml).

MM8 centers a party of 1-5 portraits, so every PC's UI shifts horizontally when the party size
changes while the vertical layout stays fixed. Capture scans fixed screen rows from mm.toml
[mm8_capture] left to right for runs of one color; the Nth run on each row belongs to PC N and
its center is the indicator pixel. The UI is scaled unevenly, so the same element can render 1 or
2 px wide (or with different edge colors) from one PC to the next; runs of a single color absorb
that where multi-pixel sequences do not.

    ready_y           ready_colors[0]   -> players.N.ready (color lists every ready_colors entry)
    healthbar_y_top   healthbar_color   -> players.N.healthbar_x
    selected_y        selected_color    -> players.N.selected, and players.N.portrait
                                           portrait_offset_y below it

Only the selected PC shows an aura, so capture taps TAB once per PC and records the aura after
each; TAB cycles the selection without ever re-selecting the current PC (which would open its
inventory) and ends where it started. The auras sorted left to right are PCs 1..N.

When the jewel and healthbar counts agree (1-5) and the TAB cycle yields one aura per PC,
mm_status.json is rewritten from scratch; otherwise it is left untouched. Either way a
click-through overlay outlines what was found for overlay_s seconds, and the spacing between PCs
is printed.

Capture from the normal game view with every PC ready and at full health.
"""

from collections import namedtuple
from pathlib import Path
import ctypes
import gc
import json
import sys
import threading
import time
import tkinter as tk
import tomllib

HERE = Path( __file__ ).resolve().parent
ROOT = HERE.parent.parent
if str( ROOT ) not in sys.path:
    sys.path.insert( 0, str( ROOT ) )

from ansi import *
from pxl_lib import PIXELS, colors_similar, rgb_to_hex, virtual_screen

CONFIG_PATH = HERE / "mm.toml"
STATUS_PATH = HERE / "mm_status.json"
MAX_PARTY = 5

# Overlay drawing. KEY_COLOR is the transparent color key and must not be used for drawing.
KEY_COLOR = "#010101"
BORDER_COLOR = "#000000"
BACKDROP_COLOR = "#000000"
MARK_COLOR = "#ff00ff"
LABEL_COLOR = "#ffffff"
SHADOW_COLOR = "#000000"
LABEL_FONT = ( "Consolas", 8 )
LABEL_GAP_PX = 2
TICK_PX = 4
HEALTH_BOX_PAD_PX = 2
WINDOW_MARGIN_PX = 48
OVERLAY_POLL_MS = 50

SWEEP_KEY = "tab"
SWEEP_POLL_S = 0.010
SAME_AURA_PX = 2

_INT_KEYS = ( "tolerance", "ready_y", "selected_y", "portrait_offset_y", "healthbar_y_top",
              "healthbar_y_bottom", "healthbar_min_chroma", "overlay_pad_px", "sweep_timeout_ms" )
_COLOR_KEYS = ( "selected_color", "healthbar_color", "health_moderate", "health_low" )

SM_CXSCREEN = 0
GA_ROOT = 2
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010

_user32 = ctypes.WinDLL( "user32" )
_user32.GetForegroundWindow.restype = ctypes.c_void_p
_user32.SetForegroundWindow.argtypes = [ ctypes.c_void_p ]
_user32.GetAncestor.argtypes = [ ctypes.c_void_p, ctypes.c_uint ]
_user32.GetAncestor.restype = ctypes.c_void_p
_user32.SetWindowPos.argtypes = [ ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, ctypes.c_uint ]
_user32.GetWindowLongPtrW.argtypes = [ ctypes.c_void_p, ctypes.c_int ]
_user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
_user32.SetWindowLongPtrW.argtypes = [ ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t ]
_user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t


class CaptureConfigError( Exception ):
    pass


def _hex( value ):
    text = str( value ).strip().lstrip( "#" )
    if len( text ) != 6:
        raise ValueError( f"bad color {value!r}" )
    return ( int( text[ 0:2 ], 16 ), int( text[ 2:4 ], 16 ), int( text[ 4:6 ], 16 ) )


def load_config():
    """Parsed mm.toml [mm8_capture]; raises CaptureConfigError when missing or invalid."""
    try:
        with open( CONFIG_PATH, "rb" ) as fh:
            raw = tomllib.load( fh ).get( "mm8_capture" )
    except ( OSError, tomllib.TOMLDecodeError ) as exc:
        raise CaptureConfigError( f"cannot read {CONFIG_PATH.name} ({exc})" )
    if not isinstance( raw, dict ):
        raise CaptureConfigError( f"missing [mm8_capture] in {CONFIG_PATH.name}" )
    try:
        cfg = { k: int( raw[ k ] ) for k in _INT_KEYS }
        cfg[ "key" ] = str( raw[ "key" ] ).strip().lower()
        cfg[ "status_key" ] = str( raw[ "status_key" ] ).strip().lower()
        for k in _COLOR_KEYS:
            cfg[ k ] = _hex( raw[ k ] )
        cfg[ "ready_colors" ] = [ _hex( c ) for c in raw[ "ready_colors" ] ]
        if not cfg[ "ready_colors" ]:
            raise ValueError( "ready_colors is empty" )
        cfg[ "ready_color" ] = cfg[ "ready_colors" ][ 0 ]
        cfg[ "overlay_s" ] = float( raw[ "overlay_s" ] )
        cfg[ "overlay_alpha" ] = float( raw[ "overlay_alpha" ] )
    except KeyError as exc:
        raise CaptureConfigError( f"[mm8_capture] is missing {exc.args[ 0 ]}" )
    except ( TypeError, ValueError ) as exc:
        raise CaptureConfigError( f"bad [mm8_capture] value ({exc})" )
    if cfg[ "healthbar_y_bottom" ] <= cfg[ "healthbar_y_top" ]:
        raise CaptureConfigError( "[mm8_capture] healthbar_y_bottom must be below healthbar_y_top" )
    return cfg


def find_runs( row, left, color, tolerance ):
    """
    Left-to-right runs of pixels similar to `color` in `row` (RGB pixels starting at screen x
    `left`). Returns [ ( center_x, span_left, span_right ) ] in screen x.
    """
    spans = []
    span = None
    for i, pixel in enumerate( row ):
        if not colors_similar( pixel, color, tolerance ):
            span = None
            continue
        if span is None:
            span = [ i, i ]
            spans.append( span )
        else:
            span[ 1 ] = i
    return [ ( left + ( a + b ) // 2, left + a, left + b ) for a, b in spans ]


def _grab_row( y ):
    """( left, pixels ) for screen row y across the primary monitor, or None on a failed grab."""
    width = int( _user32.GetSystemMetrics( SM_CXSCREEN ) ) or 2560
    grabbed = PIXELS.grab_region( 0, y, width, 1 )
    if grabbed is None or not grabbed[ 2 ]:
        return None
    return grabbed[ 0 ], grabbed[ 2 ][ 0 ]


def _deltas( xs ):
    return [ b - a for a, b in zip( xs, xs[ 1: ] ) ]


def _nums( values ):
    if not values:
        return "-"
    return " ".join( "?" if v is None else f"{MAGENTA}{v}{RESET}" for v in values )


# A rectangle in screen coordinates (l, t, r, b), an optional top-center label, magenta indicator
# segments (x0, y0, x1, y1) inclusive, and optional right-side ticks ( y, text, hex color ).
_Box = namedtuple( "_Box", "rect label marks ticks" )


class Mm8Capture:

    def __init__( self ):
        self.cfg = load_config()
        self._overlay = None

    @property
    def key( self ):
        return self.cfg[ "key" ]

    @property
    def status_key( self ):
        return self.cfg[ "status_key" ]

    def capture( self, tap ):
        """
        Detect, report, rewrite mm_status.json when consistent, and show the overlay.
        `tap( key_name )` injects a key press; the sweep uses it to cycle the selection.
        """
        try:
            self.cfg = load_config()
        except CaptureConfigError as exc:
            print( f"{RED}mm8: {exc}{RESET}" )
            return False
        cfg = self.cfg
        rows = {}
        for kind, y_key, color_key in ( ( "rdy", "ready_y", "ready_color" ),
                                        ( "hp", "healthbar_y_top", "healthbar_color" ),
                                        ( "sel", "selected_y", "selected_color" ) ):
            hits = self._scan( cfg[ y_key ], cfg[ color_key ] )
            if hits is None:
                print( f"{RED}mm8: capture failed (screen grab at y={cfg[ y_key ]}){RESET}" )
                return False
            rows[ kind ] = hits

        found = { "rdy": rows[ "rdy" ], "hp": rows[ "hp" ], "sel": rows[ "sel" ] }
        n = len( found[ "rdy" ] )
        if n != len( found[ "hp" ] ) or not 1 <= n <= MAX_PARTY:
            error = ( f"found rdy {n}, hp {len( found['hp'] )}; need the same count "
                      f"(1-{MAX_PARTY}) of each" )
        elif len( rows[ "sel" ] ) > 1:
            error = f"found {len( rows['sel'] )} selection auras before the sweep; need at most 1"
        else:
            found[ "sel" ], error = self._sweep( n, rows[ "sel" ], tap )

        if error is None:
            try:
                tmp = STATUS_PATH.with_suffix( ".json.tmp" )
                tmp.write_text( json.dumps( self._status( found ), indent = 2 ) + "\n",
                                encoding = "utf-8" )
                tmp.replace( STATUS_PATH )
            except OSError as exc:
                error = f"cannot write {STATUS_PATH} ({exc})"
        self._report( found, error )
        self._show( found )
        return error is None

    # ------------------------------------------------------------------------------ detection

    def _scan( self, y, color ):
        row = _grab_row( y )
        if row is None:
            return None
        return find_runs( row[ 1 ], row[ 0 ], color, self.cfg[ "tolerance" ] )

    def _sweep( self, n, initial, tap ):
        """
        ( distinct auras sorted left to right, error string or None ). `initial` holds the 0 or 1
        aura shown before the sweep. TAB is tapped n times, so the selection ends where it started.
        """
        auras = list( initial )
        if n == 1 and auras:
            return auras, None
        prev = auras[ 0 ][ 0 ] if auras else None
        for k in range( 1, n + 1 ):
            tap( SWEEP_KEY )
            hit = self._await_aura( prev )
            if hit is None:
                return sorted( auras ), ( f"no new selection aura within "
                                          f"{self.cfg['sweep_timeout_ms']} ms after TAB {k}/{n}" )
            if all( abs( hit[ 0 ] - a[ 0 ] ) > SAME_AURA_PX for a in auras ):
                auras.append( hit )
            prev = hit[ 0 ]
        auras.sort()
        if len( auras ) != n:
            return auras, f"TAB cycle showed {len( auras )} distinct aura(s) for {n} PC(s)"
        return auras, None

    def _await_aura( self, prev_x ):
        """
        The single aura away from prev_x (any, when None) once it holds still for two polls,
        or None on timeout.
        """
        cfg = self.cfg
        deadline = time.perf_counter() + cfg[ "sweep_timeout_ms" ] / 1000.0
        last = None
        while time.perf_counter() < deadline:
            time.sleep( SWEEP_POLL_S )
            hits = self._scan( cfg[ "selected_y" ], cfg[ "selected_color" ] )
            if ( hits is not None and len( hits ) == 1
                 and ( prev_x is None or abs( hits[ 0 ][ 0 ] - prev_x ) > SAME_AURA_PX ) ):
                if hits[ 0 ] == last:
                    return last
                last = hits[ 0 ]
            else:
                last = None
        return None

    # ------------------------------------------------------------------------------ results

    def _status( self, found ):
        cfg = self.cfg
        sel_y = cfg[ "selected_y" ]
        rdy_y = cfg[ "ready_y" ]
        players = {}
        for i, ( rdy, sel, hp ) in enumerate( zip( found[ "rdy" ], found[ "sel" ], found[ "hp" ] ), 1 ):
            players[ str( i ) ] = {
                "healthbar_x": hp[ 0 ],
                "selected": { "x": sel[ 0 ], "y": sel_y, "color": list( cfg[ "selected_color" ] ) },
                "portrait": { "x": sel[ 0 ], "y": sel_y + cfg[ "portrait_offset_y" ] },
                "ready": { "x": rdy[ 0 ], "y": rdy_y,
                           "color": [ list( c ) for c in cfg[ "ready_colors" ] ] },
            }
        return {
            "healthbar": {
                "y_top": cfg[ "healthbar_y_top" ],
                "y_bottom": cfg[ "healthbar_y_bottom" ],
                "width": 1,
                "fill_min_chroma": cfg[ "healthbar_min_chroma" ],
            },
            "players": players,
        }

    def _report( self, found, error ):
        cfg = self.cfg
        xs = { kind: [ hit[ 0 ] if hit else None for hit in hits ] for kind, hits in found.items() }
        if error is None:
            n = len( xs[ "sel" ] )
            por_y = cfg[ "selected_y" ] + cfg[ "portrait_offset_y" ]
            print( f"✅ {GREEN}mm8{RESET}: captured {MAGENTA}{n}{RESET} PC(s) -> "
                   f"{CYAN}{STATUS_PATH.name}{RESET}" )
            for i in range( n ):
                print( f"ℹ️   {CYAN}p{i + 1}{RESET}"
                       f"  sel {MAGENTA}{xs['sel'][i]},{cfg['selected_y']}{RESET}"
                       f"  por {MAGENTA}{xs['sel'][i]},{por_y}{RESET}"
                       f"  rdy {MAGENTA}{xs['rdy'][i]},{cfg['ready_y']}{RESET}"
                       f"  hp x {MAGENTA}{xs['hp'][i]}{RESET}" )
            rdy_off = [ r - s for r, s in zip( xs[ "rdy" ], xs[ "sel" ] ) ]
            hp_off = [ h - s for h, s in zip( xs[ "hp" ], xs[ "sel" ] ) ]
            print( f"ℹ️   offset from sel  rdy {_nums( rdy_off )}  hp {_nums( hp_off )}" )
        else:
            print( f"{RED}mm8: capture failed: {error}. {STATUS_PATH.name} unchanged{RESET}" )
            for kind in ( "rdy", "sel", "hp" ):
                print( f"ℹ️   {kind} x {_nums( xs[ kind ] )}" )
        sel_spacing = _deltas( xs[ "sel" ] ) if None not in xs[ "sel" ] else []
        print( f"ℹ️   spacing  rdy {_nums( _deltas( xs['rdy'] ) )}"
               f"  sel {_nums( sel_spacing )}  hp {_nums( _deltas( xs['hp'] ) )}" )

    def _show( self, found ):
        cfg = self.cfg
        marks = []
        for kind, y in ( ( "rdy", cfg[ "ready_y" ] ), ( "sel", cfg[ "selected_y" ] ) ):
            for i, hit in enumerate( found[ kind ], 1 ):
                if hit is not None:
                    marks.append( ( f"p{i} {kind}", *hit, y ) )
        py = cfg[ "selected_y" ] + cfg[ "portrait_offset_y" ]
        for i, hit in enumerate( found[ "sel" ], 1 ):
            if hit is not None:
                marks.append( ( f"p{i} por", hit[ 0 ], hit[ 0 ], hit[ 0 ], py ) )
        bars = [ ( None, *hit ) for hit in found[ "hp" ] ]
        self.show( marks, bars, cfg[ "healthbar_y_top" ], cfg[ "healthbar_y_bottom" ] )

    def show( self, marks, bars, top, bottom ):
        """
        Overlay labeled boxes for point elements `marks` [ ( label, cx, span_l, span_r, y ) ] and
        healthbar columns `bars` [ ( label or None, cx, span_l, span_r ) ] spanning top..bottom.
        """
        cfg = self.cfg
        pad = cfg[ "overlay_pad_px" ]
        boxes = []
        for label, cx, sl, sr, y in marks:
            boxes.append( _Box( ( sl - pad, y - pad, sr + pad, y + pad ), label,
                                [ ( cx, y, cx, y ) ], () ) )
        tick_colors = ( ( 100, cfg[ "healthbar_color" ] ),
                        ( 50, cfg[ "health_moderate" ] ),
                        ( 1, cfg[ "health_low" ] ) )
        for label, cx, sl, sr in bars:
            ticks = tuple( ( bottom - round( pct / 100.0 * ( bottom - top ) ), f"{pct}%",
                             rgb_to_hex( color ) ) for pct, color in tick_colors )
            boxes.append( _Box( ( sl - HEALTH_BOX_PAD_PX, top - 1, sr + HEALTH_BOX_PAD_PX, bottom + 1 ),
                                label, [ ( cx, top, cx, bottom ) ], ticks ) )
        if not boxes:
            return
        if self._overlay is not None:
            self._overlay.stop()
            self._overlay.join( 1.0 )
        self._overlay = _Overlay( boxes, cfg[ "overlay_s" ], cfg[ "overlay_alpha" ] )
        self._overlay.start()


class _Overlay:
    """
    Temporary click-through confirmation overlay on its own thread: a translucent backdrop layer
    under an opaque layer for borders, indicator marks, and labels. Closes after `seconds`.
    """

    def __init__( self, boxes, seconds, alpha ):
        self.boxes = boxes
        self.seconds = float( seconds )
        self.alpha = min( max( float( alpha ), 0.0 ), 1.0 )
        self._stop = threading.Event()
        self._thread = threading.Thread( target = self._run, name = "Mm8Overlay", daemon = True )

    def start( self ):
        self._thread.start()

    def stop( self ):
        self._stop.set()

    def join( self, timeout ):
        self._thread.join( timeout = timeout )

    def _bounds( self ):
        vs_l, vs_t, vs_w, vs_h = virtual_screen()
        left = max( vs_l, min( b.rect[ 0 ] for b in self.boxes ) - WINDOW_MARGIN_PX )
        top = max( vs_t, min( b.rect[ 1 ] for b in self.boxes ) - WINDOW_MARGIN_PX )
        right = min( vs_l + vs_w, max( b.rect[ 2 ] for b in self.boxes ) + WINDOW_MARGIN_PX )
        bottom = min( vs_t + vs_h, max( b.rect[ 3 ] for b in self.boxes ) + WINDOW_MARGIN_PX )
        return left, top, right - left, bottom - top

    def _layer( self, root, x, y, w, h, alpha ):
        win = tk.Toplevel( root )
        win.overrideredirect( True )
        win.configure( bg = KEY_COLOR )
        win.geometry( f"{w}x{h}+{x}+{y}" )
        win.attributes( "-transparentcolor", KEY_COLOR )
        win.attributes( "-alpha", alpha )
        win.attributes( "-topmost", True )
        canvas = tk.Canvas( win, width = w, height = h, bg = KEY_COLOR, highlightthickness = 0, bd = 0 )
        canvas.pack()
        return win, canvas

    @staticmethod
    def _click_through( win ):
        hwnd = _user32.GetAncestor( win.winfo_id(), GA_ROOT )
        if not hwnd:
            return
        style = _user32.GetWindowLongPtrW( hwnd, GWL_EXSTYLE )
        _user32.SetWindowLongPtrW( hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TRANSPARENT
                                   | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE )
        _user32.SetWindowPos( hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                              SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE )

    @staticmethod
    def _text( canvas, x, y, text, color, anchor ):
        canvas.create_text( x + 1, y + 1, text = text, fill = SHADOW_COLOR, font = LABEL_FONT,
                            anchor = anchor )
        canvas.create_text( x, y, text = text, fill = color, font = LABEL_FONT, anchor = anchor )

    def _draw( self, back, front, ox, oy ):
        for box in self.boxes:
            l, t, r, b = box.rect[ 0 ] - ox, box.rect[ 1 ] - oy, box.rect[ 2 ] - ox, box.rect[ 3 ] - oy
            back.create_rectangle( l, t, r, b, fill = BACKDROP_COLOR, outline = "" )
            front.create_rectangle( l, t, r, b, outline = BORDER_COLOR, width = 1 )
            for x0, y0, x1, y1 in box.marks:
                x0, y0, x1, y1 = x0 - ox, y0 - oy, x1 - ox, y1 - oy
                if y0 == y1:
                    front.create_line( x0, y0, x1 + 1, y0, fill = MARK_COLOR, width = 1 )
                else:
                    front.create_line( x0, y0, x0, y1 + 1, fill = MARK_COLOR, width = 1 )
            if box.label:
                self._text( front, ( l + r ) // 2, t - LABEL_GAP_PX, box.label, LABEL_COLOR, "s" )
            for y, text, color in box.ticks:
                y -= oy
                front.create_line( r + 1, y, r + 1 + TICK_PX, y, fill = color, width = 1 )
                self._text( front, r + TICK_PX + 3, y, text, color, "w" )

    def _run( self ):
        try:
            self._show()
        except Exception as exc:
            print( f"{RED}mm8: overlay failed ({exc}){RESET}" )
        # Tcl aborts the process if its interpreter is freed on another thread, so collect every
        # Tk object here rather than leaving cycles for the main thread's GC.
        gc.collect()

    def _show( self ):
        foreground = _user32.GetForegroundWindow()
        root = tk.Tk()
        try:
            root.withdraw()
            ox, oy, w, h = self._bounds()
            back, back_canvas = self._layer( root, ox, oy, w, h, self.alpha )
            front, front_canvas = self._layer( root, ox, oy, w, h, 1.0 )
            self._draw( back_canvas, front_canvas, ox, oy )
            root.update_idletasks()
            self._click_through( back )
            self._click_through( front )
            root.update()
            if foreground and _user32.GetForegroundWindow() != foreground:
                _user32.SetForegroundWindow( foreground )

            deadline = time.perf_counter() + self.seconds

            def poll():
                if self._stop.is_set() or time.perf_counter() >= deadline:
                    root.quit()
                    return
                root.after( OVERLAY_POLL_MS, poll )

            root.after( OVERLAY_POLL_MS, poll )
            root.mainloop()
        finally:
            try:
                root.destroy()
            except Exception:
                pass
