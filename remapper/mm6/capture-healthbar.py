"""
Throwaway: discover MM6 party healthbar geometry from one click.

Click the bright horizontal midpoint of player 1's healthbar while that character is at
full health. The script measures fill color, vertical extent, width, then locates players
2-4 (portrait offset from mm6_unselected.json, else a bottom-row scan). Writes
remapper/mm6_status.json (including any existing unselected portrait samples) and exits.

Delete this script after the file is verified.

Usage (repo root, venv; stop remapper first):
    .pxlenv\\Scripts\\python.exe remapper\\capture-healthbar.py

Esc aborts without writing.
"""

from pathlib import Path
import ctypes
import json
import sys
import time

HERE = Path( __file__ ).resolve().parent
ROOT = HERE.parent
sys.path.insert( 0, str( ROOT ) )
if str( HERE ) not in sys.path:
    sys.path.insert( 0, str( HERE ) )

from pxl_config import get_settings

get_settings( str( ROOT / "settings.toml" ) )

from ansi import *
from pxl_lib import colors_similar, get_mouse_pos, get_pixel_color

STATUS_PATH = HERE / "mm6_status.json"
UNSELECTED_PATH = HERE / "mm6_unselected.json"

VK_LBUTTON = 0x01
VK_ESCAPE = 0x1B
MAX_SPAN = 400
_user32 = ctypes.windll.user32


def _down( vk ):
    return bool( _user32.GetAsyncKeyState( vk ) & 0x8000 )


def _wait_release( vk ):
    while _down( vk ):
        if _down( VK_ESCAPE ):
            return False
        time.sleep( 0.01 )
    return True


def _wait_click():
    print( f"ℹ️ Click the bright midpoint of player {CYAN}1{RESET}'s healthbar "
           f"(that character at {GREEN}full{RESET} health). Esc cancels." )
    if _down( VK_LBUTTON ) and not _wait_release( VK_LBUTTON ):
        return None
    prev = False
    while True:
        if _down( VK_ESCAPE ):
            return None
        held = _down( VK_LBUTTON )
        if held and not prev:
            pos = get_mouse_pos()
            color = get_pixel_color( pos[ 0 ], pos[ 1 ] )
            if not _wait_release( VK_LBUTTON ):
                return None
            if color is None:
                print( f"{RED}Error: no pixel color at {MAGENTA}{pos[0]},{pos[1]}{RESET}; "
                       f"click again." )
                prev = False
                continue
            return pos[ 0 ], pos[ 1 ], color
        prev = held
        time.sleep( 0.01 )


def _match( x, y, fill, tol ):
    c = get_pixel_color( x, y )
    return c is not None and colors_similar( c, fill, tol )


def _scan_axis( x, y, fill, tol, dx, dy, limit_fn ):
    px, py = x, y
    steps = 0
    while True:
        nx, ny = px + dx, py + dy
        if not limit_fn( nx, ny ) or steps >= MAX_SPAN:
            break
        if not _match( nx, ny, fill, tol ):
            break
        px, py = nx, ny
        steps += 1
    return px, py


def health_pct_column( fill, x, y_top, y_bottom, tol ):
    height = y_bottom - y_top + 1
    if height <= 0:
        return None
    hits = 0
    for yy in range( y_top, y_bottom + 1 ):
        if _match( x, yy, fill, tol ):
            hits += 1
    return 100.0 * hits / height


def _load_unselected():
    if not UNSELECTED_PATH.is_file():
        return {}
    try:
        raw = json.loads( UNSELECTED_PATH.read_text( encoding = "utf-8" ) )
    except ( OSError, json.JSONDecodeError ):
        return {}
    if not isinstance( raw, dict ):
        return {}
    out = {}
    for n in ( "1", "2", "3", "4" ):
        item = raw.get( n )
        if not isinstance( item, dict ):
            continue
        try:
            color = item[ "color" ]
            out[ n ] = {
                "x": int( item[ "x" ] ),
                "y": int( item[ "y" ] ),
                "color": [ int( color[ 0 ] ), int( color[ 1 ] ), int( color[ 2 ] ) ],
            }
        except ( KeyError, TypeError, ValueError, IndexError ):
            continue
    return out


def _nudge( x, y_probe, fill, tol, width ):
    if _match( x, y_probe, fill, tol ):
        return x, True
    radius = max( 1, width )
    for d in range( 1, radius + 1 ):
        for cand in ( x - d, x + d ):
            if _match( cand, y_probe, fill, tol ):
                return cand, True
    return x, False


def _bars_from_portraits( p1_x, y_probe, fill, tol, width, portraits ):
    if "1" not in portraits:
        return None
    offset = p1_x - portraits[ "1" ][ "x" ]
    xs = { "1": p1_x }
    verified = { "1": True }
    for n in ( "2", "3", "4" ):
        if n not in portraits:
            return None
        cand = portraits[ n ][ "x" ] + offset
        nudged, ok = _nudge( cand, y_probe, fill, tol, width )
        xs[ n ] = nudged
        verified[ n ] = ok
        if not ok:
            print( f"{YELLOW}Warning: player {n} healthbar x {MAGENTA}{cand}{RESET} "
                   f"not fill-colored at y={MAGENTA}{y_probe}{RESET}; storing offset anyway" )
    return xs, verified


def _bars_from_scan( p1_left, p1_right, p1_x, y_probe, fill, tol, width, screen_w ):
    xs = { "1": p1_x }
    runs = []
    x = p1_right + 1
    while x < screen_w:
        if not _match( x, y_probe, fill, tol ):
            x += 1
            continue
        start = x
        while x < screen_w and _match( x, y_probe, fill, tol ):
            x += 1
        run_w = x - start
        if 0.5 * width <= run_w <= 2.0 * width + 1:
            runs.append( ( start + start + run_w - 1 ) // 2 )
        if len( runs ) >= 3:
            break
    for i, n in enumerate( ( "2", "3", "4" ) ):
        if i < len( runs ):
            xs[ n ] = runs[ i ]
        else:
            print( f"{YELLOW}Warning: could not find healthbar for player {n}{RESET}" )
            gap = p1_right - p1_left + 1
            xs[ n ] = p1_x + ( i + 1 ) * max( gap * 2, width * 8 )
    return xs


def main():
    tol = get_settings()[ "color" ][ "default_tolerance" ]
    screen_w = _user32.GetSystemMetrics( 0 )
    screen_h = _user32.GetSystemMetrics( 1 )

    clicked = _wait_click()
    if clicked is None:
        print( f"{YELLOW}Cancelled.{RESET}" )
        return 1
    x, y, fill = clicked
    fill = ( int( fill[ 0 ] ), int( fill[ 1 ] ), int( fill[ 2 ] ) )
    print( f"ℹ️ fill_color rgb {MAGENTA}{fill}{RESET} at ({MAGENTA}{x}{RESET}, {MAGENTA}{y}{RESET})" )

    _, y_top = _scan_axis( x, y, fill, tol, 0, -1, lambda nx, ny: ny >= 0 )
    _, y_bottom = _scan_axis( x, y, fill, tol, 0, 1, lambda nx, ny: ny < screen_h )
    x_left, _ = _scan_axis( x, y, fill, tol, -1, 0, lambda nx, ny: nx >= 0 )
    x_right, _ = _scan_axis( x, y, fill, tol, 1, 0, lambda nx, ny: nx < screen_w )
    width = x_right - x_left + 1
    p1_x = ( x_left + x_right ) // 2
    height = y_bottom - y_top + 1
    print( f"ℹ️ bar y {MAGENTA}{y_top}{RESET}..{MAGENTA}{y_bottom}{RESET} "
           f"height {MAGENTA}{height}{RESET} width {MAGENTA}{width}{RESET} "
           f"p1 x {MAGENTA}{p1_x}{RESET}" )

    y_probe = max( y_top, y_bottom - 1 )
    portraits = _load_unselected()
    found = _bars_from_portraits( p1_x, y_probe, fill, tol, width, portraits )
    if found is None:
        print( f"{YELLOW}No portrait file; scanning right at y={MAGENTA}{y_probe}{RESET}" )
        xs = _bars_from_scan( x_left, x_right, p1_x, y_probe, fill, tol, width, screen_w )
    else:
        xs, _verified = found

    players = {}
    for n in ( "1", "2", "3", "4" ):
        entry = { "healthbar_x": int( xs[ n ] ) }
        if n in portraits:
            entry[ "unselected" ] = portraits[ n ]
        players[ n ] = entry
        pct = health_pct_column( fill, xs[ n ], y_top, y_bottom, tol )
        pct_txt = f"{pct:.0f}%" if pct is not None else "?"
        print( f"ℹ️ player {CYAN}{n}{RESET} healthbar_x {MAGENTA}{xs[n]}{RESET} "
               f"hp {MAGENTA}{pct_txt}{RESET}" )

    status = {
        "healthbar": {
            "fill_color": list( fill ),
            "y_top": int( y_top ),
            "y_bottom": int( y_bottom ),
            "width": int( width ),
        },
        "players": players,
    }
    STATUS_PATH.write_text( json.dumps( status, indent = 2 ) + "\n", encoding = "utf-8" )
    print( f"ℹ️ {GREEN}Wrote{RESET} {CYAN}{STATUS_PATH}{RESET}" )
    return 0


if __name__ == "__main__":
    sys.exit( main() )
