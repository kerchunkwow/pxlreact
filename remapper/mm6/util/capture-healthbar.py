"""
Throwaway: discover MM6 party healthbar geometry and fill colors.

Geometry (first run): click the bright midpoint of player 1's healthbar at full health.
The script measures fill color, vertical extent, width, then locates players 2-4.

Fill shades (this or a later run): with green, yellow, and red bars all visible, click once
on the game. The four known columns are scanned for additional fill colors so yellow/red
HP still counts. Merges into remapper/mm6/mm6_status.json (portraits are preserved).

Usage (repo root, venv; stop remapper first):
    .pxlenv\\Scripts\\python.exe remapper\\mm6\\capture-healthbar.py

Esc aborts without writing.
"""

from pathlib import Path
import colorsys
import ctypes
import json
import sys
import time

HERE = Path( __file__ ).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert( 0, str( ROOT ) )
if str( HERE ) not in sys.path:
    sys.path.insert( 0, str( HERE ) )

from pxl_config import get_settings

get_settings( str( ROOT / "settings.toml" ) )

from ansi import *
from pxl_lib import PIXELS, colors_similar, get_mouse_pos, get_pixel_color

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


def _wait_click( prompt ):
    print( prompt )
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


def health_pct_column( fills, x, y_top, y_bottom, tol ):
    height = y_bottom - y_top + 1
    if height <= 0:
        return None
    hits = 0
    for yy in range( y_top, y_bottom + 1 ):
        c = get_pixel_color( x, yy )
        if c is not None and any( colors_similar( c, fill, tol ) for fill in fills ):
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


def _load_status():
    if not STATUS_PATH.is_file():
        return { "players": {} }
    try:
        raw = json.loads( STATUS_PATH.read_text( encoding = "utf-8" ) )
    except ( OSError, json.JSONDecodeError ) as exc:
        print( f"{YELLOW}Warning: could not read {CYAN}{STATUS_PATH}{RESET} ({exc}); "
               f"starting a new file" )
        return { "players": {} }
    if not isinstance( raw, dict ):
        return { "players": {} }
    raw.setdefault( "players", {} )
    if not isinstance( raw[ "players" ], dict ):
        raw[ "players" ] = {}
    return raw


def _geometry( status ):
    hb = status.get( "healthbar" )
    players = status.get( "players" ) or {}
    if not isinstance( hb, dict ):
        return None
    try:
        fill = ( int( hb[ "fill_color" ][ 0 ] ), int( hb[ "fill_color" ][ 1 ] ),
                 int( hb[ "fill_color" ][ 2 ] ) )
        y_top = int( hb[ "y_top" ] )
        y_bottom = int( hb[ "y_bottom" ] )
        width = int( hb[ "width" ] )
        xs = {}
        for n in ( "1", "2", "3", "4" ):
            p = players.get( n )
            if not isinstance( p, dict ):
                return None
            xs[ n ] = int( p[ "healthbar_x" ] )
    except ( KeyError, TypeError, ValueError, IndexError ):
        return None
    if y_bottom < y_top or width <= 0:
        return None
    return fill, y_top, y_bottom, width, xs


def _fill_name( rgb ):
    r, g, b = ( c / 255.0 for c in rgb )
    h, s, v = colorsys.rgb_to_hsv( r, g, b )
    if s < 0.25:
        return "grey"
    if h < 0.06 or h > 0.92:
        return "red"
    if h < 0.20:
        return "yellow"
    if h < 0.50:
        return "green"
    return "other"


def _is_fill_cluster( rgb, n, min_n, green, tol ):
    if n < min_n:
        return False
    if colors_similar( rgb, green, tol ):
        return True
    chroma = max( rgb ) - min( rgb )
    return max( rgb ) >= 100 and chroma >= 40


def _discover_fill_colors( xs, y_top, y_bottom, green, tol ):
    points = []
    for x in xs.values():
        for y in range( y_top, y_bottom + 1 ):
            points.append( ( x, y ) )
    PIXELS.register_points( points )
    samples = [ get_pixel_color( x, y ) for x, y in points ]
    groups = []
    for c in samples:
        if c is None:
            continue
        rgb = ( int( c[ 0 ] ), int( c[ 1 ] ), int( c[ 2 ] ) )
        for g in groups:
            if colors_similar( rgb, g[ 0 ], tol ):
                g[ 1 ] += 1
                break
        else:
            groups.append( [ rgb, 1 ] )
    groups.sort( key = lambda g: -g[ 1 ] )
    min_n = max( 8, len( samples ) // 80 )
    fills = []
    for rgb, n in groups:
        name = _fill_name( rgb )
        keep = _is_fill_cluster( rgb, n, min_n, green, tol )
        tag = f"{GREEN}fill {name}{RESET}" if keep else f"{YELLOW}skip{RESET}"
        print( f"ℹ️ cluster rgb {MAGENTA}{rgb}{RESET} n={MAGENTA}{n}{RESET} {tag}" )
        if keep:
            fills.append( rgb )
    ordered = []
    for name in ( "green", "yellow", "red" ):
        for rgb in fills:
            if _fill_name( rgb ) == name and rgb not in ordered:
                ordered.append( rgb )
    for rgb in fills:
        if rgb not in ordered:
            ordered.append( rgb )
    if not any( colors_similar( green, c, tol ) for c in ordered ):
        ordered.insert( 0, green )
    if not ordered:
        ordered = [ green ]
    return ordered


def _capture_geometry( status, tol, screen_w, screen_h ):
    clicked = _wait_click(
        f"ℹ️ Click the bright midpoint of player {CYAN}1{RESET}'s healthbar "
        f"(that character at {GREEN}full{RESET} health). Esc cancels." )
    if clicked is None:
        return None
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

    hb = status.setdefault( "healthbar", {} )
    hb[ "fill_color" ] = list( fill )
    hb[ "y_top" ] = int( y_top )
    hb[ "y_bottom" ] = int( y_bottom )
    hb[ "width" ] = int( width )
    players = status.setdefault( "players", {} )
    for n in ( "1", "2", "3", "4" ):
        entry = players.get( n )
        if not isinstance( entry, dict ):
            entry = {}
        entry[ "healthbar_x" ] = int( xs[ n ] )
        if n in portraits:
            entry[ "unselected" ] = portraits[ n ]
        players[ n ] = entry
    return fill, y_top, y_bottom, width, xs


def main():
    tol = get_settings()[ "color" ][ "default_tolerance" ]
    screen_w = _user32.GetSystemMetrics( 0 )
    screen_h = _user32.GetSystemMetrics( 1 )
    status = _load_status()
    geo = _geometry( status )

    if geo is None:
        captured = _capture_geometry( status, tol, screen_w, screen_h )
        if captured is None:
            print( f"{YELLOW}Cancelled.{RESET}" )
            return 1
        fill, y_top, y_bottom, width, xs = captured
    else:
        fill, y_top, y_bottom, width, xs = geo
        print( f"ℹ️ using stored bars y {MAGENTA}{y_top}{RESET}..{MAGENTA}{y_bottom}{RESET} "
               f"width {MAGENTA}{width}{RESET} green {MAGENTA}{fill}{RESET}" )
        clicked = _wait_click(
            f"ℹ️ Arrange {GREEN}green{RESET}, {YELLOW}yellow{RESET}, and {RED}red{RESET} "
            f"healthbars on screen, then click once on the game. Esc cancels." )
        if clicked is None:
            print( f"{YELLOW}Cancelled.{RESET}" )
            return 1

    fills = _discover_fill_colors( xs, y_top, y_bottom, fill, tol )
    names = ", ".join( f"{_fill_name( c )} {c}" for c in fills )
    print( f"ℹ️ fill_colors ({MAGENTA}{len( fills )}{RESET}): {names}" )
    if len( fills ) < 3:
        print( f"{YELLOW}Warning: expected green+yellow+red; only "
               f"{MAGENTA}{len( fills )}{RESET} fill shade(s) kept. "
               f"Show all three colors and re-run.{RESET}" )

    status.setdefault( "healthbar", {} )[ "fill_color" ] = list( fill )
    status[ "healthbar" ][ "fill_colors" ] = [ list( c ) for c in fills ]

    for n in ( "1", "2", "3", "4" ):
        pct = health_pct_column( fills, xs[ n ], y_top, y_bottom, tol )
        pct_txt = f"{pct:.0f}%" if pct is not None else "?"
        print( f"ℹ️ player {CYAN}{n}{RESET} healthbar_x {MAGENTA}{xs[n]}{RESET} "
               f"hp {MAGENTA}{pct_txt}{RESET}" )

    STATUS_PATH.write_text( json.dumps( status, indent = 2 ) + "\n", encoding = "utf-8" )
    print( f"ℹ️ {GREEN}Wrote{RESET} {CYAN}{STATUS_PATH}{RESET}" )
    return 0


if __name__ == "__main__":
    sys.exit( main() )
