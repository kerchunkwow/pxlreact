"""
MM looping bookcast (all-party buff) for remapper.

Extends BookcastPlugin: every bookcast keeps its ctrl-<key> behavior, and ctrl+shift-<key>
casts the same caster/book/school/spell once on each party portrait listed in the remap TOML
`party_portraits`, ignoring the bookcast's own target/heal/cursor/post_keys. Between casts it
waits for the caster's busy bubble to appear and then clear (mm.toml [bookcast_loop]).

The cast blocks the capture loop, so pending input is drained between steps: Esc (or the
remapper quit key) aborts the loop, closing the spellbook/targeting if a cast is mid-flight.
Modifier and key-up strokes are passed on so nothing sticks; other key-downs, mouse motion,
and button-downs are dropped while the loop runs.
"""

from pathlib import Path
import sys
import time
import tomllib

from ansi import *

from pyinterception.src.interception.constants import KeyFlag, MouseButtonFlag
from pyinterception.src.interception.strokes import KeyStroke, MouseStroke

from .bookcast import (
    ALT_SCANS, CONFIG_PATH, CTRL_SCANS, SHIFT_SCANS, BookcastPlugin, _key_info,
)

REMAPPER_DIR = Path( __file__ ).resolve().parent.parent

_MOUSE_UP_MASK = (
    MouseButtonFlag.MOUSE_LEFT_BUTTON_UP
    | MouseButtonFlag.MOUSE_RIGHT_BUTTON_UP
    | MouseButtonFlag.MOUSE_MIDDLE_BUTTON_UP
    | MouseButtonFlag.MOUSE_BUTTON_4_UP
    | MouseButtonFlag.MOUSE_BUTTON_5_UP
)


class _LoopAbort( Exception ):
    pass


def _remap_toml_path():
    if len( sys.argv ) > 1:
        return Path( sys.argv[ 1 ] ).expanduser().resolve()
    return REMAPPER_DIR / "remap.toml"


class BookcastLoopPlugin( BookcastPlugin ):

    def __init__( self, remapper ):
        super().__init__( remapper )
        self._shift = False
        self._abort = None
        self._ui_open = False
        self._esc = _key_info( "esc" )
        self.party = self._load_party()
        self._load_loop_config()
        if self.party:
            print( f"ℹ️ {GREEN}mm loop{RESET}: {MAGENTA}{len( self.party )}{RESET} party portrait(s); "
                   f"{CYAN}ctrl+shift-<key>{RESET} loops a bookcast, {CYAN}esc{RESET} aborts" )
        else:
            print( f"{YELLOW}mm loop: no party_portraits in {CYAN}{_remap_toml_path().name}{RESET}"
                   f"{YELLOW}; looping disabled{RESET}" )

    def _load_party( self ):
        path = _remap_toml_path()
        try:
            with open( path, "rb" ) as fh:
                raw = tomllib.load( fh )
        except ( OSError, tomllib.TOMLDecodeError ) as exc:
            print( f"{YELLOW}Warning: cannot read {CYAN}{path}{RESET} ({exc})" )
            return []
        items = raw.get( "party_portraits" ) or []
        if not isinstance( items, list ):
            print( f"{YELLOW}Warning: party_portraits must be a list of [ x, y ]{RESET}" )
            return []
        party = []
        for i, item in enumerate( items, 1 ):
            try:
                party.append( ( int( item[ 0 ] ), int( item[ 1 ] ) ) )
            except ( TypeError, ValueError, IndexError ):
                print( f"{YELLOW}Warning: bad party_portraits entry {MAGENTA}{i}{RESET}" )
        return party

    def _load_loop_config( self ):
        try:
            with open( CONFIG_PATH, "rb" ) as fh:
                raw = tomllib.load( fh ).get( "bookcast_loop" ) or {}
        except ( OSError, tomllib.TOMLDecodeError ):
            raw = {}
        self.loop_poll_s = float( raw.get( "poll_ms", 10 ) ) / 1000.0
        self.loop_appear_s = float( raw.get( "busy_appear_ms", 1000 ) ) / 1000.0
        self.loop_ready_s = float( raw.get( "ready_timeout_ms", 20000 ) ) / 1000.0
        self.loop_fallback_s = float( raw.get( "fallback_wait_ms", 2000 ) ) / 1000.0

    # --- trigger -----------------------------------------------------------------------------

    def _on_key( self, device, stroke ):
        is_up = bool( stroke.flags & KeyFlag.KEY_UP )
        if stroke.code in SHIFT_SCANS:
            self._shift = not is_up
            return False
        src = self._src( stroke )
        if ( not is_up and self._shift and self._ctrl and not self._alt
             and self._mode == "idle" and not self._executing and self.party
             and src not in self._swallow_up
             and src not in self._execute_passthrough
             and src in self._by_bind and self.remapper.window_ok() ):
            self._swallow_up.add( src )
            self._run_loop( self._by_bind[ src ] )
            return True
        return super()._on_key( device, stroke )

    # --- input drain while blocked -----------------------------------------------------------

    def _pump( self ):
        ctx = self.remapper.ctx
        while True:
            device = ctx.await_input( 0 )
            if device is None:
                return
            stroke = ctx.devices[ device ].receive()
            if stroke is None:
                continue
            if isinstance( stroke, KeyStroke ):
                self._pump_key( device, stroke )
            elif isinstance( stroke, MouseStroke ):
                if int( stroke.button_flags ) & int( _MOUSE_UP_MASK ):
                    ctx.send( device, stroke )

    def _pump_key( self, device, stroke ):
        rm = self.remapper
        is_up = bool( stroke.flags & KeyFlag.KEY_UP )
        src = self._src( stroke )

        if src == ( self._esc.scan_code, self._esc.is_extended ):
            if is_up:
                self._swallow_up.discard( src )
            else:
                self._swallow_up.add( src )
                self._abort = self._abort or "esc"
            return
        if rm._is_quit( stroke ):
            if not is_up:
                rm._stop = True
                self._abort = self._abort or "quit"
            return

        if stroke.code in CTRL_SCANS:
            self._ctrl = not is_up
            if not rm._take_physical_ctrl( stroke, is_up ):
                rm._forward( device, stroke )
            return
        if stroke.code in ALT_SCANS:
            if is_up:
                rm._alt_down.discard( src )
            else:
                rm._alt_down.add( src )
            rm._alt = bool( rm._alt_down )
            self._alt = rm._alt
            rm._forward( device, stroke )
            return
        if stroke.code in SHIFT_SCANS:
            self._shift = not is_up
            rm._forward( device, stroke )
            return

        if not is_up:
            return
        if src in self._swallow_up:
            self._swallow_up.discard( src )
            return
        if src in rm._held:
            dest = rm._held.pop( src )
            rm._strafe_held.discard( src )
            rm._send_dest( device, dest, is_up = True )
            return
        rm._forward( device, stroke )

    def _check( self ):
        self._pump()
        if self._abort:
            raise _LoopAbort( self._abort )

    def _sleep_checked( self, seconds ):
        end = time.perf_counter() + seconds
        while True:
            self._check()
            left = end - time.perf_counter()
            if left <= 0:
                return
            time.sleep( min( self.loop_poll_s, left ) )

    def _step_sleep( self, name ):
        self._sleep_ms( name )
        self._check()

    # --- loop --------------------------------------------------------------------------------

    def _release_held_mods( self ):
        self._release_held_ctrl()
        self._shift = False
        self.remapper.key_up( "shiftleft" )
        self.remapper.key_up( "shiftright" )
        time.sleep( 0.02 )

    def _cast_on( self, cast, xy ):
        hold = self.delays[ "click_hold" ] / 1000.0
        dbl = self.delays[ "double_click" ] / 1000.0
        if self._caster_unselected( cast.caster ):
            self.remapper.tap_key( cast.caster )
            self._step_sleep( "after_key" )
        self._ui_open = True
        self.remapper.tap_key( cast.book )
        self._step_sleep( "after_key" )
        self.remapper.move_to( cast.school[ 0 ], cast.school[ 1 ] )
        self._step_sleep( "after_move" )
        self.remapper.click( "left", times = 1, hold_s = hold )
        self._step_sleep( "after_move" )
        self.remapper.move_to( cast.spell[ 0 ], cast.spell[ 1 ] )
        self._step_sleep( "after_move" )
        self.remapper.click( "left", times = 2, interval_s = dbl, hold_s = hold )
        self._step_sleep( "after_move" )
        self.remapper.move_to( xy[ 0 ], xy[ 1 ] )
        self._step_sleep( "after_move" )
        self.remapper.click( "left", times = 1, hold_s = hold )
        self._ui_open = False

    def _wait_ready( self, caster ):
        if caster not in self.busy:
            print( f"ℹ️ {GREEN}mm loop{RESET}: no busy pixel for {CYAN}{caster}{RESET}; "
                   f"waiting {MAGENTA}{self.loop_fallback_s * 1000:.0f}{RESET} ms" )
            self._sleep_checked( self.loop_fallback_s )
            return
        t0 = time.perf_counter()
        while not self._caster_busy( caster ):
            if time.perf_counter() - t0 > self.loop_appear_s:
                print( f"{YELLOW}mm loop: caster never went busy "
                       f"({MAGENTA}{self.loop_appear_s * 1000:.0f}{RESET}{YELLOW} ms); "
                       f"cast may have failed, continuing{RESET}" )
                return
            self._sleep_checked( self.loop_poll_s )
        t1 = time.perf_counter()
        while self._caster_busy( caster ):
            if time.perf_counter() - t1 > self.loop_ready_s:
                raise _LoopAbort( f"caster still busy after "
                                  f"{self.loop_ready_s * 1000:.0f} ms" )
            self._sleep_checked( self.loop_poll_s )
        print( f"ℹ️ {GREEN}mm loop{RESET}: ready after "
               f"{MAGENTA}{( time.perf_counter() - t1 ) * 1000:.0f}{RESET} ms" )

    def _run_loop( self, cast ):
        name = cast.display_label()
        if self._caster_busy( cast.caster ):
            print( f"{YELLOW}mm loop: {name} busy{RESET}" )
            return
        total = len( self.party )
        print( f"🔁 {GREEN}mm loop{RESET}: {name} x {MAGENTA}{total}{RESET}" )
        self._release_held_mods()
        self._executing = True
        self._abort = None
        self._ui_open = False
        done = 0
        try:
            for i, xy in enumerate( self.party, 1 ):
                self._check()
                if not self.remapper.window_ok():
                    raise _LoopAbort( "game window not focused" )
                print( f"ℹ️ {GREEN}mm loop{RESET}: PC {MAGENTA}{i}{RESET}/{MAGENTA}{total}{RESET} "
                       f"at {MAGENTA}{xy[0]},{xy[1]}{RESET}" )
                self._cast_on( cast, xy )
                done = i
                if i < total:
                    self._wait_ready( cast.caster )
            print( f"✅ {GREEN}mm loop: {name} complete ({done}/{total}){RESET}" )
        except _LoopAbort as exc:
            if self._ui_open:
                self.remapper.tap_key( "esc" )
                self._sleep_ms( "after_key" )
            print( f"⏹️ {YELLOW}mm loop: {name} aborted after "
                   f"{MAGENTA}{done}{RESET}{YELLOW}/{MAGENTA}{total}{RESET}{YELLOW} ({exc}){RESET}" )
        finally:
            self._ui_open = False
            self._abort = None
            self._executing = False
