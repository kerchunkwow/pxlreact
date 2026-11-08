"""
MM bookcast plugin for remapper.

DEFINE records caster, book key, school click, spell click, then an optional target click,
H (heal lowest-HP portrait), or T (cursor: click wherever the mouse is at execute time).
BIND assigns ctrl-<key>. EXECUTE aborts if the caster's ready jewel does not show the stored
ready color. Otherwise it skips the caster key if that portrait is already selected, opens
the book, clicks the school, double-clicks the spell, then either clicks a fixed target,
the lowest-HP player below the skip threshold, or (cursor) the captured cursor position,
preceded by Esc when mm.toml cursor_esc is true. With no target, heal, or cursor flag, the
cursor is moved back to where it was when the bind was pressed.

Party size, selected/ready indicator pixels, portraits, and healthbar geometry come from
remapper/mm/mm_status.json, which Ctrl+Alt+<[mm8_capture] key> rewrites (mm8-capture.py) and
reloads. Loaded by remapper when plugin = "mm".
"""

from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
import json
import sys
import time
import tomllib

from ansi import *
from pxl_config import get_settings
from pxl_lib import PIXELS, colors_similar, get_mouse_pos, get_pixel_color, rgb_to_hex

from pyinterception.src.interception.constants import KeyFlag, MouseButtonFlag
from pyinterception.src.interception.strokes import KeyStroke, MouseStroke
from pyinterception.src.interception._keycodes import get_key_information
from pyinterception.src.interception.exceptions import UnknownKeyError

HERE = Path( __file__ ).resolve().parent
CONFIG_PATH = HERE / "mm.toml"
CASTS_PATH = HERE / "mm_bookcasts.json"
STATUS_PATH = HERE / "mm_status.json"

SPELL_CLICK_PX = 12
HEAL_KEY = "h"
CURSOR_KEY = "t"

_NAME_KEYS = (
    list( "0123456789abcdefghijklmnopqrstuvwxyz" )
    + [ f"f{i}" for i in range( 1, 13 ) ]
    + [ "space", "tab", "enter", "esc", "`", "-", "=", "[", "]", "\\", ";", "'", ",", ".", "/" ]
)


def _key_info( name ):
    return get_key_information( name.strip().lower() )


def _build_key_names():
    names = {}
    for name in _NAME_KEYS:
        try:
            info = _key_info( name )
        except UnknownKeyError:
            continue
        names[ ( info.scan_code, info.is_extended ) ] = name
    return names


def _mod_scan( *names ):
    scans = set()
    for name in names:
        try:
            scans.add( _key_info( name ).scan_code )
        except UnknownKeyError:
            pass
    return scans


KEY_NAMES = _build_key_names()
CTRL_SCANS = _mod_scan( "ctrlleft", "ctrlright", "ctrl" )
ALT_SCANS = _mod_scan( "altleft", "altright", "alt" )
SHIFT_SCANS = _mod_scan( "shiftleft", "shiftright", "shift" )
MOD_SCANS = CTRL_SCANS | ALT_SCANS | SHIFT_SCANS


def _bind_token( stroke ):
    """Friendly key name when known, otherwise sc<hex> or sc<hex>e0 for the raw scan."""
    src = ( stroke.code, bool( stroke.flags & KeyFlag.KEY_E0 ) )
    name = KEY_NAMES.get( src )
    if name is not None:
        return name
    token = f"sc{stroke.code:02x}"
    if src[ 1 ]:
        token += "e0"
    return token


def _bind_lookup( token ):
    """( scan_code, is_extended ) for a named key or an scXX / scXXe0 token."""
    token = str( token ).strip().lower()
    if token.startswith( "sc" ):
        rest = token[ 2: ]
        extended = rest.endswith( "e0" )
        hex_part = rest[ :-2 ] if extended else rest
        return ( int( hex_part, 16 ), extended )
    info = _key_info( token )
    return ( info.scan_code, info.is_extended )


def _xy( raw ):
    return [ int( raw[ 0 ] ), int( raw[ 1 ] ) ]


def _post_keys( raw ):
    if not raw:
        return None
    if isinstance( raw, str ):
        raw = [ raw ]
    if not isinstance( raw, list ) or not raw:
        return None
    keys = []
    for item in raw:
        name = str( item ).strip().lower()
        if not name:
            continue
        try:
            _key_info( name )
        except UnknownKeyError:
            print( f"{YELLOW}Warning: unknown post_keys entry {CYAN}{item}{RESET}" )
            continue
        keys.append( name )
    return keys or None


def _rgb( raw ):
    return ( int( raw[ 0 ] ), int( raw[ 1 ] ), int( raw[ 2 ] ) )


def _rgbs( raw ):
    """One [ r, g, b ] or a list of them (ready lists every jewel color that means ready)."""
    if isinstance( raw[ 0 ], list ):
        return [ _rgb( c ) for c in raw ]
    return [ _rgb( raw ) ]


def health_pct_from( min_chroma, x, y_top, y_bottom ):
    """
    Percent of column pixels from y_top through y_bottom that are bar fill, or None. The fill is a
    shaded gradient that recolors green -> yellow -> red as health drops, while the drained track
    is a dark gray, so fill is any pixel whose max - min channel spread reaches min_chroma.
    """
    height = y_bottom - y_top + 1
    if height <= 0:
        return None
    hits = 0
    for y in range( y_top, y_bottom + 1 ):
        observed = get_pixel_color( x, y )
        if observed is None:
            continue
        if max( observed ) - min( observed ) >= min_chroma:
            hits += 1
    return 100.0 * hits / height


@dataclass
class Bookcast:
    bind: str
    caster: str
    book: str
    school: list
    spell: list
    target: list | None = None
    heal: bool = False
    cursor: bool = False
    label: str = ""
    post_keys: list | None = None

    def display_label( self ):
        name = self.label.strip()
        return name if name else f"ctrl-{self.bind}"

    def to_json( self ):
        out = {}
        if self.label.strip():
            out[ "label" ] = self.label.strip()
        out.update( {
            "bind": self.bind,
            "caster": self.caster,
            "book": self.book,
            "school": list( self.school ),
            "spell": list( self.spell ),
        } )
        if self.cursor:
            out[ "cursor" ] = True
        elif self.heal:
            out[ "heal" ] = True
        elif self.target is not None:
            out[ "target" ] = list( self.target )
        if self.post_keys:
            out[ "post_keys" ] = list( self.post_keys )
        return out

    @classmethod
    def from_json( cls, raw ):
        target = raw.get( "target" )
        if target is not None:
            target = _xy( target )
        cursor = bool( raw.get( "cursor", False ) )
        heal = bool( raw.get( "heal", False ) )
        if cursor:
            heal = False
            target = None
        elif heal:
            target = None
        post_keys = _post_keys( raw.get( "post_keys" ) )
        return cls(
            bind = str( raw[ "bind" ] ).strip().lower(),
            caster = str( raw[ "caster" ] ).strip().lower(),
            book = str( raw[ "book" ] ).strip().lower(),
            school = _xy( raw[ "school" ] ),
            spell = _xy( raw[ "spell" ] ),
            target = target,
            heal = heal,
            cursor = cursor,
            label = str( raw.get( "label" ) or "" ).strip(),
            post_keys = post_keys,
        )


class BookcastPlugin:
    """
    Capture-loop plugin. on_stroke returns True when remapper must not remap or forward.
    """

    def __init__( self, remapper ):
        self.remapper = remapper
        self._load_config()
        self._mm8, self._mm8_src, self._status_src = self._load_mm8()
        self.casts = self._load_casts()
        self._by_bind = {}
        self._rebuild_binds()

        self._mode = "idle"
        self._ctrl = False
        self._alt = False
        self._executing = False
        self._swallow_up = set()
        self._define_armed = False
        self._draft_caster = None
        self._draft_book = None
        self._draft_school = None
        self._draft_spell = None
        self._draft_target = None
        self._draft_heal = False
        self._draft_cursor = False
        self._draft_down = set()

        n = len( self.casts )
        print( f"ℹ️ {GREEN}mm{RESET}: {MAGENTA}{n}{RESET} bookcast(s); "
               f"DEFINE = {CYAN}{self._chord_label()}{RESET}" )
        self.reload_status()
        for cast in self.casts:
            self._print_cast( cast )

    def _load_mm8( self ):
        """
        ( Mm8Capture, capture chord src, status chord src ) where src is ( scan, extended ), or
        ( None, None, None ).
        """
        mod = import_module( ".mm8-capture", __package__ )
        try:
            capture = mod.Mm8Capture()
            srcs = []
            for name in ( capture.key, capture.status_key ):
                key = name
                info = _key_info( name )
                srcs.append( ( info.scan_code, info.is_extended ) )
        except mod.CaptureConfigError as exc:
            print( f"{YELLOW}mm: party capture disabled ({exc}){RESET}" )
            return None, None, None
        except UnknownKeyError:
            print( f"{YELLOW}mm: party capture disabled (unknown [mm8_capture] key "
                   f"{CYAN}{key}{RESET}{YELLOW}){RESET}" )
            return None, None, None
        return capture, srcs[ 0 ], srcs[ 1 ]

    def _recapture( self ):
        """
        Run the MM8 capture sweep. Physical Ctrl/Alt are still down from the chord, so lift them
        in the OS while number keys are tapped (else the game sees Ctrl+Alt+N), then press them
        again so the physical key-ups stay paired.
        """
        rm = self.remapper
        held = list( rm._ctrl_down | rm._alt_down )
        for ident in held:
            rm._emit_ident( ident, True )
        self._executing = True
        try:
            self._sleep_ms( "after_key" )
            return self._mm8.capture( self._capture_tap )
        finally:
            for ident in held:
                rm._emit_ident( ident, False )
            self._executing = False

    def _capture_tap( self, key ):
        self.remapper.tap_key( key )

    def _observe( self, marker ):
        """( observed color or None, whether it matches the stored marker color )."""
        observed = get_pixel_color( marker[ "x" ], marker[ "y" ] )
        if observed is None:
            return None, False
        return observed, self._matches( observed, marker )

    def _show_status( self ):
        """
        Outline the captured mm_status.json points and print each PC's current selected/ready
        state and health as read from the screen now. Detects nothing and writes nothing.
        """
        ids = self.party_ids
        if not ids:
            print( f"{YELLOW}mm: no party in {CYAN}{STATUS_PATH.name}{RESET}{YELLOW}; capture with "
                   f"{CYAN}{self._capture_label()}{RESET}" )
            return
        marks = []
        bars = []
        print( f"ℹ️ {GREEN}mm{RESET}: status of {MAGENTA}{len( ids )}{RESET} PC(s) "
               f"from {CYAN}{STATUS_PATH.name}{RESET}" )
        for n in ids:
            parts = []
            for name, on_word, off_word, tag_on, tag_off in (
                    ( "selected", "selected", "unselected", "sel", "unsel" ),
                    ( "ready", "ready", "busy", "rdy", "busy" ) ):
                marker = getattr( self, name ).get( n )
                if marker is None:
                    parts.append( f"{YELLOW}{name} ?{RESET}" )
                    continue
                observed, on = self._observe( marker )
                seen = rgb_to_hex( observed ) if observed is not None else "?"
                parts.append( f"{on_word if on else off_word:<10} {MAGENTA}{seen}{RESET}" )
                x, y = marker[ "x" ], marker[ "y" ]
                marks.append( ( f"p{n} {tag_on if on else tag_off}", x, x, x, y ) )
            por = self.portrait.get( n )
            if por is not None:
                marks.append( ( f"p{n} por", por[ 0 ], por[ 0 ], por[ 0 ], por[ 1 ] ) )
            pct = self.health_pct( n )
            if pct is None:
                parts.append( f"{YELLOW}hp ?{RESET}" )
            else:
                parts.append( f"hp {MAGENTA}{pct:.0f}%{RESET}" )
                x = self.healthbar_x[ n ]
                bars.append( ( f"p{n} {pct:.0f}%", x, x, x ) )
            print( f"ℹ️   {CYAN}p{n}{RESET}  " + "  ".join( parts ) )
        if self.healthbar is not None:
            top, bottom = self.healthbar[ "y_top" ], self.healthbar[ "y_bottom" ]
        else:
            top = bottom = 0
        self._mm8.show( marks, bars, top, bottom )

    def _capture_label( self ):
        if self._mm8 is None:
            return "[mm8_capture] in mm.toml"
        return f"ctrl+alt+{self._mm8.key}"

    def reload_status( self ):
        """(Re)load mm_status.json, re-register its pixels, and print the party summary."""
        ( self.selected, self.healthbar, self.healthbar_x,
          self.portrait, self.ready, self.party_ids ) = self._load_status()
        self._register_health_pixels()
        self._print_status()

    def _print_status( self ):
        ids = self.party_ids
        if not ids:
            print( f"{YELLOW}mm: no party in {CYAN}{STATUS_PATH.name}{RESET}{YELLOW}; capture with "
                   f"{CYAN}{self._capture_label()}{RESET}" )
            return
        parts = []
        for pn in ids:
            pct = self.health_pct( pn )
            hp = f"{MAGENTA}{pct:.0f}%{RESET}" if pct is not None else "?"
            xy = self.portrait.get( pn )
            at = f" @{MAGENTA}{xy[0]},{xy[1]}{RESET}" if xy is not None else ""
            parts.append( f"{CYAN}{pn}{RESET}={hp}{at}" )
        print( f"ℹ️ {GREEN}mm{RESET}: party {MAGENTA}{len( ids )}{RESET}  " + "  ".join( parts ) )
        missing = [ name for name, data in ( ( "selected", self.selected ), ( "ready", self.ready ),
                                             ( "portrait", self.portrait ),
                                             ( "healthbar", self.healthbar_x ) )
                    if any( pn not in data for pn in ids ) ]
        if self.healthbar is None and "healthbar" not in missing:
            missing.append( "healthbar" )
        if missing:
            print( f"{YELLOW}mm: incomplete party data ({', '.join( missing )}); recapture with "
                   f"{CYAN}{self._capture_label()}{RESET}" )

    def _chord_label( self ):
        parts = []
        if self.define_ctrl:
            parts.append( "ctrl" )
        if self.define_alt:
            parts.append( "alt" )
        parts.append( self.define_key )
        return "+".join( parts )

    def _print_cast( self, cast ):
        if cast.cursor:
            tgt = f" {GREEN}cursor{RESET}"
        elif cast.heal:
            tgt = f" {GREEN}heal{RESET}"
        elif cast.target:
            tgt = f" target {MAGENTA}{cast.target[0]},{cast.target[1]}{RESET}"
        else:
            tgt = ""
        if cast.post_keys:
            tgt += f" post {CYAN}{' '.join( cast.post_keys )}{RESET}"
        bind = f"{CYAN}ctrl-{cast.bind}{RESET}"
        if cast.label.strip():
            print( f"ℹ️ {cast.label.strip()}  {bind}{tgt}" )
        else:
            print( f"ℹ️ {bind}{tgt}" )

    def _load_config( self ):
        try:
            with open( CONFIG_PATH, "rb" ) as fh:
                raw = tomllib.load( fh )
        except OSError as exc:
            print( f"{RED}Error: cannot read {CYAN}{CONFIG_PATH}{RESET} ({exc})" )
            sys.exit( 1 )
        except tomllib.TOMLDecodeError as exc:
            print( f"{RED}Error: invalid TOML in {CYAN}{CONFIG_PATH}{RESET} ({exc})" )
            sys.exit( 1 )

        self.define_ctrl = bool( raw.get( "define_ctrl", True ) )
        self.define_alt = bool( raw.get( "define_alt", True ) )
        self.define_key = ( raw.get( "define_key" ) or "b" ).strip().lower()
        try:
            self._define_info = _key_info( self.define_key )
        except UnknownKeyError:
            print( f"{RED}Error: unknown define_key {CYAN}{self.define_key}{RESET}" )
            sys.exit( 1 )

        raw_pass = raw.get( "execute_passthrough" ) or [ "a", "d" ]
        if isinstance( raw_pass, str ):
            raw_pass = [ raw_pass ]
        if not isinstance( raw_pass, list ):
            print( f"{RED}Error: execute_passthrough must be a list of keys{RESET}" )
            sys.exit( 1 )
        self._execute_passthrough = set()
        for name in raw_pass:
            try:
                info = _key_info( str( name ) )
            except UnknownKeyError:
                print( f"{YELLOW}Warning: unknown execute_passthrough key {CYAN}{name}{RESET}" )
                continue
            self._execute_passthrough.add( ( info.scan_code, info.is_extended ) )

        delays = raw.get( "delays_ms" ) or {}
        if not isinstance( delays, dict ):
            print( f"{RED}Error: delays_ms must be a table{RESET}" )
            sys.exit( 1 )
        self.delays = {
            "after_key": float( delays.get( "after_key", 50 ) ),
            "after_move": float( delays.get( "after_move", 40 ) ),
            "click_hold": float( delays.get( "click_hold", 40 ) ),
            "double_click": float( delays.get( "double_click", 50 ) ),
        }
        self.heal_skip_pct = float( raw.get( "heal_skip_pct", 99 ) )
        self.cursor_esc = bool( raw.get( "cursor_esc", True ) )
        if "status_tolerance" in raw:
            self.status_tolerance = int( raw[ "status_tolerance" ] )
        else:
            self.status_tolerance = get_settings()[ "color" ][ "default_tolerance" ]

    def _parse_marker( self, item ):
        return {
            "x": int( item[ "x" ] ),
            "y": int( item[ "y" ] ),
            "colors": _rgbs( item[ "color" ] ),
        }

    def _matches( self, observed, marker ):
        return any( colors_similar( observed, c, self.status_tolerance ) for c in marker[ "colors" ] )

    def _load_status( self ):
        """( selected, healthbar, healthbar_x, portrait, ready, party_ids ) from mm_status.json."""
        selected = {}
        healthbar = None
        healthbar_x = {}
        portrait = {}
        ready = {}
        ids = []
        if not STATUS_PATH.is_file():
            return selected, healthbar, healthbar_x, portrait, ready, ids
        try:
            raw = json.loads( STATUS_PATH.read_text( encoding = "utf-8" ) )
        except ( OSError, json.JSONDecodeError ) as exc:
            print( f"{YELLOW}Warning: cannot read {CYAN}{STATUS_PATH}{RESET} ({exc})" )
            return selected, healthbar, healthbar_x, portrait, ready, ids
        if not isinstance( raw, dict ):
            print( f"{YELLOW}Warning: {CYAN}{STATUS_PATH}{RESET} must be a JSON object" )
            return selected, healthbar, healthbar_x, portrait, ready, ids
        players = raw.get( "players" )
        if isinstance( players, dict ):
            ids = sorted( ( n for n, p in players.items() if n.isdigit() and isinstance( p, dict ) ),
                          key = int )
        hb = raw.get( "healthbar" )
        if isinstance( hb, dict ):
            try:
                healthbar = {
                    "fill_min_chroma": int( hb[ "fill_min_chroma" ] ),
                    "y_top": int( hb[ "y_top" ] ),
                    "y_bottom": int( hb[ "y_bottom" ] ),
                    "width": int( hb[ "width" ] ),
                }
            except ( KeyError, TypeError, ValueError, IndexError ) as exc:
                print( f"{YELLOW}Warning: bad healthbar data in {CYAN}{STATUS_PATH}{RESET} ({exc})" )
        for n in ids:
            p = players[ n ]
            try:
                if "healthbar_x" in p:
                    healthbar_x[ n ] = int( p[ "healthbar_x" ] )
                por = p.get( "portrait" )
                if isinstance( por, dict ):
                    portrait[ n ] = ( int( por[ "x" ] ), int( por[ "y" ] ) )
            except ( KeyError, TypeError, ValueError ):
                print( f"{YELLOW}Warning: bad healthbar/portrait for player {CYAN}{n}{RESET}" )
            for name, out in ( ( "selected", selected ), ( "ready", ready ) ):
                marker = p.get( name )
                if not isinstance( marker, dict ):
                    continue
                try:
                    out[ n ] = self._parse_marker( marker )
                except ( KeyError, TypeError, ValueError, IndexError ):
                    print( f"{YELLOW}Warning: bad {name} entry {CYAN}{n}{RESET} in "
                           f"{CYAN}{STATUS_PATH}{RESET}" )
        return selected, healthbar, healthbar_x, portrait, ready, ids

    def _register_health_pixels( self ):
        points = []
        for marker in self.selected.values():
            points.append( ( marker[ "x" ], marker[ "y" ] ) )
        for marker in self.ready.values():
            points.append( ( marker[ "x" ], marker[ "y" ] ) )
        hb = self.healthbar
        if hb is not None:
            for x in self.healthbar_x.values():
                for y in range( hb[ "y_top" ], hb[ "y_bottom" ] + 1 ):
                    points.append( ( x, y ) )
        PIXELS.register_points( points )

    def health_pct( self, player ):
        """Remaining health 0-100 for a party member id, or None if bars are not loaded."""
        hb = self.healthbar
        x = self.healthbar_x.get( str( player ) )
        if hb is None or x is None:
            return None
        return health_pct_from( hb[ "fill_min_chroma" ], x, hb[ "y_top" ], hb[ "y_bottom" ] )

    def portrait_xy( self, player ):
        """Clickable portrait point (x, y) for a party member id, or None if not captured."""
        return self.portrait.get( str( player ) )

    def _load_casts( self ):
        if not CASTS_PATH.is_file():
            self._write_empty_casts()
            return []
        try:
            text = CASTS_PATH.read_text( encoding = "utf-8" )
        except OSError as exc:
            print( f"{RED}Error: cannot read {CYAN}{CASTS_PATH}{RESET} ({exc})" )
            sys.exit( 1 )
        if not text.strip():
            self._write_empty_casts()
            return []
        try:
            raw = json.loads( text )
        except json.JSONDecodeError as exc:
            print( f"{RED}Error: cannot read {CYAN}{CASTS_PATH}{RESET} ({exc})" )
            sys.exit( 1 )
        if not isinstance( raw, list ):
            print( f"{RED}Error: {CYAN}{CASTS_PATH}{RESET} must be a JSON list{RESET}" )
            sys.exit( 1 )
        casts = []
        for item in raw:
            try:
                casts.append( Bookcast.from_json( item ) )
            except ( KeyError, TypeError, ValueError, IndexError ) as exc:
                print( f"{YELLOW}Warning: skipping bookcast in {CYAN}{CASTS_PATH}{RESET} "
                       f"(re-DEFINE; needs school click) ({exc})" )
        return casts

    def _write_empty_casts( self ):
        CASTS_PATH.write_text( "[]\n", encoding = "utf-8" )

    def _save_casts( self ):
        payload = [ c.to_json() for c in self.casts ]
        CASTS_PATH.write_text( json.dumps( payload, indent = 2 ) + "\n", encoding = "utf-8" )

    def _rebuild_binds( self ):
        self._by_bind = {}
        for cast in self.casts:
            try:
                lookup = _bind_lookup( cast.bind )
            except ( UnknownKeyError, ValueError, IndexError ):
                print( f"{YELLOW}Warning: unknown bookcast bind {CYAN}{cast.bind}{RESET}" )
                continue
            self._by_bind[ lookup ] = cast

    def _sleep_ms( self, name ):
        time.sleep( self.delays[ name ] / 1000.0 )

    def _is_define_key( self, stroke ):
        return ( stroke.code == self._define_info.scan_code
                 and bool( stroke.flags & KeyFlag.KEY_E0 ) == self._define_info.is_extended )

    def _chord_held( self ):
        return ( ( self._ctrl if self.define_ctrl else True )
                 and ( self._alt if self.define_alt else True ) )

    def _stroke_name( self, stroke ):
        return KEY_NAMES.get( ( stroke.code, bool( stroke.flags & KeyFlag.KEY_E0 ) ) )

    def _src( self, stroke ):
        return ( stroke.code, bool( stroke.flags & KeyFlag.KEY_E0 ) )

    def on_stroke( self, device, stroke ):
        if isinstance( stroke, MouseStroke ):
            return self._on_mouse( device, stroke )
        if isinstance( stroke, KeyStroke ):
            return self._on_key( device, stroke )
        return False

    def _on_mouse( self, device, stroke ):
        if self._mode != "defining":
            return False
        if not ( stroke.button_flags & MouseButtonFlag.MOUSE_LEFT_BUTTON_DOWN ):
            return False
        pos = list( get_mouse_pos() )
        if self._draft_school is None:
            self._draft_school = pos
            print( f"ℹ️ {GREEN}mm{RESET}: school click {MAGENTA}{pos[0]},{pos[1]}{RESET}" )
        elif not self._far( self._draft_school, pos ) and self._draft_spell is None:
            pass
        elif self._draft_spell is None:
            self._draft_spell = pos
            print( f"ℹ️ {GREEN}mm{RESET}: spell click {MAGENTA}{pos[0]},{pos[1]}{RESET}" )
        elif not self._far( self._draft_spell, pos ):
            pass
        else:
            self._draft_target = pos
            self._draft_heal = False
            self._draft_cursor = False
            print( f"ℹ️ {GREEN}mm{RESET}: target click {MAGENTA}{pos[0]},{pos[1]}{RESET}" )
        return False

    def _far( self, a, b ):
        return abs( a[ 0 ] - b[ 0 ] ) > SPELL_CLICK_PX or abs( a[ 1 ] - b[ 1 ] ) > SPELL_CLICK_PX

    def _on_key( self, device, stroke ):
        is_up = bool( stroke.flags & KeyFlag.KEY_UP )
        src = self._src( stroke )

        if stroke.code in CTRL_SCANS:
            self._ctrl = not is_up
            return False
        if stroke.code in ALT_SCANS:
            self._alt = not is_up
            return False
        if stroke.code in SHIFT_SCANS:
            return False

        if src in self._swallow_up:
            if is_up:
                self._swallow_up.discard( src )
                if self._is_define_key( stroke ):
                    self._define_armed = False
            return True

        if self._is_define_key( stroke ) and self._chord_held() and not is_up:
            self._swallow_up.add( src )
            if not self._define_armed:
                self._define_armed = True
                self._on_define_chord()
            return True

        if ( self._mm8 is not None and src == self._mm8_src and not is_up
             and self._ctrl and self._alt and self._mode == "idle" and not self._executing ):
            self._swallow_up.add( src )
            if self._recapture():
                self.reload_status()
            return True

        if ( self._mm8 is not None and src == self._status_src and not is_up
             and self._ctrl and self._alt and self._mode == "idle" and not self._executing ):
            self._swallow_up.add( src )
            self._show_status()
            return True

        if self._mode == "binding":
            if is_up:
                self.remapper._forward( device, stroke )
                return True
            self._commit_bind( _bind_token( stroke ) )
            self._swallow_up.add( src )
            return True

        if self._mode == "defining":
            if is_up:
                self._draft_down.discard( src )
                self.remapper._forward( device, stroke )
                return True
            name = self._stroke_name( stroke )
            if name == HEAL_KEY and self._draft_spell is not None:
                self._draft_heal = True
                self._draft_cursor = False
                self._draft_target = None
                self._swallow_up.add( src )
                print( f"ℹ️ {GREEN}mm{RESET}: heal target (lowest HP portrait)" )
                return True
            if name == CURSOR_KEY and self._draft_spell is not None:
                self._draft_cursor = True
                self._draft_heal = False
                self._draft_target = None
                self._swallow_up.add( src )
                print( f"ℹ️ {GREEN}mm{RESET}: cursor target (click mouse position at execute)" )
                return True
            if src in self._draft_down:
                self.remapper._forward( device, stroke )
                return True
            self._draft_down.add( src )
            if name is not None:
                if self._draft_caster is None:
                    self._draft_caster = name
                    print( f"ℹ️ {GREEN}mm{RESET}: caster {CYAN}{name}{RESET}" )
                elif self._draft_book is None:
                    self._draft_book = name
                    print( f"ℹ️ {GREEN}mm{RESET}: book {CYAN}{name}{RESET}" )
            self.remapper._forward( device, stroke )
            return True

        if self._executing:
            return False

        if ( not is_up and self._ctrl and not self._alt
             and src not in self._execute_passthrough
             and src in self._by_bind and self.remapper.window_ok() ):
            self._swallow_up.add( src )
            self._execute( self._by_bind[ src ] )
            return True

        return False

    def _on_define_chord( self ):
        if self._mode == "defining":
            self._finish_define()
            return
        if self._mode != "idle" or self._executing:
            print( f"{YELLOW}mm: DEFINE ignored (busy){RESET}" )
            return
        if not self.remapper.window_ok():
            return
        self._mode = "defining"
        self._draft_caster = None
        self._draft_book = None
        self._draft_school = None
        self._draft_spell = None
        self._draft_target = None
        self._draft_heal = False
        self._draft_cursor = False
        self._draft_down = set()
        print( f"ℹ️ {GREEN}mm{RESET}: DEFINE started — press caster, book key, "
               f"click school, click spell, optional target click or {CYAN}H{RESET} "
               f"(heal lowest HP) or {CYAN}T{RESET} (cursor), then "
               f"{CYAN}{self._chord_label()}{RESET}" )

    def _finish_define( self ):
        caster, book, school, spell = (
            self._draft_caster, self._draft_book, self._draft_school, self._draft_spell )
        target = self._draft_target
        heal = self._draft_heal
        cursor = self._draft_cursor
        self._mode = "idle"
        if not caster or not book or not school or not spell:
            print( f"{YELLOW}mm: DEFINE cancelled "
                   f"(need caster, book key, school click, and spell click){RESET}" )
            return
        self._pending = ( caster, book, school, spell, target, heal, cursor )
        self._mode = "binding"
        print( f"ℹ️ {GREEN}mm{RESET}: DEFINE complete — press the key to bind as "
               f"{CYAN}ctrl-<key>{RESET}" )

    def _commit_bind( self, bind_name ):
        caster, book, school, spell, target, heal, cursor = self._pending
        self._mode = "idle"
        if cursor:
            target = None
            heal = False
        elif heal:
            target = None
        prev = next( ( c for c in self.casts if c.bind == bind_name ), None )
        label = prev.label if prev else ""
        post_keys = prev.post_keys if prev else None
        cast = Bookcast( bind = bind_name, caster = caster, book = book,
                         school = school, spell = spell, target = target, heal = heal,
                         cursor = cursor, label = label, post_keys = post_keys )
        self.casts = [ c for c in self.casts if c.bind != bind_name ]
        self.casts.append( cast )
        self._rebuild_binds()
        self._save_casts()
        print( f"ℹ️ {GREEN}mm{RESET}: bound and saved" )
        self._print_cast( cast )

    def _caster_unselected( self, caster ):
        """True unless the caster's selection aura shows the stored selected color (key should be sent)."""
        marker = self.selected.get( caster )
        if marker is None:
            return True
        observed = get_pixel_color( marker[ "x" ], marker[ "y" ] )
        if observed is None:
            return True
        return not self._matches( observed, marker )

    def _caster_busy( self, caster ):
        """True when the caster's ready jewel shows none of the stored ready colors."""
        marker = self.ready.get( caster )
        if marker is None:
            return False
        observed = get_pixel_color( marker[ "x" ], marker[ "y" ] )
        if observed is None:
            return False
        return not self._matches( observed, marker )

    def _heal_target( self ):
        """Portrait (x, y) of the lowest-HP player below heal_skip_pct, or None to abort."""
        ids = self.party_ids
        have = sum( 1 for n in ids if n in self.portrait )
        if not ids or have < len( ids ):
            print( f"{YELLOW}mm: heal aborted "
                   f"(portraits incomplete, {MAGENTA}{have}{RESET}/{len( ids )}){RESET}" )
            return None
        pcts = {}
        for n in ids:
            pct = self.health_pct( n )
            if pct is None:
                print( f"{YELLOW}mm: heal aborted (healthbars incomplete){RESET}" )
                return None
            pcts[ n ] = pct
        if all( p >= self.heal_skip_pct for p in pcts.values() ):
            print( f"ℹ️ {GREEN}mm{RESET}: heal skipped "
                   f"(all ≥ {MAGENTA}{self.heal_skip_pct:.0f}%{RESET})" )
            return None
        n = min( pcts, key = lambda k: ( pcts[ k ], k ) )
        xy = self.portrait_xy( n )
        if xy is None:
            print( f"{YELLOW}mm: heal aborted "
                   f"(no portrait for player {CYAN}{n}{RESET}){RESET}" )
            return None
        print( f"ℹ️ {GREEN}mm{RESET}: heal -> player {CYAN}{n}{RESET} "
               f"{MAGENTA}{pcts[n]:.0f}%{RESET}" )
        return xy

    def _release_held_ctrl( self ):
        """
        Execute starts on the bind key-down while Ctrl is still held. The capture loop is
        blocked for the whole cast, so the physical Ctrl-up is not forwarded until afterward.
        Lift Ctrl in the OS first or injected Esc is Ctrl+Esc (Start menu).
        """
        self._ctrl = False
        self.remapper.key_up( "ctrlleft" )
        self.remapper.key_up( "ctrlright" )
        time.sleep( 0.02 )

    def _execute( self, cast ):
        if self._caster_busy( cast.caster ):
            print( f"{YELLOW}mm: {cast.display_label()} busy{RESET}" )
            return
        print( f"{cast.display_label()}" )
        cursor_xy = None
        restore_xy = None
        if cast.cursor:
            pos = get_mouse_pos()
            cursor_xy = [ int( pos[ 0 ] ), int( pos[ 1 ] ) ]
            print( f"ℹ️ {GREEN}mm{RESET}: cursor {MAGENTA}{cursor_xy[0]},{cursor_xy[1]}{RESET}" )
        elif not cast.heal and cast.target is None:
            pos = get_mouse_pos()
            restore_xy = [ int( pos[ 0 ] ), int( pos[ 1 ] ) ]
        self._release_held_ctrl()
        if cast.cursor and self.cursor_esc:
            self.remapper.tap_key( "esc" )
            self._sleep_ms( "after_key" )
        heal_xy = None
        if cast.heal:
            heal_xy = self._heal_target()
            if heal_xy is None:
                return
        self._executing = True
        hold = self.delays[ "click_hold" ] / 1000.0
        dbl = self.delays[ "double_click" ] / 1000.0
        try:
            if self._caster_unselected( cast.caster ):
                marker = self.selected.get( cast.caster )
                if marker is None:
                    why = "no selected marker loaded"
                else:
                    observed, _ = self._observe( marker )
                    seen = rgb_to_hex( observed ) if observed is not None else "?"
                    why = ( f"{MAGENTA}{seen}{RESET} at {MAGENTA}{marker['x']},{marker['y']}{RESET}"
                            f", selected is {MAGENTA}{rgb_to_hex( marker['colors'][ 0 ] )}{RESET}" )
                print( f"ℹ️ {GREEN}mm{RESET}: selecting caster {CYAN}{cast.caster}{RESET} ({why})" )
                self.remapper.tap_key( cast.caster )
                self._sleep_ms( "after_key" )
            self.remapper.tap_key( cast.book )
            self._sleep_ms( "after_key" )
            self.remapper.move_to( cast.school[ 0 ], cast.school[ 1 ] )
            self._sleep_ms( "after_move" )
            self.remapper.click( "left", times = 1, hold_s = hold )
            self._sleep_ms( "after_move" )
            self.remapper.move_to( cast.spell[ 0 ], cast.spell[ 1 ] )
            self._sleep_ms( "after_move" )
            self.remapper.click( "left", times = 2, interval_s = dbl, hold_s = hold )
            if restore_xy is not None:
                self.remapper.move_to( restore_xy[ 0 ], restore_xy[ 1 ] )
                self._sleep_ms( "after_move" )
            if cast.cursor:
                target = cursor_xy
            elif cast.heal:
                target = heal_xy
            else:
                target = cast.target
            if target is not None:
                self.remapper.move_to( target[ 0 ], target[ 1 ] )
                self._sleep_ms( "after_move" )
                self.remapper.click( "left", times = 1, hold_s = hold )
            if cast.post_keys:
                self._sleep_ms( "after_key" )
                for key in cast.post_keys:
                    self.remapper.tap_key( key )
                    self._sleep_ms( "after_key" )
        finally:
            self._executing = False
