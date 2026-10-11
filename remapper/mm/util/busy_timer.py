"""
Standalone MM recovery monitor (busy timer).

Polls each party member's readiness bubble from remapper/mm/mm_status.json. A cycle is the
time the pixel matches the stored busy (gray) color until it does not. Prints who flipped and
the duration in tenths of a millisecond, plus a running min/mean/max for that player.

Not imported by remapper or bookcast. Absent unless you run this file.

Usage (repo root, venv; can run alongside remapper):
    .pxlenv\\Scripts\\python.exe remapper\\mm\\util\\busy_timer.py

Esc quits and prints a per-player summary.

Optional remapper/mm/mm.toml table (bookcast ignores it):

    [busy_timer]
    poll_ms = 3
    confirm = 2
"""

from pathlib import Path
import ctypes
import json
import sys
import time
import tomllib

HERE = Path( __file__ ).resolve().parent
MM = HERE.parent
ROOT = MM.parent.parent
sys.path.insert( 0, str( ROOT ) )

from pxl_config import get_settings

get_settings( str( ROOT / "settings.toml" ) )

from ansi import *
from pxl_lib import PIXELS, colors_similar, get_pixel_color

STATUS_PATH = MM / "mm_status.json"
CONFIG_PATH = MM / "mm.toml"

VK_ESCAPE = 0x1B
_user32 = ctypes.windll.user32

DEFAULT_POLL_MS = 3
DEFAULT_CONFIRM = 2


def _down( vk ):
    return bool( _user32.GetAsyncKeyState( vk ) & 0x8000 )


def _rgb( raw ):
    return ( int( raw[ 0 ] ), int( raw[ 1 ] ), int( raw[ 2 ] ) )


def _load_busy():
    if not STATUS_PATH.is_file():
        print( f"{RED}Error: missing {CYAN}{STATUS_PATH}{RESET}" )
        return None
    try:
        raw = json.loads( STATUS_PATH.read_text( encoding = "utf-8" ) )
    except ( OSError, json.JSONDecodeError ) as exc:
        print( f"{RED}Error: cannot read {CYAN}{STATUS_PATH}{RESET} ({exc})" )
        return None
    players = raw.get( "players" ) if isinstance( raw, dict ) else None
    if not isinstance( players, dict ):
        print( f"{RED}Error: {CYAN}{STATUS_PATH}{RESET} has no players object" )
        return None
    out = {}
    for n in ( "1", "2", "3", "4" ):
        p = players.get( n )
        marker = p.get( "busy" ) if isinstance( p, dict ) else None
        if not isinstance( marker, dict ):
            continue
        try:
            out[ n ] = {
                "x": int( marker[ "x" ] ),
                "y": int( marker[ "y" ] ),
                "color": _rgb( marker[ "color" ] ),
            }
        except ( KeyError, TypeError, ValueError, IndexError ):
            print( f"{YELLOW}Warning: bad busy entry {CYAN}{n}{RESET} in "
                   f"{CYAN}{STATUS_PATH}{RESET}" )
    return out


def _load_timer_config():
    tol = get_settings()[ "color" ][ "default_tolerance" ]
    poll_ms = DEFAULT_POLL_MS
    confirm = DEFAULT_CONFIRM
    if not CONFIG_PATH.is_file():
        return tol, poll_ms, confirm
    try:
        with open( CONFIG_PATH, "rb" ) as fh:
            raw = tomllib.load( fh )
    except ( OSError, tomllib.TOMLDecodeError ) as exc:
        print( f"{YELLOW}Warning: cannot read {CYAN}{CONFIG_PATH}{RESET} ({exc})" )
        return tol, poll_ms, confirm
    if "unselected_tolerance" in raw:
        tol = int( raw[ "unselected_tolerance" ] )
    timer = raw.get( "busy_timer" )
    if isinstance( timer, dict ):
        if "tolerance" in timer:
            tol = int( timer[ "tolerance" ] )
        if "poll_ms" in timer:
            poll_ms = float( timer[ "poll_ms" ] )
        if "confirm" in timer:
            confirm = int( timer[ "confirm" ] )
    if poll_ms < 0:
        poll_ms = 0
    if confirm < 1:
        confirm = 1
    return tol, poll_ms, confirm


def _mean( values ):
    return sum( values ) / len( values )


def _print_duration( n, ms, hist ):
    extra = ""
    if hist:
        extra = ( f"  n={MAGENTA}{len( hist )}{RESET}"
                  f"  min {MAGENTA}{min( hist ):.1f}{RESET}"
                  f"  mean {MAGENTA}{_mean( hist ):.1f}{RESET}"
                  f"  max {MAGENTA}{max( hist ):.1f}{RESET}" )
    print( f"ℹ️ {CYAN}{n}{RESET}  {MAGENTA}{ms:.1f}{RESET} ms{extra}" )


def _print_summary( tracks ):
    print( f"ℹ️ {GREEN}mm timer{RESET}: summary" )
    for n, tr in tracks.items():
        hist = tr[ "hist" ]
        if not hist:
            print( f"ℹ️ {CYAN}{n}{RESET}  no samples" )
            continue
        print( f"ℹ️ {CYAN}{n}{RESET}  n={MAGENTA}{len( hist )}{RESET}"
               f"  min {MAGENTA}{min( hist ):.1f}{RESET}"
               f"  mean {MAGENTA}{_mean( hist ):.1f}{RESET}"
               f"  max {MAGENTA}{max( hist ):.1f}{RESET} ms" )


def _snapshot( markers ):
    # Drop the cached grab so all four busy pixels share one fresh frame at `now`.
    PIXELS._frame = None
    now = time.perf_counter()
    observed = {}
    for n, marker in markers.items():
        observed[ n ] = get_pixel_color( marker[ "x" ], marker[ "y" ] )
    return now, observed


def main():
    markers = _load_busy()
    if markers is None:
        return 1
    if not markers:
        print( f"{RED}Error: no busy pixels in {CYAN}{STATUS_PATH}{RESET}; "
               f"run {CYAN}capture-busy.py{RESET}" )
        return 1

    tol, poll_ms, confirm = _load_timer_config()
    PIXELS.register_points( [ ( m[ "x" ], m[ "y" ] ) for m in markers.values() ] )
    PIXELS.max_age = 1.0

    tracks = {}
    for n in markers:
        tracks[ n ] = {
            "busy": None,
            "pending": None,
            "pending_n": 0,
            "t_busy": None,
            "hist": [],
        }

    names = " ".join( f"{CYAN}{n}{RESET}" for n in tracks )
    print( f"ℹ️ {GREEN}mm timer{RESET}: players {names}  "
           f"poll {MAGENTA}{poll_ms}{RESET} ms  confirm {MAGENTA}{confirm}{RESET}  "
           f"Esc quits" )

    period = poll_ms / 1000.0
    while True:
        if _down( VK_ESCAPE ):
            break
        tick = time.perf_counter()
        now, observed = _snapshot( markers )
        for n, marker in markers.items():
            color = observed[ n ]
            tr = tracks[ n ]
            if color is None:
                continue
            is_busy = colors_similar( color, marker[ "color" ], tol )
            if tr[ "busy" ] is None:
                tr[ "busy" ] = is_busy
                state = f"{YELLOW}busy{RESET}" if is_busy else f"{GREEN}ready{RESET}"
                print( f"ℹ️ {CYAN}{n}{RESET} {state}" )
                continue
            if is_busy == tr[ "busy" ]:
                tr[ "pending" ] = None
                tr[ "pending_n" ] = 0
                continue
            if tr[ "pending" ] != is_busy:
                tr[ "pending" ] = is_busy
                tr[ "pending_t" ] = now
                tr[ "pending_n" ] = 1
            else:
                tr[ "pending_n" ] += 1
            if tr[ "pending_n" ] < confirm:
                continue
            tr[ "busy" ] = is_busy
            t0 = tr[ "pending_t" ]
            tr[ "pending" ] = None
            tr[ "pending_n" ] = 0
            if is_busy:
                tr[ "t_busy" ] = t0
                print( f"ℹ️ {CYAN}{n}{RESET} {YELLOW}busy{RESET}" )
            elif tr[ "t_busy" ] is not None:
                ms = ( t0 - tr[ "t_busy" ] ) * 1000.0
                tr[ "t_busy" ] = None
                tr[ "hist" ].append( ms )
                _print_duration( n, ms, tr[ "hist" ] )
            else:
                print( f"ℹ️ {CYAN}{n}{RESET} {GREEN}ready{RESET}" )
        elapsed = time.perf_counter() - tick
        if period > 0 and elapsed < period:
            time.sleep( period - elapsed )

    _print_summary( tracks )
    return 0


if __name__ == "__main__":
    sys.exit( main() )
