"""
pxl_remap.py provides keyboard-capture remapping for PxlReact.

A remap intercepts a single physical "source" key and, when pressed, sends the first qualifying key
from an ordered sequence. Qualification is driven by a per-entry predicate: a timed sequence uses
per-key cooldown timers, while a pixel-color sequence uses live pixel-color tests.

Capturing (as opposed to sending, which PxlIntercept handles) requires its own Interception context
with a keyboard filter and a blocking await/receive/send loop run on a background thread.
"""

import random
import threading
import time
from dataclasses import dataclass, field

# Local pyinterception clone (do not modify)
import pyinterception.src.interception as pyint
from pyinterception.src.interception.constants import KeyFlag, FilterKeyFlag, MouseFlag, MouseButtonFlag
from pyinterception.src.interception.strokes import KeyStroke, MouseStroke
from pyinterception.src.interception._keycodes import get_key_information

from pxl_config import get_settings
from pxl_intercept import detect_device_index
from pxl_lib import ColorCondition, PixelMonitor, CastLock
from ansi import *

# Substitute values for Action.key that send a mouse click at the current cursor position
MOUSE_BUTTONS = frozenset( ( 'left', 'right', 'middle' ) )


@dataclass
class Action:
    """
    A single game action: a substitute to send (keyboard key or mouse button), gated by an optional
    cooldown and zero or more pixel-color conditions (all of which must hold), with an optional cast
    time during which other actions must not interrupt it.

    For `key`, use a keyboard key name (e.g. "e") or a mouse button: "left", "right", or "middle".
    Mouse substitutes click at the current cursor position without moving the pointer.
    """

    name: str
    key: str
    cooldown: float = 0.0
    cast_time: float = 0.0

    # Color gate: a list of ColorCondition that must ALL hold (logical AND) for the action to fire.
    # An empty list means "no color check"; color_ready short-circuits to True.
    color_checks: list = field( default_factory = list )

    # Last fire time (perf_counter); negative means never fired
    last: float = field( default = -1.0, init = False )

    def cooldown_ready( self ):
        return self.last < 0 or ( time.perf_counter() - self.last ) >= self.cooldown

    def color_ready( self ):
        return all( cond.passes() for cond in self.color_checks )

    def ready( self ):
        return self.cooldown_ready() and self.color_ready()

    def fire( self ):
        self.last = time.perf_counter()

    def cooldown_remaining( self ):
        """Seconds until the cooldown gate reopens (0.0 when ready or never fired)."""
        if self.cooldown > 0 and self.last >= 0:
            return max( 0.0, self.cooldown - ( time.perf_counter() - self.last ) )
        return 0.0


class Rotation:
    """
    An ordered list of Actions; resolves to the first ready action.
    """

    def __init__( self, actions ):
        self.actions = actions

    def resolve( self ):
        """Return the first action whose readiness predicate passes, or None."""
        for action in self.actions:
            if action.ready():
                return action
        return None


def _build_color_checks( color_check ):
    """
    Normalize a `color_check` config into a list of ColorCondition (all ANDed at resolve time).

    Accepts either a single condition dict { px, py, color, match? } or a list of such dicts. A
    missing/empty/None value yields an empty list (no color gate). Each condition's `match` defaults
    to True ("pixel must be this color"); set match = False for "pixel must NOT be this color".
    """
    if not color_check:
        return []
    conditions = color_check if isinstance( color_check, list ) else [ color_check ]
    out = []
    for cc in conditions:
        if not cc:
            continue
        out.append(
            ColorCondition(
                px = cc[ 'px' ],
                py = cc[ 'py' ],
                color = cc[ 'color' ],
                tolerance = cc[ 'tolerance' ],
                match = cc.get( 'match', True ),
            )
        )
    return out


def build_actions( actions_cfg ):
    """
    Build a name -> Action map from an ACTIONS config dict. A missing or empty `color_check` is
    normalized to no color gate (empty condition list).
    """
    out = {}
    for name, c in actions_cfg.items():
        key = c[ 'key' ]
        if key.lower() in MOUSE_BUTTONS:
            key = key.lower()
        out[ name ] = Action(
            name = name,
            key = key,
            cooldown = c.get( 'cooldown', 0.0 ),
            cast_time = c.get( 'cast_time', 0.0 ),
            color_checks = _build_color_checks( c.get( 'color_check' ) ),
        )
    return out


def build_rotations( rotations_cfg, actions ):
    """
    Build a name -> Rotation map from ROTATIONS config { name: { key, actions } }. Rotations
    reference shared Action instances (from `actions`) so cooldown state is shared across every
    rotation that uses a given action.
    """
    rotations = {}
    for rname, cfg in rotations_cfg.items():
        resolved = []
        for aname in cfg[ 'actions' ]:
            if aname not in actions:
                raise ValueError( f"Rotation '{rname}' references unknown action '{aname}'" )
            resolved.append( actions[ aname ] )
        rotations[ rname ] = Rotation( resolved )
    return rotations


class PxlRemapper:
    """
    Captures keyboard input and remaps configured source keys to sequenced substitutes.

    Driver filters are device-wide, so the entire keyboard is captured; every stroke that is not a
    remapped source (or command hotkey) is re-sent unchanged.

    Substitutes are sent through this same capture context, on the loop thread. Sends from the
    filter-owning context pass downstream without being re-intercepted (the same path as ordinary
    passed-through typing), which is what makes a source-equal substitute such as e -> e work; an
    earlier design sent through a separate context, whose fresh injections WERE re-intercepted and
    swallowed. Sending on the loop thread also avoids racing the device's shared stroke buffer
    against receive().

    This class also owns the application's command hotkeys (formerly the keyboard-library KEYBINDS):
    F12 / ESC quit, Ctrl+P reports the mouse color, and Ctrl+<reload_key> (default R) reloads
    profile.json. These are intercepted (not forwarded) and are not gated by wincheck.
    """

    def __init__( self, wincheck, actions, rotations, on_quit = None, cast_lock = None,
                  hub = None, on_reload = None, pause_event = None ):
        """
        Args:
            wincheck (PxlWinCheck): gating; remaps apply only while wincheck.check() returns True.
            actions (dict): ACTIONS config { action_name: { key, cooldown, cast_time, color_check } }
            rotations (dict): ROTATIONS config { rotation_name: { key, actions: [ action_name ] } };
                each rotation's `key` is the physical source key it captures.
            on_quit (callable | None): invoked when the F12/ESC quit hotkey is pressed.
            cast_lock (CastLock | None): shared cast gate; while active, remap presses are dropped
                and reactions also refuse to fire, so neither side interrupts a cast. A private lock
                is created if none is supplied.
            hub (StatusHub | None): reporting funnel for runtime state (status bar). A private hub
                is created if none is supplied.
            on_reload (callable | None): invoked when the Ctrl+<reload_key> hotkey is pressed.
            pause_event (threading.Event | None): shared pause gate. F12 sets it (entering pause);
                while set, remapped keys drop to raw pass-through, F11 clears it (resuming), and a
                second F12 quits. A private Event is created if none given.
        """
        self.wincheck = wincheck
        self.on_quit = on_quit
        self.on_reload = on_reload
        self.pause_event = pause_event if pause_event is not None else threading.Event()

        if hub is None:
            from pxl_status import StatusHub
            hub = StatusHub()
        self.hub = hub

        settings = get_settings()

        # Poll interval (ms) for await_input so the loop can observe the stop flag
        self.AWAIT_TIMEOUT_MS = settings[ 'remapper' ][ 'await_timeout_ms' ]

        # Humanized hold (seconds) for an injected substitute key, mirroring PxlIntercept press delays
        self.MIN_HOLD = settings[ 'remapper' ][ 'min_hold' ]
        self.MAX_HOLD = settings[ 'remapper' ][ 'max_hold' ]

        # Shared cast lock: while blocked, remap presses are dropped and reactions defer firing
        # (armed by remap actions with cast_time and by cast-time reactions).
        self.cast_lock = cast_lock if cast_lock is not None else CastLock()

        self.ctx = pyint.Interception()
        self.ctx.set_filter( self.ctx.is_keyboard, FilterKeyFlag.FILTER_KEY_ALL )

        idx = detect_device_index( settings[ 'devices' ][ 'keyboard_hwid' ] )
        if idx is not None:
            self.ctx.keyboard = idx
            print( f"ℹ️ {GREEN}PxlRemapper{RESET}: capturing keyboard device {MAGENTA}{idx}{RESET}" )
        else:
            print( f"⚠️ {YELLOW}PxlRemapper: keyboard HWID not matched; using default device "
                   f"{MAGENTA}{self.ctx.keyboard}{RESET}" )

        midx = detect_device_index( settings[ 'devices' ][ 'mouse_hwid' ] )
        if midx is not None:
            self.ctx.mouse = midx
            print( f"ℹ️ {GREEN}PxlRemapper{RESET}: mouse device {MAGENTA}{midx}{RESET}" )
        else:
            print( f"⚠️ {YELLOW}PxlRemapper: mouse HWID not matched; using default device "
                   f"{MAGENTA}{self.ctx.mouse}{RESET}" )

        # Command hotkey scan codes; ctrl shares scan 0x1D for both left/right (E0 distinguishes)
        self._sc_esc = get_key_information( 'esc' ).scan_code
        self._sc_f12 = get_key_information( 'f12' ).scan_code
        self._sc_f11 = get_key_information( 'f11' ).scan_code
        self._sc_p = get_key_information( 'p' ).scan_code
        self._sc_reload = get_key_information( settings[ 'gui' ][ 'reload_key' ] ).scan_code
        self._sc_ctrl = get_key_information( 'ctrlleft' ).scan_code
        self._ctrl_down = False
        self._p_swallowed = False
        self._reload_swallowed = False

        # Ctrl+P toggles this single-pixel screen-discovery monitor
        self._pixel_monitor = PixelMonitor()

        self.rebind( actions, rotations )

        self._stop_event = threading.Event()
        self._thread = None
        self.start()

    def rebind( self, actions, rotations ):
        """
        (Re)build the action pool, rotations, and source-key bindings from config. Called at
        construction and again on profile reload; the loop thread reads `self.remaps` on each
        stroke, so swapping in fresh dicts takes effect immediately (cooldown state resets).
        """
        action_pool = build_actions( actions )
        rotation_pool = build_rotations( rotations, action_pool )

        # source-scancode -> remap lookup, plus per-source down-state for once-per-press
        new_remaps = {}     # scan_code -> ( extended_bool, Rotation )
        new_down = {}       # scan_code -> bool
        rotation_view = []  # ordered ( rotation_name, Rotation ) for the status bar
        for rotation_name, cfg in rotations.items():
            info = get_key_information( cfg[ 'key' ] )
            new_remaps[ info.scan_code ] = ( info.is_extended, rotation_pool[ rotation_name ] )
            new_down[ info.scan_code ] = False
            rotation_view.append( ( rotation_name, rotation_pool[ rotation_name ] ) )
            print( f"ℹ️ {GREEN}PxlRemapper{RESET}: bound {CYAN}{cfg[ 'key' ]}{RESET} "
                   f"(scan {MAGENTA}0x{info.scan_code:02x}{RESET}) -> rotation {MAGENTA}{rotation_name}{RESET}" )

        self.remaps = new_remaps
        self.down = new_down
        self.hub.set_rotation_view( rotation_view )

    def start( self ):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread( target = self._run, name = 'PxlRemapper', daemon = True )
        self._thread.start()

    def stop( self ):
        print( f"ℹ️ {YELLOW}Closing PxlRemapper...{RESET}" )
        self._stop_event.set()
        self._pixel_monitor.stop()
        if self._thread:
            self._thread.join( timeout = 2.0 )
        try:
            self.ctx.destroy()
        except Exception:
            pass

    def _match( self, stroke ):
        """Return ( scan_code, rotation ) if the stroke matches a remapped source, else None."""
        entry = self.remaps.get( stroke.code )
        if entry is None:
            return None

        extended, rotation = entry
        stroke_extended = bool( stroke.flags & KeyFlag.KEY_E0 )
        if stroke_extended != extended:
            return None

        return stroke.code, rotation

    def _run( self ):
        try:
            while not self._stop_event.is_set():
                device = self.ctx.await_input( self.AWAIT_TIMEOUT_MS )
                if device is None:
                    continue

                stroke = self.ctx.devices[ device ].receive()
                if stroke is None:
                    continue

                # Only keyboard strokes are filtered; anything else passes straight through
                if not isinstance( stroke, KeyStroke ):
                    self.ctx.send( device, stroke )
                    continue

                # Command hotkeys (quit / report color) take precedence and are not gated
                if self._handle_command( device, stroke ):
                    continue

                match = self._match( stroke )
                if match is None:
                    # Not a remapped key; re-send unchanged so normal typing is preserved
                    self.ctx.send( device, stroke )
                    continue

                scan_code, rotation = match
                is_up = bool( stroke.flags & KeyFlag.KEY_UP )

                if is_up:
                    # Release of a remapped source; reset down-state and swallow
                    self.down[ scan_code ] = False
                    continue

                if self.down[ scan_code ]:
                    # Auto-repeat while held; ignore until released (once-per-press)
                    continue

                self.down[ scan_code ] = True

                if self.pause_event.is_set():
                    # Paused: remapped keys behave like the raw key and never fire; resume is
                    # F11-only (handled in _handle_command).
                    self.ctx.send( device, stroke )
                    continue

                if not self.wincheck.check():
                    # Outside the target app: behave like the real key (silently)
                    self.ctx.send( device, stroke )
                    continue

                if self.cast_lock.blocked():
                    # A cast is in progress; drop this press so the cast is not interrupted and
                    # flash the status bar frame.
                    # FUTURE: to queue instead of dropping, buffer the rotation here and flush at
                    # cast-end by shrinking the await_input timeout to wake the loop when the cast
                    # elapses, then resolve/fire each buffered press in order.
                    self.hub.record_drop()
                    continue

                self._fire( rotation )
        except Exception as exc:
            print( f"{RED}PxlRemapper loop error: {exc}{RESET}" )
        finally:
            try:
                self.ctx.destroy()
            except Exception:
                pass

    def _handle_command( self, device, stroke ):
        """
        Handle the application's command hotkeys. Returns True if the stroke was consumed (and must
        not be processed further); ctrl is forwarded so it still works for the focused app, while
        quit / report-color keys are swallowed.
        """
        is_up = bool( stroke.flags & KeyFlag.KEY_UP )
        code = stroke.code

        # Track (and forward) ctrl so Ctrl+P can be detected without breaking normal ctrl usage
        if code == self._sc_ctrl:
            self._ctrl_down = not is_up
            self.ctx.send( device, stroke )
            return True

        # F12 pause/quit toggle: first press pauses; a second press while paused quits. Resume from
        # pause is F11-only (below). Both edges are swallowed.
        if code == self._sc_f12:
            if not is_up:
                if self.pause_event.is_set():
                    print( f"⏹️ {RED}quit hotkey{RESET}" )
                    if self.on_quit is not None:
                        self.on_quit()
                else:
                    self.pause_event.set()
                    self.hub.set_paused( True )
                    print( f"⏸️ {YELLOW}paused{RESET} - F11 resumes, F12 quits" )
            return True

        # F11 resume: only meaningful while paused (clears the pause gate); a no-op otherwise.
        # Swallowed in both states so it never reaches the game.
        if code == self._sc_f11:
            if not is_up and self.pause_event.is_set():
                self.pause_event.clear()
                self.hub.set_paused( False )
                print( f"▶️ {GREEN}resumed{RESET}" )
            return True

        # Ctrl+P -> toggle the pixel monitor (reports the pixel under the cursor when its color
        # changes, until Ctrl+P is pressed again)
        if code == self._sc_p and ( self._ctrl_down or self._p_swallowed ):
            if not is_up:
                self._p_swallowed = True
                self._pixel_monitor.toggle()
            else:
                self._p_swallowed = False
            return True

        # Ctrl+<reload_key> -> reload profile.json (applied by the main loop between ticks)
        if code == self._sc_reload and ( self._ctrl_down or self._reload_swallowed ):
            if not is_up:
                self._reload_swallowed = True
                if self.on_reload is not None:
                    self.on_reload()
            else:
                self._reload_swallowed = False
            return True

        return False

    def _press_substitute( self, key ):
        """
        Inject a single press of `key` through the capture context.

        `key` may be a keyboard key name or a mouse button (left / right / middle). Keyboard
        substitutes use KeyStrokes on ctx.keyboard; mouse substitutes send button down/up on
        ctx.mouse at the current cursor position (no pointer move). Modifier-bearing keyboard
        substitutes are not supported; game remaps use bare keys or mouse buttons.
        """
        hold = random.uniform( self.MIN_HOLD, self.MAX_HOLD )
        btn = key.lower()
        if btn in MOUSE_BUTTONS:
            down_flag, up_flag = MouseButtonFlag.from_string( btn )
            self.ctx.send( self.ctx.mouse,
                           MouseStroke( MouseFlag.MOUSE_MOVE_ABSOLUTE, down_flag, 0, 0, 0 ) )
            time.sleep( hold )
            self.ctx.send( self.ctx.mouse,
                           MouseStroke( MouseFlag.MOUSE_MOVE_ABSOLUTE, up_flag, 0, 0, 0 ) )
            return

        info = get_key_information( key )

        down = KeyStroke( info.scan_code, KeyFlag.KEY_DOWN )
        up = KeyStroke( info.scan_code, KeyFlag.KEY_UP )
        if info.is_extended:
            down.flags |= KeyFlag.KEY_E0
            up.flags |= KeyFlag.KEY_E0

        self.ctx.send( self.ctx.keyboard, down )
        time.sleep( hold )
        self.ctx.send( self.ctx.keyboard, up )

    def _fire( self, rotation ):
        """
        Resolve the rotation and send the chosen action's key (if any). An action with a cast_time
        arms the global cast lock before the key is sent so neither remapped presses nor reactions
        can interrupt the cast. The fired ability is published to the hub; a press with nothing
        ready is silent (the status bar's live rotation rows already show why).
        """
        action = rotation.resolve()
        if action is None:
            return

        # Arm before send so a concurrent reaction poll cannot slip a key in between.
        self.cast_lock.begin( action.cast_time )
        self._press_substitute( action.key )
        action.fire()

        self.hub.record_ability( action.name )
