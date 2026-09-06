"""
Throwaway: capture unselected-character portrait pixels for MM6 bookcasts.

Click four unselected party portraits in order (1-4). Each click records cursor (x, y) and
the pixel color under the cursor. Writes remapper/mm6_unselected.json and exits.

Not used at runtime by remapper/mm6.py except as a data file; delete this script after the
file is verified.

Usage (repo root, venv; stop remapper first so this process owns the mouse):
    .pxlenv\\Scripts\\python.exe remapper\\capture-unselected.py

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
from pxl_lib import get_mouse_pos, get_pixel_color

OUT_PATH = HERE / "mm6_unselected.json"

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


def main():
    print( f"ℹ️ Click unselected portraits {CYAN}1{RESET} through {CYAN}4{RESET} in order." )
    print( f"ℹ️ Pixel under the cursor is stored with each click. Esc cancels." )
    print( f"ℹ️ Writing to {CYAN}{OUT_PATH}{RESET} after the fourth click." )

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
            color = get_pixel_color( x, y )
            if color is None:
                print( f"{RED}Error: no pixel color at {MAGENTA}{x},{y}{RESET}; try again." )
            else:
                samples[ str( n ) ] = { "x": int( x ), "y": int( y ), "color": [ int( c ) for c in color ] }
                print( f"ℹ️ {GREEN}{n}{RESET}: ({MAGENTA}{x}{RESET}, {MAGENTA}{y}{RESET}) "
                       f"rgb {MAGENTA}{tuple( color )}{RESET}" )
                n += 1
            if not _wait_release( VK_LBUTTON ):
                print( f"{YELLOW}Cancelled.{RESET}" )
                return 1
            prev = False
            continue
        prev = held
        time.sleep( 0.01 )

    OUT_PATH.write_text( json.dumps( samples, indent = 2 ) + "\n", encoding = "utf-8" )
    print( f"ℹ️ {GREEN}Wrote{RESET} {CYAN}{OUT_PATH}{RESET}" )
    return 0


if __name__ == "__main__":
    sys.exit( main() )
