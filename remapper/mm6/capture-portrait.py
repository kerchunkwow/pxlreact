"""
Throwaway: capture a clickable (x, y) on each MM6 party portrait.

Click anywhere on portraits 1-4 in order. Precision does not matter; the click is a
future target/select point. Merges into remapper/mm6_status.json and exits.

Delete this script after the file is verified.

Usage (repo root, venv; stop remapper first):
    .pxlenv\\Scripts\\python.exe remapper\\capture-portrait.py

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

from ansi import *
from pxl_lib import get_mouse_pos

STATUS_PATH = HERE / "mm6_status.json"

VK_LBUTTON = 0x01
VK_ESCAPE = 0x1B
_user32 = ctypes.windll.user32


def _down( vk ):
    return bool( _user32.GetAsyncKeyState( vk ) & 0x8000 )


def _wait_release( vk ):
    while _down( vk ):
        if _down( VK_ESCAPE ):
            return False
        time.sleep( 0.01 )
    return True


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


def main():
    print( f"ℹ️ Click anywhere on portraits {CYAN}1{RESET} through {CYAN}4{RESET} in order." )
    print( f"ℹ️ Precision is unimportant. Esc cancels." )
    print( f"ℹ️ Merging into {CYAN}{STATUS_PATH}{RESET} after the fourth click." )

    if _down( VK_LBUTTON ) and not _wait_release( VK_LBUTTON ):
        print( f"{YELLOW}Cancelled.{RESET}" )
        return 1

    samples = {}
    n = 1
    prev = False
    while n <= 4:
        if _down( VK_ESCAPE ):
            print( f"{YELLOW}Cancelled.{RESET}" )
            return 1
        held = _down( VK_LBUTTON )
        if held and not prev:
            x, y = get_mouse_pos()
            samples[ str( n ) ] = { "x": int( x ), "y": int( y ) }
            print( f"ℹ️ {GREEN}{n}{RESET}: ({MAGENTA}{x}{RESET}, {MAGENTA}{y}{RESET})" )
            n += 1
            if not _wait_release( VK_LBUTTON ):
                print( f"{YELLOW}Cancelled.{RESET}" )
                return 1
            prev = False
            continue
        prev = held
        time.sleep( 0.01 )

    status = _load_status()
    for n, pos in samples.items():
        entry = status[ "players" ].get( n )
        if not isinstance( entry, dict ):
            entry = {}
        entry[ "portrait" ] = pos
        status[ "players" ][ n ] = entry

    STATUS_PATH.write_text( json.dumps( status, indent = 2 ) + "\n", encoding = "utf-8" )
    print( f"ℹ️ {GREEN}Wrote{RESET} {CYAN}{STATUS_PATH}{RESET}" )
    return 0


if __name__ == "__main__":
    sys.exit( main() )
