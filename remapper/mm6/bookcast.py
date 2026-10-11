"""
MM6 bookcast plugin for remapper.

DEFINE records caster, book key, school click, spell click, then an optional target click
or H to flag a healing cast that aims at the lowest-HP portrait. BIND assigns ctrl-<key>.
EXECUTE skips the caster key if that portrait is already selected, opens the book, clicks
the school, double-clicks the spell, then either clicks a fixed target or (heal) the
lowest-HP player below the skip threshold.

Unselected portrait pixels and healthbar geometry come from remapper/mm6_status.json
(capture-healthbar.py), with a fallback to remapper/mm6_unselected.json for portraits only.
Loaded by remapper when plugin = "mm6".
"""

from dataclasses import dataclass
from pathlib import Path
import json
import sys
import time
import tomllib

from ansi import *
from pxl_config import get_settings
from pxl_lib import PIXELS, colors_similar, get_mouse_pos, get_pixel_color

from pyinterception.src.interception.constants import KeyFlag, MouseButtonFlag
from pyinterception.src.interception.strokes import KeyStroke, MouseStroke
from pyinterception.src.interception._keycodes import get_key_information
from pyinterception.src.interception.exceptions import UnknownKeyError

HERE = Path( __file__ ).resolve().parent
CONFIG_PATH = HERE / "mm6.toml"
CASTS_PATH = HERE / "mm6_bookcasts.json"
UNSELECTED_PATH = HERE / "mm6_unselected.json"
STATUS_PATH = HERE / "mm6_status.json"

SPELL_CLICK_PX = 12
HEAL_KEY = "h"

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


def _xy( raw ):
    return [ int( raw[ 0 ] ), int( raw[ 1 ] ) ]


def _rgb( raw ):
    return ( int( raw[ 0 ] ), int( raw[ 1 ] ), int( raw[ 2 ] ) )


def health_pct_from( fill, x, y_top, y_bottom, tolerance ):
    """Percent of column pixels matching fill_color from y_top through y_bottom, or None."""
    height = y_bottom - y_top + 1
    if height <= 0:
        return None
    hits = 0
    for y in range( y_top, y_bottom + 1 ):
        observed = get_pixel_color( x, y )
        if observed is not None and colors_similar( observed, fill, tolerance ):
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

    def to_json( self ):
        out = {
            "bind": self.bind,
            "caster": self.caster,
            "book": self.book,
            "school": list( self.school ),
            "spell": list( self.spell ),
        }
        if self.heal:
            out[ "heal" ] = True
        elif self.target is not None:
            out[ "target" ] = list( self.target )
        return out

    @classmethod
    def from_json( cls, raw ):
        target = raw.get( "target" )
        if target is not None:
            target = _xy( target )
        return cls(
            bind = str( raw[ "bind" ] ).strip().lower(),
            caster = str( raw[ "caster" ] ).strip().lower(),
            book = str( raw[ "book" ] ).strip().lower(),
            school = _xy( raw[ "school" ] ),
            spell = _xy( raw[ "spell" ] ),
            target = target,
            heal = bool( raw.get( "heal", False ) ),
        )


class BookcastPlugin:
    """
    Capture-loop plugin. on_stroke returns True when remapper must not remap or forward.
    """

    def __init__( self, remapper ):
        self.remapper = remapper
        self._load_config()
        self.unselected, self.healthbar, self.healthbar_x, self.portrait = self._load_status()
        self._register_health_pixels()
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
        self._draft_down = set()

        n = len( self.casts )
        u = len( self.unselected )
        print( f"ℹ️ {GREEN}mm6{RESET}: {MAGENTA}{n}{RESET} bookcast(s); "
               f"DEFINE = {CYAN}{self._chord_label()}{RESET}" )
        if u == 4:
            src = STATUS_PATH.name if STATUS_PATH.is_file() else UNSELECTED_PATH.name
            print( f"ℹ️ {GREEN}mm6{RESET}: unselected portraits loaded from {CYAN}{src}{RESET}" )
        else:
            print( f"{YELLOW}mm6: unselected portraits missing or incomplete "
                   f"({MAGENTA}{u}{RESET}/4); caster key will always be sent" )
        if self.healthbar is not None and len( self.healthbar_x ) == 4:
            parts = []
            for pn in ( "1", "2", "3", "4" ):
                pct = self.health_pct( pn )
                parts.append( f"{CYAN}{pn}{RESET}={MAGENTA}{pct:.0f}%{RESET}" if pct is not None
                              else f"{CYAN}{pn}{RESET}=?" )
            print( f"ℹ️ {GREEN}mm6{RESET}: health " + " ".join( parts ) )
        else:
            print( f"{YELLOW}mm6: healthbars not loaded; run {CYAN}capture-healthbar.py{RESET}" )
        if len( self.portrait ) == 4:
            parts = [ f"{CYAN}{pn}{RESET}=({MAGENTA}{self.portrait[pn][0]}{RESET},"
                      f"{MAGENTA}{self.portrait[pn][1]}{RESET})"
                      for pn in ( "1", "2", "3", "4" ) ]
            print( f"ℹ️ {GREEN}mm6{RESET}: portrait " + " ".join( parts ) )
        else:
            print( f"{YELLOW}mm6: portraits incomplete ({MAGENTA}{len( self.portrait )}{RESET}/4); "
                   f"run {CYAN}capture-portrait.py{RESET}" )
        for cast in self.casts:
            self._print_cast( cast )

    def _chord_label( self ):
        parts = []
        if self.define_ctrl:
            parts.append( "ctrl" )
        if self.define_alt:
            parts.append( "alt" )
        parts.append( self.define_key )
        return "+".join( parts )

    def _print_cast( self, cast ):
        if cast.heal:
            tgt = f" {GREEN}heal{RESET} (lowest HP)"
        elif cast.target:
            tgt = f" target {MAGENTA}{cast.target[0]},{cast.target[1]}{RESET}"
        else:
            tgt = ""
        print( f"ℹ️ {GREEN}mm6{RESET}: ctrl-{CYAN}{cast.bind}{RESET} -> "
               f"{CYAN}{cast.caster}{RESET} {CYAN}{cast.book}{RESET} "
               f"school {MAGENTA}{cast.school[0]},{cast.school[1]}{RESET} "
               f"spell {MAGENTA}{cast.spell[0]},{cast.spell[1]}{RESET}{tgt}" )

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
        if "unselected_tolerance" in raw:
            self.unselected_tolerance = int( raw[ "unselected_tolerance" ] )
        else:
            self.unselected_tolerance = get_settings()[ "color" ][ "default_tolerance" ]

    def _parse_unselected_entry( self, item ):
        color = item[ "color" ]
        return {
            "x": int( item[ "x" ] ),
            "y": int( item[ "y" ] ),
            "color": _rgb( color ),
        }

    def _load_unselected_file( self, path ):
        if not path.is_file():
            return {}
        try:
            raw = json.loads( path.read_text( encoding = "utf-8" ) )
        except ( OSError, json.JSONDecodeError ) as exc:
            print( f"{YELLOW}Warning: cannot read {CYAN}{path}{RESET} ({exc})" )
            return {}
        if not isinstance( raw, dict ):
            print( f"{YELLOW}Warning: {CYAN}{path}{RESET} must be a JSON object" )
            return {}
        # Legacy mm6_unselected.json is keyed 1-4; status file uses players.*
        src = raw.get( "players", raw )
        if not isinstance( src, dict ):
            return {}
        out = {}
        for n in ( "1", "2", "3", "4" ):
            item = src.get( n )
            if isinstance( item, dict ) and "unselected" in item:
                item = item[ "unselected" ]
            if not isinstance( item, dict ) or "color" not in item:
                continue
            try:
                out[ n ] = self._parse_unselected_entry( item )
            except ( KeyError, TypeError, ValueError, IndexError ):
                print( f"{YELLOW}Warning: bad unselected entry {CYAN}{n}{RESET} in "
                       f"{CYAN}{path}{RESET}" )
        return out

    def _load_status( self ):
        unselected = {}
        healthbar = None
        healthbar_x = {}
        portrait = {}
        if STATUS_PATH.is_file():
            try:
                raw = json.loads( STATUS_PATH.read_text( encoding = "utf-8" ) )
            except ( OSError, json.JSONDecodeError ) as exc:
                print( f"{YELLOW}Warning: cannot read {CYAN}{STATUS_PATH}{RESET} ({exc})" )
                raw = None
            if isinstance( raw, dict ):
                unselected = self._load_unselected_file( STATUS_PATH )
                hb = raw.get( "healthbar" )
                players = raw.get( "players" ) or {}
                try:
                    healthbar = {
                        "fill_color": _rgb( hb[ "fill_color" ] ),
                        "y_top": int( hb[ "y_top" ] ),
                        "y_bottom": int( hb[ "y_bottom" ] ),
                        "width": int( hb[ "width" ] ),
                    }
                    for n in ( "1", "2", "3", "4" ):
                        p = players.get( n )
                        if isinstance( p, dict ) and "healthbar_x" in p:
                            healthbar_x[ n ] = int( p[ "healthbar_x" ] )
                except ( KeyError, TypeError, ValueError, IndexError ) as exc:
                    print( f"{YELLOW}Warning: bad healthbar data in {CYAN}{STATUS_PATH}{RESET} "
                           f"({exc})" )
                    healthbar = None
                    healthbar_x = {}
                for n in ( "1", "2", "3", "4" ):
                    p = players.get( n ) if isinstance( players, dict ) else None
                    if not isinstance( p, dict ):
                        continue
                    por = p.get( "portrait" )
                    try:
                        if isinstance( por, dict ):
                            portrait[ n ] = ( int( por[ "x" ] ), int( por[ "y" ] ) )
                        elif isinstance( por, ( list, tuple ) ) and len( por ) >= 2:
                            portrait[ n ] = ( int( por[ 0 ] ), int( por[ 1 ] ) )
                    except ( KeyError, TypeError, ValueError, IndexError ):
                        print( f"{YELLOW}Warning: bad portrait for player {CYAN}{n}{RESET}" )
        if len( unselected ) < 4:
            fallback = self._load_unselected_file( UNSELECTED_PATH )
            for n, v in fallback.items():
                unselected.setdefault( n, v )
        return unselected, healthbar, healthbar_x, portrait

    def _register_health_pixels( self ):
        points = []
        for marker in self.unselected.values():
            points.append( ( marker[ "x" ], marker[ "y" ] ) )
        hb = self.healthbar
        if hb is not None:
            for x in self.healthbar_x.values():
                for y in range( hb[ "y_top" ], hb[ "y_bottom" ] + 1 ):
                    points.append( ( x, y ) )
        if points:
            PIXELS.register_points( points )

    def health_pct( self, player ):
        """Remaining health 0-100 for player '1'-'4', or None if bars are not loaded."""
        hb = self.healthbar
        x = self.healthbar_x.get( str( player ) )
        if hb is None or x is None:
            return None
        return health_pct_from(
            hb[ "fill_color" ], x, hb[ "y_top" ], hb[ "y_bottom" ], self.unselected_tolerance )

    def portrait_xy( self, player ):
        """Clickable portrait point (x, y) for player '1'-'4', or None if not captured."""
        return self.portrait.get( str( player ) )

    def _load_casts( self ):
        if not CASTS_PATH.is_file():
            return []
        try:
            raw = json.loads( CASTS_PATH.read_text( encoding = "utf-8" ) )
        except ( OSError, json.JSONDecodeError ) as exc:
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

    def _save_casts( self ):
        payload = [ c.to_json() for c in self.casts ]
        CASTS_PATH.write_text( json.dumps( payload, indent = 2 ) + "\n", encoding = "utf-8" )

    def _rebuild_binds( self ):
        self._by_bind = {}
        for cast in self.casts:
            try:
                info = _key_info( cast.bind )
            except UnknownKeyError:
                print( f"{YELLOW}Warning: unknown bookcast bind {CYAN}{cast.bind}{RESET}" )
                continue
            self._by_bind[ ( info.scan_code, info.is_extended ) ] = cast

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
            print( f"ℹ️ {GREEN}mm6{RESET}: school click {MAGENTA}{pos[0]},{pos[1]}{RESET}" )
        elif not self._far( self._draft_school, pos ) and self._draft_spell is None:
            pass
        elif self._draft_spell is None:
            self._draft_spell = pos
            print( f"ℹ️ {GREEN}mm6{RESET}: spell click {MAGENTA}{pos[0]},{pos[1]}{RESET}" )
        elif not self._far( self._draft_spell, pos ):
            pass
        else:
            self._draft_target = pos
            self._draft_heal = False
            print( f"ℹ️ {GREEN}mm6{RESET}: target click {MAGENTA}{pos[0]},{pos[1]}{RESET}" )
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

        if self._mode == "binding":
            if is_up:
                self.remapper._forward( device, stroke )
                return True
            name = self._stroke_name( stroke )
            if name is None:
                print( f"{YELLOW}mm6: unrecognized bind key (scan "
                       f"{MAGENTA}0x{stroke.code:02x}{RESET}); cancelled{RESET}" )
                self._mode = "idle"
                self._swallow_up.add( src )
                return True
            self._commit_bind( name )
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
                self._draft_target = None
                self._swallow_up.add( src )
                print( f"ℹ️ {GREEN}mm6{RESET}: heal target (lowest HP portrait)" )
                return True
            if src in self._draft_down:
                self.remapper._forward( device, stroke )
                return True
            self._draft_down.add( src )
            if name is not None:
                if self._draft_caster is None:
                    self._draft_caster = name
                    print( f"ℹ️ {GREEN}mm6{RESET}: caster {CYAN}{name}{RESET}" )
                elif self._draft_book is None:
                    self._draft_book = name
                    print( f"ℹ️ {GREEN}mm6{RESET}: book {CYAN}{name}{RESET}" )
            self.remapper._forward( device, stroke )
            return True

        if self._executing:
            return False

        if ( not is_up and self._ctrl and not self._alt
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
            print( f"{YELLOW}mm6: DEFINE ignored (busy){RESET}" )
            return
        self._mode = "defining"
        self._draft_caster = None
        self._draft_book = None
        self._draft_school = None
        self._draft_spell = None
        self._draft_target = None
        self._draft_heal = False
        self._draft_down = set()
        print( f"ℹ️ {GREEN}mm6{RESET}: DEFINE started — press caster, book key, "
               f"click school, click spell, optional target click or {CYAN}H{RESET} "
               f"(heal lowest HP), then {CYAN}{self._chord_label()}{RESET}" )

    def _finish_define( self ):
        caster, book, school, spell = (
            self._draft_caster, self._draft_book, self._draft_school, self._draft_spell )
        target = self._draft_target
        heal = self._draft_heal
        self._mode = "idle"
        if not caster or not book or not school or not spell:
            print( f"{YELLOW}mm6: DEFINE cancelled "
                   f"(need caster, book key, school click, and spell click){RESET}" )
            return
        self._pending = ( caster, book, school, spell, target, heal )
        self._mode = "binding"
        print( f"ℹ️ {GREEN}mm6{RESET}: DEFINE complete — press the key to bind as "
               f"{CYAN}ctrl-<key>{RESET}" )

    def _commit_bind( self, bind_name ):
        caster, book, school, spell, target, heal = self._pending
        self._mode = "idle"
        if heal:
            target = None
        cast = Bookcast( bind = bind_name, caster = caster, book = book,
                         school = school, spell = spell, target = target, heal = heal )
        self.casts = [ c for c in self.casts if c.bind != bind_name ]
        self.casts.append( cast )
        self._rebuild_binds()
        self._save_casts()
        print( f"ℹ️ {GREEN}mm6{RESET}: bound and saved" )
        self._print_cast( cast )

    def _caster_unselected( self, caster ):
        """True when the caster portrait matches the stored unselected color (key should be sent)."""
        marker = self.unselected.get( caster )
        if marker is None:
            return True
        observed = get_pixel_color( marker[ "x" ], marker[ "y" ] )
        if observed is None:
            return True
        return colors_similar( observed, marker[ "color" ], self.unselected_tolerance )

    def _heal_target( self ):
        """Portrait (x, y) of the lowest-HP player below heal_skip_pct, or None to abort."""
        if len( self.portrait ) < 4:
            print( f"{YELLOW}mm6: heal aborted "
                   f"(portraits incomplete, {MAGENTA}{len( self.portrait )}{RESET}/4){RESET}" )
            return None
        pcts = {}
        for n in ( "1", "2", "3", "4" ):
            pct = self.health_pct( n )
            if pct is None:
                print( f"{YELLOW}mm6: heal aborted (healthbars incomplete){RESET}" )
                return None
            pcts[ n ] = pct
        if all( p >= self.heal_skip_pct for p in pcts.values() ):
            print( f"ℹ️ {GREEN}mm6{RESET}: heal skipped "
                   f"(all ≥ {MAGENTA}{self.heal_skip_pct:.0f}%{RESET})" )
            return None
        n = min( pcts, key = lambda k: ( pcts[ k ], k ) )
        xy = self.portrait_xy( n )
        if xy is None:
            print( f"{YELLOW}mm6: heal aborted "
                   f"(no portrait for player {CYAN}{n}{RESET}){RESET}" )
            return None
        print( f"ℹ️ {GREEN}mm6{RESET}: heal -> player {CYAN}{n}{RESET} "
               f"{MAGENTA}{pcts[n]:.0f}%{RESET}" )
        return xy

    def _execute( self, cast ):
        print( f"ℹ️ {GREEN}mm6{RESET}: EXECUTE ctrl-{CYAN}{cast.bind}{RESET}" )
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
            target = heal_xy if cast.heal else cast.target
            if target is not None:
                self.remapper.move_to( target[ 0 ], target[ 1 ] )
                self._sleep_ms( "after_move" )
                self.remapper.click( "left", times = 1, hold_s = hold )
        finally:
            self._executing = False
