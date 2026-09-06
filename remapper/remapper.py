"""
Standalone 1:1 keyboard remapper for offline/DOS games.

Captures the keyboard through Interception, rewrites configured source keys to destination
scan codes (hold-through, including auto-repeat), and sends the rewritten strokes on the
same capture context so they are not recaptured. That is what makes circular binds such as
f -> a vs a -> left produce the destination key rather than a chained remap.

Optional `plugin` in the remap TOML loads a sibling module (e.g. mm6) into this same capture
loop so game-specific hotkeys can share the Interception context.

Usage (from the repo root, venv active):
    .pxlenv\\Scripts\\python.exe remapper\\remapper.py
    .pxlenv\\Scripts\\python.exe remapper\\remapper.py remapper\\other.toml
"""

from pathlib import Path
import ctypes
import random
import sys
import time
import tomllib

HERE = Path( __file__ ).resolve().parent
ROOT = HERE.parent
sys.path.insert( 0, str( ROOT ) )
if str( HERE ) not in sys.path:
    sys.path.insert( 0, str( HERE ) )

from pxl_config import get_settings

# Populate the settings cache from the repo root before pxl_intercept's import-time lookup
get_settings( str( ROOT / "settings.toml" ) )

from pxl_intercept import detect_device_index

from ansi import *

import pyinterception.src.interception as pyint
import pyinterception.src.interception.inputs as _pi_inputs
from pyinterception.src.interception.constants import (
    KeyFlag, FilterKeyFlag, MouseFlag, MouseButtonFlag, FilterMouseButtonFlag,
)
from pyinterception.src.interception.strokes import KeyStroke, MouseStroke
from pyinterception.src.interception._keycodes import get_key_information
from pyinterception.src.interception._utils import to_interception_coordinate
from pyinterception.src.interception.exceptions import UnknownKeyError

_user32 = ctypes.windll.user32


def _foreground_title():
    buf = ctypes.create_unicode_buffer( 256 )
    _user32.GetWindowTextW( _user32.GetForegroundWindow(), buf, 256 )
    return buf.value


def _key_info( name ):
    """Resolve a config key name; names are matched case-insensitively."""
    return get_key_information( name.strip().lower() )


def load_config( path ):
    """
    Load remap.toml.

    Returns ( target_window, quit_key_name, quit_info, binds, bind_rows, plugin_name ).
    `binds` maps ( scan_code, is_extended ) -> dest KeyData.
    `bind_rows` is an ordered list of ( src_name, dest_name, src_info, dest_info ) for logging.
    """
    try:
        with open( path, "rb" ) as fh:
            raw = tomllib.load( fh )
    except OSError as exc:
        print( f"{RED}Error: cannot read {CYAN}{path}{RESET} ({exc})" )
        sys.exit( 1 )
    except tomllib.TOMLDecodeError as exc:
        print( f"{RED}Error: invalid TOML in {CYAN}{path}{RESET} ({exc})" )
        sys.exit( 1 )

    binds_cfg = raw.get( "binds" )
    if not isinstance( binds_cfg, dict ) or not binds_cfg:
        print( f"{RED}Error: {CYAN}{path}{RESET} is missing a non-empty [binds] table" )
        sys.exit( 1 )

    target_window = raw.get( "target_window" ) or ""
    if not isinstance( target_window, str ):
        print( f"{RED}Error: target_window must be a string{RESET}" )
        sys.exit( 1 )

    quit_name = raw.get( "quit_key" ) or "f12"
    if not isinstance( quit_name, str ):
        print( f"{RED}Error: quit_key must be a string{RESET}" )
        sys.exit( 1 )

    plugin_name = ( raw.get( "plugin" ) or "" ).strip()
    if plugin_name and not isinstance( plugin_name, str ):
        print( f"{RED}Error: plugin must be a string{RESET}" )
        sys.exit( 1 )

    try:
        quit_info = _key_info( quit_name )
    except UnknownKeyError:
        print( f"{RED}Error: unknown quit_key {CYAN}{quit_name}{RESET}" )
        sys.exit( 1 )

    binds = {}
    bind_rows = []
    for src_name, dest_name in binds_cfg.items():
        if not isinstance( dest_name, str ):
            print( f"{RED}Error: bind destination for {CYAN}{src_name}{RESET} must be a string" )
            sys.exit( 1 )
        try:
            src_info = _key_info( src_name )
            dest_info = _key_info( dest_name )
        except UnknownKeyError as exc:
            print( f"{RED}Error: unknown key in bind {CYAN}{src_name}{RESET} -> "
                   f"{CYAN}{dest_name}{RESET} ({exc})" )
            sys.exit( 1 )
        lookup = ( src_info.scan_code, src_info.is_extended )
        if lookup in binds:
            print( f"{YELLOW}Warning: {CYAN}{src_name}{RESET} collides with an earlier bind "
                   f"at scan {MAGENTA}0x{src_info.scan_code:02x}{RESET}; later bind wins" )
        binds[ lookup ] = dest_info
        bind_rows.append( ( src_name, dest_name, src_info, dest_info ) )

    return target_window, quit_name.strip().lower(), quit_info, binds, bind_rows, plugin_name


class Remapper:
    """
    Capture-loop remapper. Substitutes are sent on this same Interception context so the driver
    does not feed them back into the filter.
    """

    def __init__( self, target_window, quit_name, quit_info, binds, plugin_name = "" ):
        settings = get_settings()
        self.AWAIT_TIMEOUT_MS = settings[ "remapper" ][ "await_timeout_ms" ]
        self.MIN_HOLD = settings[ "remapper" ][ "min_hold" ]
        self.MAX_HOLD = settings[ "remapper" ][ "max_hold" ]
        self._target = target_window
        self._target_lower = target_window.lower()
        self._quit_name = quit_name
        self._quit = quit_info
        self.binds = binds
        self.plugin = None
        # Source ( scan, extended ) currently held as a remapped dest; used so a mid-hold alt-tab
        # still releases the destination key instead of leaving it stuck.
        self._held = {}
        self._stop = False
        self._destroyed = False

        # Package import opens a process-global Interception context we do not use. A
        # second context on the same devices can drop pass-through of intercepted
        # strokes while still allowing fresh injections (remaps) through. Close it so
        # this process has one owner before we take the keyboard.
        try:
            _pi_inputs._g_context.destroy()
        except Exception:
            pass

        self.ctx = pyint.Interception()
        self.ctx.set_filter( self.ctx.is_keyboard, FilterKeyFlag.FILTER_KEY_ALL )

        idx = detect_device_index( settings[ "devices" ][ "keyboard_hwid" ] )
        if idx is not None:
            self.ctx.keyboard = idx
            print( f"ℹ️ {GREEN}Remapper{RESET}: capturing keyboard device {MAGENTA}{idx}{RESET}" )
        else:
            print( f"⚠️ {YELLOW}Remapper: keyboard HWID not matched; using default device "
                   f"{MAGENTA}{self.ctx.keyboard}{RESET}" )

        if plugin_name:
            self._setup_mouse( settings )
            self._attach_plugin( plugin_name )

    def _setup_mouse( self, settings ):
        self.ctx.set_filter( self.ctx.is_mouse, FilterMouseButtonFlag.FILTER_MOUSE_ALL )
        midx = detect_device_index( settings[ "devices" ][ "mouse_hwid" ] )
        if midx is not None:
            self.ctx.mouse = midx
            print( f"ℹ️ {GREEN}Remapper{RESET}: capturing mouse device {MAGENTA}{midx}{RESET}" )
        else:
            print( f"⚠️ {YELLOW}Remapper: mouse HWID not matched; using default device "
                   f"{MAGENTA}{self.ctx.mouse}{RESET}" )

    def _attach_plugin( self, name ):
        try:
            mod = __import__( name )
        except ImportError as exc:
            print( f"{RED}Error: cannot import plugin {CYAN}{name}{RESET} ({exc})" )
            self._destroy()
            sys.exit( 1 )
        ctor = getattr( mod, "BookcastPlugin", None )
        if ctor is None:
            print( f"{RED}Error: plugin {CYAN}{name}{RESET} has no BookcastPlugin" )
            self._destroy()
            sys.exit( 1 )
        self.plugin = ctor( self )
        print( f"ℹ️ {GREEN}Remapper{RESET}: loaded plugin {CYAN}{name}{RESET}" )

    def window_ok( self ):
        if not self._target:
            return True
        return self._target_lower in _foreground_title().lower()

    def _window_ok( self ):
        return self.window_ok()

    def _is_quit( self, stroke ):
        return ( stroke.code == self._quit.scan_code
                 and bool( stroke.flags & KeyFlag.KEY_E0 ) == self._quit.is_extended )

    def _emit( self, device, code, flags ):
        """Send a key stroke on the capture context so it is not re-intercepted."""
        self.ctx.send( device, KeyStroke( code, flags ) )

    def _send_dest( self, device, dest, is_up ):
        flags = KeyFlag.KEY_UP if is_up else KeyFlag.KEY_DOWN
        if dest.is_extended:
            flags |= KeyFlag.KEY_E0
        self._emit( device, dest.scan_code, flags )

    def _forward( self, device, stroke ):
        """Pass an intercepted stroke to the OS with original scan code and flags."""
        if isinstance( stroke, KeyStroke ):
            self._emit( device, stroke.code, stroke.flags )
        else:
            self.ctx.send( device, stroke )

    def tap_key( self, key ):
        """Inject a down/up of `key` (name or KeyData) on the capture keyboard."""
        info = key if hasattr( key, "scan_code" ) else _key_info( key )
        hold = random.uniform( self.MIN_HOLD, self.MAX_HOLD )
        down = KeyStroke( info.scan_code, KeyFlag.KEY_DOWN )
        up = KeyStroke( info.scan_code, KeyFlag.KEY_UP )
        if info.is_extended:
            down.flags |= KeyFlag.KEY_E0
            up.flags |= KeyFlag.KEY_E0
        self.ctx.send( self.ctx.keyboard, down )
        time.sleep( hold )
        self.ctx.send( self.ctx.keyboard, up )

    def move_to( self, x, y ):
        """Move the cursor to screen pixel (x, y) via an absolute mouse stroke."""
        ix, iy = to_interception_coordinate( int( x ), int( y ) )
        self.ctx.send( self.ctx.mouse,
                       MouseStroke( MouseFlag.MOUSE_MOVE_ABSOLUTE, 0, 0, ix, iy ) )

    def click( self, button = "left", times = 1, interval_s = 0.0, hold_s = None ):
        """Click at the current cursor position without moving."""
        down_flag, up_flag = MouseButtonFlag.from_string( button )
        hold = hold_s if hold_s is not None else random.uniform( self.MIN_HOLD, self.MAX_HOLD )
        for i in range( times ):
            self.ctx.send( self.ctx.mouse,
                           MouseStroke( MouseFlag.MOUSE_MOVE_ABSOLUTE, down_flag, 0, 0, 0 ) )
            time.sleep( hold )
            self.ctx.send( self.ctx.mouse,
                           MouseStroke( MouseFlag.MOUSE_MOVE_ABSOLUTE, up_flag, 0, 0, 0 ) )
            if i + 1 < times and interval_s > 0:
                time.sleep( interval_s )

    def run( self ):
        gate = ( f"title contains {CYAN}{self._target}{RESET}" if self._target
                 else f"{YELLOW}always on{RESET}" )
        extra = f"; plugin {CYAN}{self.plugin.__class__.__module__}{RESET}" if self.plugin else ""
        print( f"ℹ️ {GREEN}Remapper{RESET}: {gate}; "
               f"quit = {CYAN}{self._quit_name}{RESET}{extra}" )
        try:
            self._loop()
        finally:
            self._destroy()

    def _loop( self ):
        while not self._stop:
            device = self.ctx.await_input( self.AWAIT_TIMEOUT_MS )
            if device is None:
                continue

            stroke = self.ctx.devices[ device ].receive()
            if stroke is None:
                continue

            if isinstance( stroke, MouseStroke ):
                if self.plugin is not None and self.plugin.on_stroke( device, stroke ):
                    continue
                self.ctx.send( device, stroke )
                continue

            if not isinstance( stroke, KeyStroke ):
                self.ctx.send( device, stroke )
                continue

            is_up = bool( stroke.flags & KeyFlag.KEY_UP )

            if self._is_quit( stroke ):
                if not is_up:
                    print( f"⏹️ {RED}quit{RESET} ({CYAN}{self._quit_name}{RESET})" )
                    self._stop = True
                continue

            if self.plugin is not None and self.plugin.on_stroke( device, stroke ):
                continue

            src = ( stroke.code, bool( stroke.flags & KeyFlag.KEY_E0 ) )
            dest = self.binds.get( src )

            # Finish a remapped hold with the dest key-up even if the window gate changed
            if is_up and src in self._held:
                self._send_dest( device, self._held.pop( src ), is_up = True )
                continue

            if dest is not None and not is_up and ( src in self._held or self._window_ok() ):
                self._held[ src ] = dest
                self._send_dest( device, dest, is_up = False )
                continue

            self._forward( device, stroke )

    def _destroy( self ):
        if self._destroyed:
            return
        self._destroyed = True
        print( f"ℹ️ {YELLOW}Closing Remapper...{RESET}" )
        try:
            self.ctx.destroy()
        except Exception:
            pass


def main():
    if len( sys.argv ) > 1:
        cfg_path = Path( sys.argv[ 1 ] ).expanduser().resolve()
    else:
        cfg_path = HERE / "remap.toml"

    target_window, quit_name, quit_info, binds, bind_rows, plugin_name = load_config( cfg_path )
    print( f"ℹ️ {GREEN}Remapper{RESET}: loaded {CYAN}{cfg_path}{RESET}" )
    for src_name, dest_name, src_info, dest_info in bind_rows:
        print( f"ℹ️   {CYAN}{src_name}{RESET} (scan {MAGENTA}0x{src_info.scan_code:02x}{RESET}) -> "
               f"{CYAN}{dest_name}{RESET} (scan {MAGENTA}0x{dest_info.scan_code:02x}{RESET})" )

    remapper = Remapper( target_window, quit_name, quit_info, binds, plugin_name = plugin_name )
    try:
        remapper.run()
    except KeyboardInterrupt:
        print( f"⏹️ {RED}interrupted{RESET}" )
        remapper._destroy()


if __name__ == "__main__":
    main()
