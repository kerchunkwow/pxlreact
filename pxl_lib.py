"""
pxl_lib.py provides various utility functions for use throughout PxlReact; these may eventually find
a home in their own class.
"""

import colorsys
import ctypes
import threading
import time
from ctypes import wintypes

from mss import MSS

from ansi import *

WPT = wintypes.POINT()


class PixelSource:
    """
    Shared screen-pixel reader backed by mss frame grabs.

    A GDI GetPixel round-trip costs ~2.8 ms on this machine, and an mss BitBlt grab costs the same
    ~2.8 ms REGARDLESS of region size - so one grab of the bounding region of every configured
    pixel serves the whole tick (poll loop, readiness checks, remapper fire checks, status bar) for
    the price of a single legacy read. Indexing pixels out of the grabbed BGRA buffer is
    sub-microsecond (never use mss's ScreenShot.pixel(); it builds a full nested pixel list).

    Thread safety: MSS instances are not shareable across threads, so each calling thread gets its
    own via thread-local storage; the cached frame (an immutable tuple) is swapped under a lock and
    read without one. Callers outside the registered region fall back to an uncached 1x1 grab, so
    discovery tools (pixel picker, Ctrl+P monitor) work with no registration.

    `max_age` sets how long a cached frame keeps serving reads: within one poll tick every consumer
    hits the cache, while the next tick (a full tick_interval later) grabs fresh.
    """

    def __init__( self, max_age = 0.010 ):
        self.max_age = max_age
        self._lock = threading.Lock()
        self._tls = threading.local()
        self._region = None     # mss monitor dict covering all registered points
        self._frame = None      # ( raw_bgra, width, left, top, grabbed_at )

    def _sct( self ):
        sct = getattr( self._tls, 'sct', None )
        if sct is None:
            sct = MSS()
            self._tls.sct = sct
        return sct

    def register_points( self, points, pad = 2 ):
        """
        (Re)declare every coordinate the app is configured to read; the cache region becomes their
        padded bounding box. Called at startup and after a profile reload. An empty list disables
        the cache (all reads fall back to 1x1 grabs).
        """
        with self._lock:
            if not points:
                self._region = None
            else:
                xs = [ p[ 0 ] for p in points ]
                ys = [ p[ 1 ] for p in points ]
                self._region = { 'left': min( xs ) - pad, 'top': min( ys ) - pad,
                                 'width': max( xs ) - min( xs ) + 1 + 2 * pad,
                                 'height': max( ys ) - min( ys ) + 1 + 2 * pad }
            self._frame = None

    def get( self, x, y ):
        """RGB at screen (x, y), or None on a failed grab."""
        region = self._region
        if ( region is not None
             and region[ 'left' ] <= x < region[ 'left' ] + region[ 'width' ]
             and region[ 'top' ] <= y < region[ 'top' ] + region[ 'height' ] ):
            return self._get_cached( x, y )
        return self._get_single( x, y )

    def _get_cached( self, x, y ):
        frame = self._frame
        if frame is None or ( time.perf_counter() - frame[ 4 ] ) > self.max_age:
            with self._lock:
                # Re-check under the lock; another thread may have refreshed while we waited
                frame = self._frame
                if frame is None or ( time.perf_counter() - frame[ 4 ] ) > self.max_age:
                    try:
                        shot = self._sct().grab( self._region )
                    except Exception:
                        print( f'{MAGENTA}\tbad grab for ({YELLOW}{x}{RESET}, {YELLOW}{y}{RESET})' )
                        return None
                    frame = ( shot.raw, shot.width, self._region[ 'left' ],
                              self._region[ 'top' ], time.perf_counter() )
                    self._frame = frame

        raw, width, left, top, _ = frame
        off = ( ( y - top ) * width + ( x - left ) ) * 4
        return raw[ off + 2 ], raw[ off + 1 ], raw[ off ]     # BGRA -> RGB

    def _get_single( self, x, y ):
        try:
            raw = self._sct().grab( { 'left': x, 'top': y, 'width': 1, 'height': 1 } ).raw
        except Exception:
            print( f'{MAGENTA}\tbad read at {YELLOW}{x}{RESET}, {YELLOW}{y}{RESET}' )
            return None
        return raw[ 2 ], raw[ 1 ], raw[ 0 ]


# Module-level singleton: every consumer reads through this, so the app can register its
# configured points once and all callers share the per-tick frame.
PIXELS = PixelSource()


def get_pixel_color( x, y ):
    """
    Retrieve the rgb values of the pixel at coordinates (x, y), served from the shared PixelSource
    frame cache (see PixelSource for the caching and thread-safety story).
    """
    return PIXELS.get( x, y )

def get_color_difference( c1, c2 ):
    """
    Calculate the sum of squared differences between two colors. Unrolled: this runs for every
    color comparison on the poll path and is ~4x faster than a generator over zip().
    """
    dr = c1[ 0 ] - c2[ 0 ]
    dg = c1[ 1 ] - c2[ 1 ]
    db = c1[ 2 ] - c2[ 2 ]
    return dr * dr + dg * dg + db * db

def colors_different( c1, c2, tolerance ):
    """
    True when the SSD of two colors exceeds `tolerance` (colors are "different enough").
    A tolerance of 0 means any deviation counts as different (exact-match semantics).
    """
    return get_color_difference( c1, c2 ) > tolerance

def colors_similar( c1, c2, tolerance ):
    """
    Return true if two colors are within `tolerance` of each other (similar enough to be the same).
    """
    return not colors_different( c1, c2, tolerance )

class CastLock:
    """
    Shared "a cast is in progress" gate, coordinating the reaction system and the keyboard remapper.

    When armed for a duration, `active()` returns True until it elapses. Both the remapper and the
    reaction poll loop refuse to fire while active, so a cast-time action cannot interrupt a
    cast-time reaction and vice versa. Either side arms the lock via `begin(cast_time)` immediately
    before sending its key. Thread-safe: armed and read from the reaction poll thread and the
    remapper thread. Arming never shortens an already-longer active cast.
    """

    def __init__( self ):
        self._until = 0.0
        self._lock = threading.Lock()

    def arm( self, duration ):
        with self._lock:
            self._until = max( self._until, time.perf_counter() + duration )

    def begin( self, duration ):
        """Arm the lock when `duration` > 0. Call immediately before sending input."""
        if duration > 0:
            self.arm( duration )

    def active( self ):
        with self._lock:
            return time.perf_counter() < self._until

    def blocked( self ):
        """True while a cast is in progress (same as active(); named for fire-path call sites)."""
        return self.active()


def matches_any( color, palette, tolerance ):
    """
    True when `color` is similar (within `tolerance`) to any color in `palette`.

    Used as a cheap allow-list test against an already-read pixel color; an empty or None palette
    yields False. Each comparison is a few integer ops, so testing a small palette every poll tick
    is negligible next to the GetPixel read that produced `color`.
    """
    if not palette:
        return False
    return any( colors_similar( color, ref, tolerance ) for ref in palette )


class ColorCondition:
    """
    A single pixel-color condition: the pixel at (px, py) must either match `color` (match = True)
    or differ from it (match = False), judged within this condition's own `tolerance` (0 = exact).
    `passes()` reads the pixel live and applies the test.

    Used to compose multi-layered checks where several conditions must all hold (logical AND).
    """

    __slots__ = ( 'px', 'py', 'color', 'match', 'tolerance' )

    def __init__( self, px, py, color, tolerance, match = True ):
        self.px = px
        self.py = py
        self.color = color
        self.tolerance = tolerance
        self.match = match

    def passes( self ):
        """
        Read the configured pixel and return whether the condition holds. A failed read (None)
        fails the condition, since we cannot confirm the required state.
        """
        observed = get_pixel_color( self.px, self.py )
        if observed is None:
            return False
        if self.match:
            return colors_similar( observed, self.color, self.tolerance )
        return colors_different( observed, self.color, self.tolerance )


def get_mouse_pos():
    """
    Get the current position of the mouse cursor.

    Returns:
        tuple[int, int]: The (x, y) screen coordinates of the mouse cursor.
    """
    ctypes.windll.user32.GetCursorPos( ctypes.byref( WPT ) )
    return WPT.x, WPT.y

def rgb_to_hex( rgb ):
    """Convert an RGB tuple to a hexadecimal color."""
    return "#{:02x}{:02x}{:02x}".format( *rgb )

def color_name( rgb ):
    """
    Rough human hue label for an RGB color (e.g. "green", "purple"), to make the in-game association
    obvious in logs - a green tint reads as poison, a purple tint as a curse, etc. Low-saturation or
    very dark/light colors collapse to grey/black/white.
    """
    r, g, b = ( c / 255.0 for c in rgb )
    h, s, v = colorsys.rgb_to_hsv( r, g, b )

    if v < 0.12:
        return "black"
    if s < 0.12:
        return "white" if v > 0.75 else "grey"

    deg = h * 360.0
    for limit, name in ( ( 15, "red" ), ( 45, "orange" ), ( 70, "yellow" ), ( 170, "green" ),
                         ( 200, "cyan" ), ( 260, "blue" ), ( 320, "purple" ), ( 360, "red" ) ):
        if deg < limit:
            return name
    return "red"

def color_swatch( rgb, width = 2 ):
    """Return a small block colored with `rgb` as its terminal background (truecolor)."""
    r, g, b = rgb
    return f"{bg_rgb( r, g, b )}{' ' * width}{RESET}"

def describe_color( rgb ):
    """
    One-line visual + textual description of a color: a truecolor swatch, the hue name, the RGB
    triple, and the hex code. Used by reporting to make logged colors recognizable at a glance.
    """
    r, g, b = rgb
    return ( f"{color_swatch( rgb )} {color_name( rgb ):>6} "
             f"({MAGENTA}{r:>3}{RESET}, {MAGENTA}{g:>3}{RESET}, {MAGENTA}{b:>3}{RESET}) "
             f"{CYAN}{rgb_to_hex( rgb )}{RESET}" )

def report_color_at( x, y, color = None ):
    """
    Print the RGB color value at the specified coordinates.

    Args:
        x (int): The x-coordinate of the pixel.
        y (int): The y-coordinate of the pixel.
        color (tuple[int, int, int] | None): A pre-read color to report; if None, the pixel is read.

    Output format: (x, y) - (r, g, b) - 0xdddddd - #dddddd
    """
    if color is None:
        color = get_pixel_color( x, y )
    if color is None:
        print( f"{RED}Error: Could not read pixel at ({x}, {y}){RESET}" )
        return
    
    r, g, b = color
    hex_int = f"0x{r:02x}{g:02x}{b:02x}"
    hex_hash = f"#{r:02x}{g:02x}{b:02x}"
    
    print( f"({MAGENTA}{x}{RESET}, {MAGENTA}{y}{RESET}) - "
           f"({MAGENTA}{r}{RESET}, {MAGENTA}{g}{RESET}, {MAGENTA}{b}{RESET}) - "
           f"{MAGENTA}{hex_int}{RESET} - {MAGENTA}{hex_hash}{RESET}" )

class PixelMonitor:
    """
    Toggleable background watcher for a single pixel, intended for screen discovery.

    On start it captures the current mouse position, waits an initial settle delay (so the cursor
    can be moved away and any hover state can clear), then polls that fixed coordinate and reports
    the color only when it changes - each distinct color is printed once, not on every poll.
    """

    def __init__( self, delay = 1.0, interval = 0.1 ):
        self.delay = delay
        self.interval = interval
        self._thread = None
        self._stop = threading.Event()
        self._x = None
        self._y = None
        self._last = None

    @property
    def active( self ):
        return self._thread is not None and self._thread.is_alive()

    def toggle( self ):
        """Start the monitor if stopped, otherwise stop it."""
        if self.active:
            self.stop()
        else:
            self.start()

    def start( self ):
        if self.active:
            return
        self._x, self._y = get_mouse_pos()
        self._last = None
        self._stop.clear()
        self._thread = threading.Thread( target = self._run, name = 'PixelMonitor', daemon = True )
        self._thread.start()
        print( f"👁️ {GREEN}pixel monitor on{RESET} ({MAGENTA}{self._x}{RESET}, {MAGENTA}{self._y}{RESET})" )

    def stop( self ):
        if not self.active:
            return
        self._stop.set()
        print( f"👁️ {YELLOW}pixel monitor off{RESET}" )

    def _run( self ):
        # Initial settle delay so the cursor can move away / hover states can clear
        if self._stop.wait( self.delay ):
            return
        while not self._stop.is_set():
            color = get_pixel_color( self._x, self._y )
            if color is not None and color != self._last:
                self._last = color
                report_color_at( self._x, self._y, color )
            self._stop.wait( self.interval )

