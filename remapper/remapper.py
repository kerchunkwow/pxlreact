"""
Standalone 1:1 keyboard remapper for offline/DOS games.

Captures the keyboard through Interception, rewrites configured source keys to destination
scan codes (hold-through, including auto-repeat), and sends the rewritten strokes on the
same capture context so they are not recaptured. That is what makes circular binds such as
f -> a vs a -> left produce the destination key rather than a chained remap.

Optional `plugin` in the remap TOML loads a sibling module (e.g. mm) into this same capture
loop so game-specific hotkeys can share the Interception context. Optional `[sequences]` maps
a key to a one-shot tap list (injected keys are not remapped or re-sequenced).

Ctrl+Alt+M records a click sequence: left/right clicks are stored with cursor position, a second
Ctrl+Alt+M waits for a bind key, and Ctrl+that key replays move+click via Interception. Saved next
to the remap TOML as click_sequences.json. Ctrl+Alt+C prints the pixel under the cursor
as a truecolor chip plus the hue name and hex in that color.
Ctrl+wheel (no Alt) repeats the wheel tick `ctrl_wheel_repeat` times so MM fly climbs/descends faster.
Alt+wheel (no Ctrl) taps the keys in [alt_wheel] (MM look: wheel up = PgDn, wheel down = Del).
Each notch is repeated `repeat` times, spaced by `delay_ms`, and the wheel tick is not forwarded.
Optional [strafe] toggles Left Ctrl around named keys only (MM: Z on, A/D become Ctrl+Left/Right)
and lifts that Ctrl around every other key.

Usage (from the repo root, venv active):
    .pxlenv\\Scripts\\python.exe remapper\\remapper.py
    .pxlenv\\Scripts\\python.exe remapper\\remapper.py remapper\\other.toml
"""

from pathlib import Path
import json
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
from pxl_lib import color_name, get_mouse_pos, get_pixel_color, rgb_to_hex
from pxl_wincheck import foreground_exe, foreground_title

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

def _key_info( name ):
    """Resolve a config key name; names are matched case-insensitively."""
    return get_key_information( name.strip().lower() )


def _mod_scan( *names ):
    scans = set()
    for name in names:
        try:
            scans.add( _key_info( name ).scan_code )
        except UnknownKeyError:
            pass
    return scans


CTRL_SCANS = _mod_scan( "ctrlleft", "ctrlright", "ctrl" )
ALT_SCANS = _mod_scan( "altleft", "altright", "alt" )
_MOUSE_BUTTON_MASK = (
    MouseButtonFlag.MOUSE_LEFT_BUTTON_DOWN
    | MouseButtonFlag.MOUSE_LEFT_BUTTON_UP
    | MouseButtonFlag.MOUSE_RIGHT_BUTTON_DOWN
    | MouseButtonFlag.MOUSE_RIGHT_BUTTON_UP
    | MouseButtonFlag.MOUSE_MIDDLE_BUTTON_DOWN
    | MouseButtonFlag.MOUSE_MIDDLE_BUTTON_UP
    | MouseButtonFlag.MOUSE_BUTTON_4_DOWN
    | MouseButtonFlag.MOUSE_BUTTON_4_UP
    | MouseButtonFlag.MOUSE_BUTTON_5_DOWN
    | MouseButtonFlag.MOUSE_BUTTON_5_UP
)

_NAME_KEYS = (
    list( "0123456789abcdefghijklmnopqrstuvwxyz" )
    + [ f"f{i}" for i in range( 1, 13 ) ]
    + [ "space", "tab", "enter", "esc", "`", "-", "=", "[", "]", "\\", ";", "'", ",", ".", "/" ]
)


def _build_key_names():
    names = {}
    for name in _NAME_KEYS:
        try:
            info = _key_info( name )
        except UnknownKeyError:
            continue
        names[ ( info.scan_code, info.is_extended ) ] = name
    return names


KEY_NAMES = _build_key_names()


def _load_window_json( path ):
    """Optional window.json next to the remap TOML: title_contains + exe."""
    if not path.is_file():
        return "", ""
    try:
        raw = json.loads( path.read_text( encoding = "utf-8" ) )
    except ( OSError, json.JSONDecodeError ) as exc:
        print( f"{YELLOW}Warning: cannot read {CYAN}{path}{RESET} ({exc})" )
        return "", ""
    if not isinstance( raw, dict ):
        print( f"{YELLOW}Warning: {CYAN}{path}{RESET} must be a JSON object" )
        return "", ""
    title = str( raw.get( "title_contains" ) or raw.get( "title" ) or "" ).strip()
    exe = str( raw.get( "exe" ) or "" ).strip()
    return title, exe


def _parse_alt_wheel( raw ):
    """Return ( up, down, repeat, delay_ms ). up/down are (name, KeyData) or None."""
    cfg = raw.get( "alt_wheel" )
    if not cfg:
        return None, None, 1, 0
    if not isinstance( cfg, dict ):
        print( f"{RED}Error: alt_wheel must be a table{RESET}" )
        sys.exit( 1 )

    def one( field ):
        name = cfg.get( field, "" )
        if name is None:
            name = ""
        if not isinstance( name, str ):
            print( f"{RED}Error: alt_wheel.{field} must be a key name{RESET}" )
            sys.exit( 1 )
        name = name.strip().lower()
        if not name:
            return None
        try:
            return name, _key_info( name )
        except UnknownKeyError:
            print( f"{RED}Error: unknown alt_wheel.{field} key {CYAN}{name}{RESET}" )
            sys.exit( 1 )

    repeat = int( cfg.get( "repeat", 6 ) )
    if repeat < 1:
        repeat = 1
    delay_ms = float( cfg.get( "delay_ms", 15 ) )
    if delay_ms < 0:
        delay_ms = 0
    return one( "up" ), one( "down" ), repeat, delay_ms


def _parse_strafe( raw, quit_info, binds ):
    """
    Return ( toggle_src or None, strafe key srcs, toggle name, key names,
              while-on binds, while-on labels ).
    Empty toggle disables the feature. `while_on` binds apply only while strafe mode is on.
    """
    cfg = raw.get( "strafe" )
    if not cfg:
        return None, set(), "", [], {}, []
    if not isinstance( cfg, dict ):
        print( f"{RED}Error: strafe must be a table{RESET}" )
        sys.exit( 1 )
    toggle_name = str( cfg.get( "toggle" ) or "" ).strip().lower()
    if not toggle_name:
        return None, set(), "", [], {}, []
    try:
        toggle_info = _key_info( toggle_name )
    except UnknownKeyError:
        print( f"{RED}Error: unknown strafe toggle {CYAN}{toggle_name}{RESET}" )
        sys.exit( 1 )
    toggle = ( toggle_info.scan_code, toggle_info.is_extended )
    if toggle == ( quit_info.scan_code, quit_info.is_extended ):
        print( f"{RED}Error: strafe toggle {CYAN}{toggle_name}{RESET} collides with quit_key{RESET}" )
        sys.exit( 1 )
    if toggle in binds:
        print( f"{YELLOW}Warning: strafe toggle {CYAN}{toggle_name}{RESET} "
               f"swallows that [binds] entry while the game is focused{RESET}" )
    keys_cfg = cfg.get( "keys", [ "a", "d" ] )
    if not isinstance( keys_cfg, list ) or not keys_cfg:
        print( f"{RED}Error: strafe keys must be a non-empty list{RESET}" )
        sys.exit( 1 )
    strafe_keys = set()
    names = []
    for name in keys_cfg:
        if not isinstance( name, str ):
            print( f"{RED}Error: strafe keys must be key names{RESET}" )
            sys.exit( 1 )
        key_name = name.strip().lower()
        try:
            info = _key_info( key_name )
        except UnknownKeyError:
            print( f"{RED}Error: unknown strafe key {CYAN}{key_name}{RESET}" )
            sys.exit( 1 )
        src = ( info.scan_code, info.is_extended )
        if src == toggle:
            print( f"{RED}Error: strafe toggle {CYAN}{toggle_name}{RESET} "
                   f"cannot also be a strafe key{RESET}" )
            sys.exit( 1 )
        if src not in binds:
            print( f"{RED}Error: strafe key {CYAN}{key_name}{RESET} must be a [binds] entry{RESET}" )
            sys.exit( 1 )
        if src not in strafe_keys:
            names.append( key_name )
        strafe_keys.add( src )
    while_on = {}
    while_names = []
    while_cfg = cfg.get( "while_on" ) or {}
    if while_cfg and not isinstance( while_cfg, dict ):
        print( f"{RED}Error: strafe.while_on must be a table of key = key{RESET}" )
        sys.exit( 1 )
    for src_name, dest_name in while_cfg.items():
        if not isinstance( dest_name, str ):
            print( f"{RED}Error: strafe.while_on destination for {CYAN}{src_name}{RESET} "
                   f"must be a key name{RESET}" )
            sys.exit( 1 )
        try:
            src_info = _key_info( src_name )
            dest_info = _key_info( dest_name )
        except UnknownKeyError as exc:
            print( f"{RED}Error: unknown key in strafe.while_on {CYAN}{src_name}{RESET} -> "
                   f"{CYAN}{dest_name}{RESET} ({exc})" )
            sys.exit( 1 )
        src = ( src_info.scan_code, src_info.is_extended )
        if src == toggle or src in strafe_keys:
            print( f"{RED}Error: strafe.while_on {CYAN}{src_name}{RESET} "
                   f"collides with the strafe toggle or a strafe key{RESET}" )
            sys.exit( 1 )
        if src in binds:
            print( f"{YELLOW}Warning: strafe.while_on {CYAN}{src_name}{RESET} "
                   f"is ignored; that key already has a [binds] entry{RESET}" )
            continue
        while_on[ src ] = dest_info
        while_names.append( f"{src_name.strip().lower()}->{dest_name.strip().lower()}" )
    return toggle, strafe_keys, toggle_name, names, while_on, while_names


def load_config( path ):
    """
    Load remap.toml.

    Returns ( target_window, target_exe, quit_key_name, quit_info, binds, bind_rows,
              plugin_name, sequences, seq_rows, sequence_delay_ms, click_sequence_delay_ms,
              ctrl_wheel_repeat, ctrl_wheel_delay_ms, alt_wheel_up, alt_wheel_down,
              alt_wheel_repeat, alt_wheel_delay_ms, strafe_toggle, strafe_keys, strafe_toggle_name, strafe_key_names,
              strafe_while, strafe_while_names ).
    `binds` maps ( scan_code, is_extended ) -> dest KeyData.
    `sequences` maps ( scan_code, is_extended ) -> list of dest KeyData.
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
    target_exe = raw.get( "target_exe" ) or ""
    if not isinstance( target_exe, str ):
        print( f"{RED}Error: target_exe must be a string{RESET}" )
        sys.exit( 1 )
    target_window = target_window.strip()
    target_exe = target_exe.strip()

    json_title, json_exe = _load_window_json( Path( path ).parent / "window.json" )
    if not target_window:
        target_window = json_title
    if not target_exe:
        target_exe = json_exe

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

    sequence_delay_ms = float( raw.get( "sequence_delay_ms", 35 ) )
    sequences = {}
    seq_rows = []
    seq_cfg = raw.get( "sequences" ) or {}
    if seq_cfg and not isinstance( seq_cfg, dict ):
        print( f"{RED}Error: sequences must be a table of key = [ key, ... ]{RESET}" )
        sys.exit( 1 )
    for src_name, dest_names in seq_cfg.items():
        if isinstance( dest_names, str ):
            dest_names = [ dest_names ]
        if not isinstance( dest_names, list ) or not dest_names:
            print( f"{RED}Error: sequence {CYAN}{src_name}{RESET} must be a non-empty list of keys" )
            sys.exit( 1 )
        try:
            src_info = _key_info( src_name )
        except UnknownKeyError as exc:
            print( f"{RED}Error: unknown sequence key {CYAN}{src_name}{RESET} ({exc})" )
            sys.exit( 1 )
        lookup = ( src_info.scan_code, src_info.is_extended )
        if lookup == ( quit_info.scan_code, quit_info.is_extended ):
            print( f"{RED}Error: sequence {CYAN}{src_name}{RESET} collides with quit_key{RESET}" )
            sys.exit( 1 )
        if lookup in binds:
            print( f"{RED}Error: sequence {CYAN}{src_name}{RESET} collides with a [binds] entry{RESET}" )
            sys.exit( 1 )
        dest_infos = []
        for dest_name in dest_names:
            if not isinstance( dest_name, str ):
                print( f"{RED}Error: sequence {CYAN}{src_name}{RESET} entries must be key names" )
                sys.exit( 1 )
            try:
                dest_infos.append( _key_info( dest_name ) )
            except UnknownKeyError as exc:
                print( f"{RED}Error: unknown key in sequence {CYAN}{src_name}{RESET} -> "
                       f"{CYAN}{dest_name}{RESET} ({exc})" )
                sys.exit( 1 )
        if lookup in sequences:
            print( f"{YELLOW}Warning: {CYAN}{src_name}{RESET} collides with an earlier sequence "
                   f"at scan {MAGENTA}0x{src_info.scan_code:02x}{RESET}; later sequence wins" )
        sequences[ lookup ] = dest_infos
        seq_rows.append( ( src_name.strip().lower(), [ str( n ).strip().lower() for n in dest_names ],
                           src_info ) )

    click_sequence_delay_ms = float( raw.get( "click_sequence_delay_ms", 20 ) )
    ctrl_wheel_repeat = int( raw.get( "ctrl_wheel_repeat", 8 ) )
    if ctrl_wheel_repeat < 1:
        ctrl_wheel_repeat = 1
    ctrl_wheel_delay_ms = float( raw.get( "ctrl_wheel_delay_ms", 5 ) )
    alt_wheel_up, alt_wheel_down, alt_wheel_repeat, alt_wheel_delay_ms = _parse_alt_wheel( raw )
    strafe_toggle, strafe_keys, strafe_toggle_name, strafe_key_names, strafe_while, strafe_while_names = _parse_strafe(
        raw, quit_info, binds )

    return ( target_window, target_exe, quit_name.strip().lower(), quit_info, binds, bind_rows,
             plugin_name, sequences, seq_rows, sequence_delay_ms, click_sequence_delay_ms,
             ctrl_wheel_repeat, ctrl_wheel_delay_ms, alt_wheel_up, alt_wheel_down,
             alt_wheel_repeat, alt_wheel_delay_ms,
             strafe_toggle, strafe_keys, strafe_toggle_name, strafe_key_names,
             strafe_while, strafe_while_names )


def _print_probe( x, y, rgb ):
    """Truecolor chip of the sampled pixel, with the hue name and hex printed in that color."""
    r, g, b = ( int( c ) for c in rgb[ :3 ] )
    ink = fg_rgb( r, g, b )
    fill = f"{bg_rgb( r, g, b )}{' ' * 8}{RESET}"
    info = ( f"({MAGENTA}{x}{RESET}, {MAGENTA}{y}{RESET})  "
             f"{ink}{color_name( ( r, g, b ) )}{RESET}  "
             f"({MAGENTA}{r:>3}{RESET}, {MAGENTA}{g:>3}{RESET}, {MAGENTA}{b:>3}{RESET})  "
             f"{ink}{rgb_to_hex( ( r, g, b ) )}{RESET}" )
    print( f"┌{'─' * 8}┐" )
    print( f"│{fill}│  {info}" )
    print( f"└{'─' * 8}┘" )


class Remapper:
    """
    Capture-loop remapper. Substitutes are sent on this same Interception context so the driver
    does not feed them back into the filter.
    """

    def __init__( self, target_window, quit_name, quit_info, binds, plugin_name = "",
                  target_exe = "", sequences = None, sequence_delay_ms = 35,
                  click_sequence_delay_ms = 20, click_seqs_path = None,
                  ctrl_wheel_repeat = 8, ctrl_wheel_delay_ms = 5,
                  alt_wheel_up = None, alt_wheel_down = None,
                  alt_wheel_repeat = 6, alt_wheel_delay_ms = 15,
                  strafe_toggle = None, strafe_keys = None,
                  strafe_toggle_name = "", strafe_key_names = None, strafe_while = None,
                  strafe_while_names = None ):
        settings = get_settings()
        self.AWAIT_TIMEOUT_MS = settings[ "remapper" ][ "await_timeout_ms" ]
        self.MIN_HOLD = settings[ "remapper" ][ "min_hold" ]
        self.MAX_HOLD = settings[ "remapper" ][ "max_hold" ]
        self._target = target_window
        self._target_lower = target_window.lower()
        self._exe = target_exe
        self._exe_lower = target_exe.lower()
        self._quit_name = quit_name
        self._quit = quit_info
        self.binds = binds
        self.sequences = sequences or {}
        self.sequence_delay_s = float( sequence_delay_ms ) / 1000.0
        self.click_sequence_delay_s = float( click_sequence_delay_ms ) / 1000.0
        self.ctrl_wheel_repeat = int( ctrl_wheel_repeat )
        self.ctrl_wheel_delay_s = float( ctrl_wheel_delay_ms ) / 1000.0
        self._alt_wheel_up = alt_wheel_up
        self._alt_wheel_down = alt_wheel_down
        self.alt_wheel_repeat = int( alt_wheel_repeat )
        if self.alt_wheel_repeat < 1:
            self.alt_wheel_repeat = 1
        self.alt_wheel_delay_s = float( alt_wheel_delay_ms ) / 1000.0
        self._click_seqs_path = Path( click_seqs_path ) if click_seqs_path else HERE / "click_sequences.json"
        self.plugin = None
        self._ctrl = False
        self._alt = False
        self._alt_down = set()
        self._ctrl_down = set()
        self._ctrl_shadow = set()
        self._ctrl_left = _key_info( "ctrlleft" )
        self._ctrl_left_src = ( self._ctrl_left.scan_code, self._ctrl_left.is_extended )
        self._strafe_toggle = strafe_toggle
        self._strafe_toggle_name = strafe_toggle_name or ""
        self._strafe_keys = set( strafe_keys or () )
        self._strafe_key_names = list( strafe_key_names or () )
        self._strafe_while = dict( strafe_while or {} )
        self._strafe_while_names = list( strafe_while_names or () )
        self._strafe_on = False
        self._strafe_held = set()
        self._strafe_toggle_held = False
        self._synth_ctrl = False
        self._bracket_depth = 0
        self._bracket_lifted = False
        self._record_key = _key_info( "m" )
        self._probe = _key_info( "c" )
        self._chord_swallow = set()
        self._seq_down = set()
        self._click_mode = "idle"
        self._click_draft = []
        self._click_seqs = {}
        self._click_seq_down = set()
        self._click_playing = False
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

        self._setup_mouse( settings )
        if plugin_name:
            self._attach_plugin( plugin_name )
        self._load_click_seqs()

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
        """True when the foreground window matches the captured title substring and/or exe."""
        if not self._target and not self._exe:
            return True
        if self._exe and foreground_exe().lower() != self._exe_lower:
            return False
        if self._target and self._target_lower not in foreground_title().lower():
            return False
        return True

    def _window_ok( self ):
        return self.window_ok()

    def _plugin_sees( self, stroke ):
        """Plugin sees in-game strokes, plus Ctrl/Alt always so its modifier flags stay accurate."""
        if self.plugin is None:
            return False
        if self.window_ok():
            return True
        if isinstance( stroke, KeyStroke ):
            return stroke.code in CTRL_SCANS or stroke.code in ALT_SCANS
        return False

    def _is_quit( self, stroke ):
        return ( stroke.code == self._quit.scan_code
                 and bool( stroke.flags & KeyFlag.KEY_E0 ) == self._quit.is_extended )

    def _is_record_key( self, stroke ):
        return ( stroke.code == self._record_key.scan_code
                 and bool( stroke.flags & KeyFlag.KEY_E0 ) == self._record_key.is_extended )

    def _is_probe( self, stroke ):
        return ( stroke.code == self._probe.scan_code
                 and bool( stroke.flags & KeyFlag.KEY_E0 ) == self._probe.is_extended )

    def _report_mouse( self ):
        x, y = get_mouse_pos()
        color = get_pixel_color( x, y )
        if color is None:
            print( f"{RED}Error: Could not read pixel at "
                   f"({MAGENTA}{x}{RESET}, {MAGENTA}{y}{RESET}){RESET}" )
            return
        _print_probe( x, y, color )

    def _load_click_seqs( self ):
        path = self._click_seqs_path
        if not path.is_file():
            return
        try:
            raw = json.loads( path.read_text( encoding = "utf-8" ) )
        except ( OSError, json.JSONDecodeError ) as exc:
            print( f"{YELLOW}Warning: cannot read {CYAN}{path}{RESET} ({exc})" )
            return
        if not isinstance( raw, list ):
            print( f"{YELLOW}Warning: {CYAN}{path}{RESET} must be a JSON list" )
            return
        for item in raw:
            if not isinstance( item, dict ):
                continue
            bind = str( item.get( "bind" ) or "" ).strip().lower()
            clicks = item.get( "clicks" )
            if not bind or not isinstance( clicks, list ) or not clicks:
                continue
            try:
                info = _key_info( bind )
            except UnknownKeyError:
                print( f"{YELLOW}Warning: unknown click-sequence bind {CYAN}{bind}{RESET}" )
                continue
            lookup = ( info.scan_code, info.is_extended )
            if lookup == ( self._quit.scan_code, self._quit.is_extended ):
                print( f"{YELLOW}Warning: click-sequence {CYAN}{bind}{RESET} collides with quit_key" )
                continue
            steps = []
            for step in clicks:
                if not isinstance( step, dict ):
                    continue
                button = str( step.get( "button" ) or "left" ).strip().lower()
                if button not in ( "left", "right" ):
                    continue
                try:
                    steps.append( { "button": button, "x": int( step[ "x" ] ), "y": int( step[ "y" ] ) } )
                except ( KeyError, TypeError, ValueError ):
                    continue
            if not steps:
                continue
            self._click_seqs[ lookup ] = { "bind": bind, "clicks": steps }
            print( f"ℹ️ {GREEN}Remapper{RESET}: click-seq {CYAN}ctrl+{bind}{RESET} "
                   f"{MAGENTA}{len( steps )}{RESET} click(s)" )

    def _save_click_seqs( self ):
        payload = []
        for item in self._click_seqs.values():
            payload.append( {
                "bind": item[ "bind" ],
                "clicks": [ { "button": s[ "button" ], "x": s[ "x" ], "y": s[ "y" ] }
                            for s in item[ "clicks" ] ],
            } )
        try:
            self._click_seqs_path.write_text(
                json.dumps( payload, indent = 2 ) + "\n", encoding = "utf-8" )
        except OSError as exc:
            print( f"{RED}Error: cannot write {CYAN}{self._click_seqs_path}{RESET} ({exc})" )
            return
        print( f"ℹ️ {GREEN}Remapper{RESET}: saved {CYAN}{self._click_seqs_path}{RESET}" )

    def _note_click( self, stroke ):
        if self._click_mode != "recording" or not self.window_ok():
            return
        if stroke.button_flags & MouseButtonFlag.MOUSE_LEFT_BUTTON_DOWN:
            button = "left"
        elif stroke.button_flags & MouseButtonFlag.MOUSE_RIGHT_BUTTON_DOWN:
            button = "right"
        else:
            return
        x, y = get_mouse_pos()
        self._click_draft.append( { "button": button, "x": int( x ), "y": int( y ) } )
        print( f"ℹ️ {GREEN}Remapper{RESET}: {CYAN}{button}{RESET} "
               f"{MAGENTA}{x}{RESET},{MAGENTA}{y}{RESET}  "
               f"({MAGENTA}{len( self._click_draft )}{RESET})" )

    def _on_click_record_chord( self ):
        if self._click_playing:
            print( f"{YELLOW}Remapper: click record ignored (playing){RESET}" )
            return
        if self._click_mode == "recording":
            if not self._click_draft:
                print( f"{YELLOW}Remapper: click record cancelled (no clicks){RESET}" )
                self._click_mode = "idle"
                return
            self._click_mode = "binding"
            print( f"ℹ️ {GREEN}Remapper{RESET}: recorded {MAGENTA}{len( self._click_draft )}{RESET} "
                   f"click(s) — press the key to bind as {CYAN}ctrl-<key>{RESET}" )
            return
        if self._click_mode == "binding":
            print( f"{YELLOW}Remapper: click bind cancelled{RESET}" )
            self._click_mode = "idle"
            self._click_draft = []
            return
        if not self.window_ok():
            return
        self._click_mode = "recording"
        self._click_draft = []
        print( f"ℹ️ {GREEN}Remapper{RESET}: click record started — left/right click, then "
               f"{CYAN}ctrl+alt+m{RESET}" )

    def _commit_click_bind( self, bind_name ):
        self._click_mode = "idle"
        clicks = list( self._click_draft )
        self._click_draft = []
        try:
            info = _key_info( bind_name )
        except UnknownKeyError:
            print( f"{YELLOW}Remapper: unknown bind key {CYAN}{bind_name}{RESET}; cancelled{RESET}" )
            return
        lookup = ( info.scan_code, info.is_extended )
        if lookup == ( self._quit.scan_code, self._quit.is_extended ):
            print( f"{YELLOW}Remapper: {CYAN}{bind_name}{RESET} is quit_key; cancelled{RESET}" )
            return
        self._click_seqs[ lookup ] = { "bind": bind_name, "clicks": clicks }
        self._save_click_seqs()
        print( f"ℹ️ {GREEN}Remapper{RESET}: bound {CYAN}ctrl+{bind_name}{RESET} "
               f"({MAGENTA}{len( clicks )}{RESET} click(s))" )

    def _run_click_seq( self, clicks ):
        delay = self.click_sequence_delay_s
        hold = delay if delay > 0 else 0.01
        self._click_playing = True
        try:
            for step in clicks:
                self.move_to( step[ "x" ], step[ "y" ] )
                if delay > 0:
                    time.sleep( delay )
                self.click( step[ "button" ], times = 1, hold_s = hold )
                if delay > 0:
                    time.sleep( delay )
        finally:
            self._click_playing = False

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

    def _run_sequence( self, dests ):
        for i, dest in enumerate( dests ):
            self.tap_key( dest )
            if i + 1 < len( dests ) and self.sequence_delay_s > 0:
                time.sleep( self.sequence_delay_s )

    def key_up( self, key ):
        """Inject a key-up so a physically held modifier is not still down in the OS."""
        info = key if hasattr( key, "scan_code" ) else _key_info( key )
        up = KeyStroke( info.scan_code, KeyFlag.KEY_UP )
        if info.is_extended:
            up.flags |= KeyFlag.KEY_E0
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

    def _boost_ctrl_wheel( self, device, stroke ):
        """If Ctrl (and not Alt) is held, replay this wheel tick ctrl_wheel_repeat times."""
        if self.ctrl_wheel_repeat <= 1:
            return False
        if not ( stroke.button_flags & MouseButtonFlag.MOUSE_WHEEL ):
            return False
        if not self._ctrl or self._alt or not self.window_ok():
            return False
        extra = MouseStroke(
            MouseFlag.MOUSE_MOVE_RELATIVE, MouseButtonFlag.MOUSE_WHEEL,
            stroke.button_data, 0, 0 )
        delay = self.ctrl_wheel_delay_s
        self.ctx.send( device, stroke )
        for _ in range( self.ctrl_wheel_repeat - 1 ):
            if delay > 0:
                time.sleep( delay )
            self.ctx.send( device, extra )
        return True

    def _emit_ident( self, ident, is_up ):
        code, extended = ident
        flags = KeyFlag.KEY_UP if is_up else KeyFlag.KEY_DOWN
        if extended:
            flags |= KeyFlag.KEY_E0
        self.ctx.send( self.ctx.keyboard, KeyStroke( code, flags ) )

    def _remap_alt_wheel( self, stroke ):
        """
        Alt+wheel (Ctrl not held) repeats the look key and swallows the wheel.
        Alt is released around the burst so the game sees that key alone.
        """
        if not ( stroke.button_flags & MouseButtonFlag.MOUSE_WHEEL ):
            return False
        if not self._alt or self._ctrl or not self.window_ok():
            return False
        raw = int( stroke.button_data )
        if raw >= 0x8000:
            raw -= 0x10000
        if raw > 0:
            bound = self._alt_wheel_up
        elif raw < 0:
            bound = self._alt_wheel_down
        else:
            return False
        if bound is None:
            return False
        _name, info = bound
        notches = abs( raw ) // 120
        if notches < 1:
            notches = 1
        taps = notches * self.alt_wheel_repeat
        ident = ( info.scan_code, info.is_extended )
        delay = self.alt_wheel_delay_s

        def fire():
            held = list( self._alt_down )
            for held_ident in held:
                self._emit_ident( held_ident, True )
            try:
                for i in range( taps ):
                    self._emit_ident( ident, False )
                    if delay > 0:
                        time.sleep( delay )
                    self._emit_ident( ident, True )
                    if i + 1 < taps and delay > 0:
                        time.sleep( delay )
            finally:
                for held_ident in held:
                    self._emit_ident( held_ident, False )

        self._with_bracket( fire )
        return True

    def run( self ):
        if self._target or self._exe:
            parts = []
            if self._target:
                parts.append( f"title contains {CYAN}{self._target}{RESET}" )
            if self._exe:
                parts.append( f"exe {CYAN}{self._exe}{RESET}" )
            gate = "; ".join( parts )
        else:
            gate = ( f"{YELLOW}always on{RESET} — run {CYAN}remapper/capture-window.py{RESET} "
                     f"or set target_window" )
        extra = f"; plugin {CYAN}{self.plugin.__class__.__module__}{RESET}" if self.plugin else ""
        wheel = ""
        if self.ctrl_wheel_repeat > 1:
            wheel = f"; ctrl+wheel x{CYAN}{self.ctrl_wheel_repeat}{RESET}"
        strafe = ""
        if self._strafe_toggle_name:
            keys = ", ".join( self._strafe_key_names )
            strafe = ( f"; strafe {CYAN}{self._strafe_toggle_name}{RESET} "
                       f"({CYAN}{keys}{RESET})" )
            if self._strafe_while_names:
                shown = ", ".join( f"{CYAN}{name}{RESET}" for name in self._strafe_while_names )
                strafe += f" while on {shown}"
        look = ""
        if self._alt_wheel_up or self._alt_wheel_down:
            up = self._alt_wheel_up[ 0 ] if self._alt_wheel_up else "-"
            down = self._alt_wheel_down[ 0 ] if self._alt_wheel_down else "-"
            look = ( f"; alt+wheel {CYAN}{up}{RESET}/{CYAN}{down}{RESET}"
                     f" x{CYAN}{self.alt_wheel_repeat}{RESET}" )
        print( f"ℹ️ {GREEN}Remapper{RESET}: {gate}; "
               f"quit = {CYAN}{self._quit_name}{RESET}; "
               f"record = {CYAN}ctrl+alt+m{RESET}; "
               f"probe = {CYAN}ctrl+alt+c{RESET}{wheel}{look}{strafe}{extra}" )
        try:
            self._loop()
        finally:
            self._destroy()

    def _emit_ctrl( self, is_up ):
        flags = KeyFlag.KEY_UP if is_up else KeyFlag.KEY_DOWN
        info = self._ctrl_left
        if info.is_extended:
            flags |= KeyFlag.KEY_E0
        self.ctx.send( self.ctx.keyboard, KeyStroke( info.scan_code, flags ) )

    def _strafe_wants_ctrl( self ):
        """Synthetic Ctrl belongs down only while strafe mode is holding A/D in-game."""
        return ( self._strafe_on and bool( self._strafe_held )
                 and not self._alt and not self._ctrl and self._window_ok() )

    def _synth_refresh( self ):
        """Press or release injected Left Ctrl so it matches _strafe_wants_ctrl."""
        if self._bracket_depth:
            return
        # Physical Ctrl is shadowed on top of an injection; leave that key down.
        if self._ctrl and self._synth_ctrl:
            return
        want = self._strafe_wants_ctrl()
        if want and not self._synth_ctrl:
            self._emit_ctrl( False )
            self._synth_ctrl = True
        elif not want and self._synth_ctrl and not self._ctrl:
            self._emit_ctrl( True )
            self._synth_ctrl = False

    def _bracket_begin( self ):
        """Lift synthetic Ctrl so the next key or click is not a Ctrl chord."""
        self._bracket_depth += 1
        if self._bracket_depth == 1 and self._synth_ctrl and not self._ctrl:
            self._emit_ctrl( True )
            self._synth_ctrl = False
            self._bracket_lifted = True

    def _bracket_end( self ):
        if self._bracket_depth:
            self._bracket_depth -= 1
        if self._bracket_depth == 0 and self._bracket_lifted:
            self._bracket_lifted = False
            self._synth_refresh()

    def _with_bracket( self, fn ):
        self._bracket_begin()
        try:
            return fn()
        finally:
            self._bracket_end()

    def _take_physical_ctrl( self, stroke, is_up ):
        """
        Update physical Ctrl. Return True when this stroke must not be forwarded.

        A press while synthetic Ctrl is already down is swallowed (and its matching
        release too). Releasing the last physical Ctrl while strafe keys are held
        keeps Ctrl down for those keys instead of ending the strafe.
        """
        code = stroke.code
        extended = bool( stroke.flags & KeyFlag.KEY_E0 )
        ident = ( code, extended )
        if not is_up:
            self._ctrl_down.add( ident )
            self._ctrl = True
            if self._synth_ctrl:
                self._ctrl_shadow.add( ident )
                return True
            return False
        self._ctrl_down.discard( ident )
        self._ctrl = bool( self._ctrl_down )
        if ident in self._ctrl_shadow:
            self._ctrl_shadow.discard( ident )
            if not self._ctrl:
                self._synth_refresh()
            return True
        if ( self._strafe_on and self._strafe_held and not self._alt
             and self._window_ok() and not self._ctrl ):
            if ident == self._ctrl_left_src and not self._synth_ctrl:
                self._synth_ctrl = True
                return True
            if not self._synth_ctrl:
                self._emit_ctrl( False )
                self._synth_ctrl = True
            return False
        return False

    def _release_synth_ctrl( self ):
        self._bracket_depth = 0
        self._bracket_lifted = False
        if not self._synth_ctrl:
            return
        try:
            self._emit_ctrl( True )
        except Exception:
            pass
        self._synth_ctrl = False

    def _loop( self ):
        while not self._stop:
            device = self.ctx.await_input( self.AWAIT_TIMEOUT_MS )
            if device is None:
                continue

            stroke = self.ctx.devices[ device ].receive()
            if stroke is None:
                continue

            if isinstance( stroke, MouseStroke ):
                if self._remap_alt_wheel( stroke ):
                    continue
                if self._boost_ctrl_wheel( device, stroke ):
                    continue
                self._note_click( stroke )
                button = bool( int( stroke.button_flags ) & int( _MOUSE_BUTTON_MASK ) )
                if button:
                    self._bracket_begin()
                try:
                    if not ( self._plugin_sees( stroke ) and self.plugin.on_stroke( device, stroke ) ):
                        self.ctx.send( device, stroke )
                finally:
                    if button:
                        self._bracket_end()
                continue

            if not isinstance( stroke, KeyStroke ):
                self.ctx.send( device, stroke )
                continue

            is_up = bool( stroke.flags & KeyFlag.KEY_UP )
            src = ( stroke.code, bool( stroke.flags & KeyFlag.KEY_E0 ) )

            swallowed_ctrl = False
            if stroke.code in CTRL_SCANS:
                swallowed_ctrl = self._take_physical_ctrl( stroke, is_up )
            elif stroke.code in ALT_SCANS:
                ident = ( stroke.code, bool( stroke.flags & KeyFlag.KEY_E0 ) )
                if is_up:
                    self._alt_down.discard( ident )
                else:
                    self._alt_down.add( ident )
                self._alt = bool( self._alt_down )
                if not is_up:
                    self._synth_refresh()

            if not self._window_ok():
                self._synth_refresh()

            if self._is_quit( stroke ):
                if not is_up:
                    print( f"⏹️ {RED}quit{RESET} ({CYAN}{self._quit_name}{RESET})" )
                    self._stop = True
                continue

            if src in self._chord_swallow:
                if is_up:
                    self._chord_swallow.discard( src )
                continue

            if not is_up and self._ctrl and self._alt and self._is_probe( stroke ):
                self._chord_swallow.add( src )
                self._report_mouse()
                continue

            if not is_up and self._ctrl and self._alt and self._is_record_key( stroke ):
                self._chord_swallow.add( src )
                self._on_click_record_chord()
                continue

            if swallowed_ctrl:
                if self._plugin_sees( stroke ):
                    self.plugin.on_stroke( device, stroke )
                continue

            if self._strafe_toggle is not None and src == self._strafe_toggle:
                if not self._window_ok():
                    if is_up:
                        self._strafe_toggle_held = False
                else:
                    if is_up:
                        self._strafe_toggle_held = False
                    elif not self._strafe_toggle_held:
                        self._strafe_toggle_held = True
                        self._strafe_on = not self._strafe_on
                        if self._strafe_on:
                            print( f"ℹ️ {GREEN}strafe on{RESET}" )
                        else:
                            print( f"ℹ️ {YELLOW}strafe off{RESET}" )
                        self._synth_refresh()
                    continue

            if self._click_mode == "binding":
                if stroke.code in CTRL_SCANS or stroke.code in ALT_SCANS:
                    self._forward( device, stroke )
                    if stroke.code in ALT_SCANS and is_up:
                        self._synth_refresh()
                    continue
                if self._ctrl or self._alt:
                    self._with_bracket( lambda: self._forward( device, stroke ) )
                    continue
                if is_up:
                    self._with_bracket( lambda: self._forward( device, stroke ) )
                    continue
                name = KEY_NAMES.get( src )
                if name is None:
                    print( f"{YELLOW}Remapper: unrecognized bind key (scan "
                           f"{MAGENTA}0x{stroke.code:02x}{RESET}); cancelled{RESET}" )
                    self._click_mode = "idle"
                    self._click_draft = []
                    self._chord_swallow.add( src )
                    continue
                self._commit_click_bind( name )
                self._chord_swallow.add( src )
                continue

            if self._plugin_sees( stroke ):
                bracket = src not in self._strafe_keys
                if bracket:
                    self._bracket_begin()
                try:
                    consumed = self.plugin.on_stroke( device, stroke )
                finally:
                    if bracket:
                        self._bracket_end()
                if consumed:
                    continue

            clicks = self._click_seqs.get( src )
            if clicks is not None and src in self._click_seq_down:
                if is_up:
                    self._click_seq_down.discard( src )
                continue
            if ( clicks is not None and not is_up and not self._click_playing
                 and self._window_ok() and self._ctrl and not self._alt ):
                self._click_seq_down.add( src )
                self._with_bracket( lambda: self._run_click_seq( clicks[ "clicks" ] ) )
                continue

            seq = self.sequences.get( src )
            if seq is not None:
                if is_up:
                    if src in self._seq_down:
                        self._seq_down.discard( src )
                        continue
                    self._with_bracket( lambda: self._forward( device, stroke ) )
                    continue
                if src in self._seq_down:
                    continue
                if not self._window_ok() or self._alt or self._ctrl:
                    self._with_bracket( lambda: self._forward( device, stroke ) )
                    continue
                self._seq_down.add( src )
                self._with_bracket( lambda: self._run_sequence( seq ) )
                continue

            dest = self.binds.get( src )
            if dest is None and src in self._held:
                dest = self._held[ src ]
            elif dest is None and self._strafe_on:
                dest = self._strafe_while.get( src )

            # Finish a remapped hold with the dest key-up even if the window gate changed.
            # Strafe releases the arrow while Ctrl is still down, then drops Ctrl if that
            # was the last strafe key.
            if is_up and src in self._held:
                held_dest = self._held.pop( src )
                if src in self._strafe_keys:
                    self._send_dest( device, held_dest, is_up = True )
                    self._strafe_held.discard( src )
                    self._synth_refresh()
                else:
                    self._with_bracket(
                        lambda held_dest = held_dest: self._send_dest(
                            device, held_dest, is_up = True ) )
                continue

            # Alt-Tab / Alt-Enter: do not remap while Alt is held.
            # Physical Ctrl stays live so WASD still becomes arrows.
            gated = self._window_ok() and not self._alt
            if dest is not None and not is_up and ( src in self._held or gated ):
                self._held[ src ] = dest
                if src in self._strafe_keys:
                    self._strafe_held.add( src )
                    self._synth_refresh()
                    self._send_dest( device, dest, is_up = False )
                else:
                    self._with_bracket(
                        lambda dest = dest: self._send_dest( device, dest, is_up = False ) )
                continue

            if stroke.code in CTRL_SCANS or stroke.code in ALT_SCANS:
                self._forward( device, stroke )
                if stroke.code in ALT_SCANS and is_up:
                    self._synth_refresh()
                continue

            self._with_bracket( lambda: self._forward( device, stroke ) )

    def _destroy( self ):
        if self._destroyed:
            return
        self._destroyed = True
        print( f"ℹ️ {YELLOW}Closing Remapper...{RESET}" )
        self._release_synth_ctrl()
        try:
            self.ctx.destroy()
        except Exception:
            pass


def main():
    if len( sys.argv ) > 1:
        cfg_path = Path( sys.argv[ 1 ] ).expanduser().resolve()
    else:
        cfg_path = HERE / "remap.toml"

    ( target_window, target_exe, quit_name, quit_info, binds, bind_rows, plugin_name,
      sequences, seq_rows, sequence_delay_ms, click_sequence_delay_ms,
      ctrl_wheel_repeat, ctrl_wheel_delay_ms, alt_wheel_up, alt_wheel_down,
      alt_wheel_repeat, alt_wheel_delay_ms,
      strafe_toggle, strafe_keys, strafe_toggle_name, strafe_key_names,
      strafe_while, strafe_while_names ) = load_config( cfg_path )
    print( f"ℹ️ {GREEN}Remapper{RESET}: loaded {CYAN}{cfg_path}{RESET}" )
    for src_name, dest_name, src_info, dest_info in bind_rows:
        print( f"ℹ️   {CYAN}{src_name}{RESET} (scan {MAGENTA}0x{src_info.scan_code:02x}{RESET}) -> "
               f"{CYAN}{dest_name}{RESET} (scan {MAGENTA}0x{dest_info.scan_code:02x}{RESET})" )
    for src_name, dest_names, src_info in seq_rows:
        chain = " -> ".join( dest_names )
        print( f"ℹ️   {CYAN}{src_name}{RESET} sequence {CYAN}{chain}{RESET} "
               f"({MAGENTA}{sequence_delay_ms:.0f}{RESET} ms)" )

    remapper = Remapper( target_window, quit_name, quit_info, binds, plugin_name = plugin_name,
                         target_exe = target_exe, sequences = sequences,
                         sequence_delay_ms = sequence_delay_ms,
                         click_sequence_delay_ms = click_sequence_delay_ms,
                         click_seqs_path = cfg_path.parent / "click_sequences.json",
                         ctrl_wheel_repeat = ctrl_wheel_repeat,
                         ctrl_wheel_delay_ms = ctrl_wheel_delay_ms,
                         alt_wheel_up = alt_wheel_up, alt_wheel_down = alt_wheel_down,
                         alt_wheel_repeat = alt_wheel_repeat,
                         alt_wheel_delay_ms = alt_wheel_delay_ms,
                         strafe_toggle = strafe_toggle, strafe_keys = strafe_keys,
                         strafe_toggle_name = strafe_toggle_name,
                         strafe_key_names = strafe_key_names,
                         strafe_while = strafe_while,
                         strafe_while_names = strafe_while_names )
    try:
        remapper.run()
    except KeyboardInterrupt:
        print( f"⏹️ {RED}interrupted{RESET}" )
        remapper._destroy()


if __name__ == "__main__":
    main()
