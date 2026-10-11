"""
Throwaway: capture the MM (or DOSBox) foreground window identity for remapper gating.

Click once on the game window. Writes remapper/window.json (title substring + exe basename)
and exits. Stop remapper first so this process sees the click.

Usage (repo root, venv):
    .pxlenv\\Scripts\\python.exe remapper\\capture-window.py

Esc aborts without writing.
"""

from pathlib import Path
import ctypes
import json
import re
import sys
import time

HERE = Path( __file__ ).resolve().parent
ROOT = HERE.parent
sys.path.insert( 0, str( ROOT ) )

from ansi import *
from pxl_wincheck import foreground_exe, foreground_title

OUT_PATH = HERE / "window.json"

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


def _title_contains( title ):
    """Stable substring: DOSBox 'Program: …' or a known MM needle; else the full title."""
    if not title:
        return ""
    m = re.search( r"Program:\s*\S+", title, re.I )
    if m:
        return m.group( 0 )
    lower = title.lower()
    for needle in ( "Might and Magic VI", "Might and Magic 6", "MM6" ):
        if needle.lower() in lower:
            return needle
    return title


def main():
    print( f"ℹ️ Focus MM and {CYAN}click once{RESET} on the game window." )
    print( f"ℹ️ Esc cancels. Writing to {CYAN}{OUT_PATH}{RESET}." )

    if _down( VK_LBUTTON ) and not _wait_release( VK_LBUTTON ):
        print( f"{YELLOW}Cancelled.{RESET}" )
        return 1

    prev = False
    while True:
        if _down( VK_ESCAPE ):
            print( f"{YELLOW}Cancelled.{RESET}" )
            return 1
        held = _down( VK_LBUTTON )
        if held and not prev:
            title = foreground_title()
            exe = foreground_exe()
            contains = _title_contains( title )
            if not title and not exe:
                print( f"{RED}Error: empty title and exe; click the game window, not the console." )
                if not _wait_release( VK_LBUTTON ):
                    print( f"{YELLOW}Cancelled.{RESET}" )
                    return 1
                prev = False
                continue
            payload = {
                "title": title,
                "title_contains": contains,
                "exe": exe,
            }
            OUT_PATH.write_text( json.dumps( payload, indent = 2 ) + "\n", encoding = "utf-8" )
            print( f"ℹ️ title {CYAN}{title or '(none)'}{RESET}" )
            print( f"ℹ️ title_contains {CYAN}{contains or '(none)'}{RESET}" )
            print( f"ℹ️ exe {CYAN}{exe or '(none)'}{RESET}" )
            print( f"ℹ️ {GREEN}Wrote{RESET} {CYAN}{OUT_PATH}{RESET}" )
            if contains == title and title:
                print( f"{YELLOW}Warning: using the full title as the match; DOSBox cycle-count "
                       f"changes will miss. Edit {CYAN}title_contains{RESET} to a stable substring." )
            return 0
        prev = held
        time.sleep( 0.01 )


if __name__ == "__main__":
    sys.exit( main() )
