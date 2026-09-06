"""Standalone cooldown / countdown shape indicators.

Lightweight always-on-top visual timers: pressing a bound key starts that
key's cooldown, showing a small borderless shape (bar or arc) that animates
(fill or drain) over a configured duration, then flashes and optionally beeps
on expiration before hiding. One independent, draggable window per timer.

Design principles follow stopwatch.py: config-driven, low dependency
(tkinter + pynput + stdlib winsound), a pynput listener that hands events to
the Tk loop through a queue, and perf_counter timing. Indicator *definitions*
(INDICATORS) are kept separate from their key *associations* (BINDINGS), and
rendering (Indicator) is separate from timing state (Timer).
"""

import queue
import threading
import time
import tkinter as tk
import winsound

from pynput import keyboard


# ======================================================================
# Configuration
# ======================================================================

# ---- Indicator definitions (appearance/behavior, no key association) ---
# shape:     "bar" | "arc"
# behavior:  "fill" (empty -> full) | "drain" (full -> empty)
# bar:       width, height, axis ("horizontal" | "vertical")
# arc:       size (square box); style "pie" (filled, default) | "ring"
#            thickness = border width (pie) or stroke width (ring)
# colors:    fg, bg, border ("" for none), flash_color
# sound:     True (default beep) | (freq_hz, ms) | "path.wav" | False
INDICATORS = {
    "bar_drain": {
        "shape": "bar", "behavior": "drain", "axis": "horizontal",
        "width": 128, "height": 16,
        "fg": "purple", "bg": "black", "border": "white",
        "flash_color": "yellow", "sound": True,
    },
    "ring_sweep": {
        "shape": "arc", "behavior": "drain", "style": "pie", "size": 48,
        "thickness": 1,
        "fg": "magenta", "bg": "black", "border": "white",
        "flash_color": "yellow", "sound": ( 880, 150 ),
    },
}

# ---- Key -> indicator associations -----------------------------------
# key: single char ("q") or named key ("f9", "space"); duration in seconds;
# x/y: initial top-left screen position.
BINDINGS = [
    { "key": "e", "indicator": "bar_drain",  "duration": 1.4, "x": 2032, "y": 1208 },
    { "key": "e", "indicator": "ring_sweep", "duration": 8.8, "x": 1944, "y": 1197 },
]

# ---- Global behavior -------------------------------------------------
UPDATE_INTERVAL_MS = 16          # ~20ms redraw cadence target
TRANSPARENCY       = 0.85        # 0.0 (invisible) - 1.0 (opaque)
RETRIGGER          = "restart"   # re-press during cooldown: "restart" | "ignore"
FLASH_DURATION_MS  = 600         # total expiration flash time
FLASH_INTERVAL_MS  = 100         # flash color toggle period
ARRANGE_KEY        = "f8"        # toggle show/hide all for dragging; "" disables
EXIT_KEY           = "f11"


# ======================================================================
# Inlined ANSI console colors (keeps this module dependency-free)
# ======================================================================

GREEN   = "\033[92m"
YELLOW  = "\033[93m"
MAGENTA = "\033[95m"
CYAN    = "\033[96m"
RED     = "\033[91m"
RESET   = "\033[0m"


# ======================================================================
# Helpers
# ======================================================================

def parse_key( spec ):
    """Resolve a config key string to a pynput key object.

    Accepts a named key ("f9", "space", "esc") or a single character.
    """
    named = getattr( keyboard.Key, spec, None )
    if named is not None:
        return named
    return keyboard.KeyCode.from_char( spec )


def key_label( spec ):
    """Human-readable label for a config key string."""
    return spec.upper()


def clamp( value, low, high ):
    return max( low, min( high, value ) )


def play_sound( spec ):
    """Fire a sound asynchronously so the redraw loop is never stalled."""
    if not spec:
        return

    def _play():
        try:
            if spec is True:
                winsound.Beep( 1000, 200 )
            elif isinstance( spec, ( tuple, list ) ):
                winsound.Beep( int( spec[0] ), int( spec[1] ) )
            elif isinstance( spec, str ):
                winsound.PlaySound(
                    spec, winsound.SND_FILENAME | winsound.SND_ASYNC
                )
        except Exception:
            pass

    threading.Thread( target=_play, daemon=True ).start()


# ======================================================================
# Indicator renderers (view only; know nothing about time)
# ======================================================================

class Indicator:
    """Draws a 0..1 fraction of a shape in a given color."""

    def __init__( self, cfg ):
        self.cfg = cfg

    def size( self ):
        """Return (width, height) of the shape's bounding box."""
        raise NotImplementedError

    def render( self, canvas, fraction, color ):
        """Draw `fraction` (0..1) of the shape filled with `color`."""
        raise NotImplementedError


class BarIndicator( Indicator ):

    def size( self ):
        return ( self.cfg["width"], self.cfg["height"] )

    def render( self, canvas, fraction, color ):
        cfg = self.cfg
        w, h = self.size()
        canvas.delete( "all" )
        canvas.create_rectangle(
            1, 1, w - 1, h - 1,
            outline=cfg.get( "border" ) or "", fill=cfg["bg"],
        )
        if fraction <= 0:
            return
        if cfg.get( "axis", "horizontal" ) == "horizontal":
            fw = int( ( w - 2 ) * fraction )
            canvas.create_rectangle( 1, 1, 1 + fw, h - 1, outline="", fill=color )
        else:
            fh = int( ( h - 2 ) * fraction )
            canvas.create_rectangle( 1, h - 1 - fh, w - 1, h - 1, outline="", fill=color )


class ArcIndicator( Indicator ):

    def size( self ):
        s = self.cfg["size"]
        return ( s, s )

    def render( self, canvas, fraction, color ):
        """Draw remaining/filled portion as a pie or ring.

        `fraction` is the *display* amount (0..1): for drain, Timer already
        passes remaining (1 -> 0), so we always draw that much of the shape.
        Sweep starts at 12 o'clock and runs clockwise.
        """
        cfg = self.cfg
        s = cfg["size"]
        canvas.delete( "all" )

        pad = 1
        box = ( pad, pad, s - pad, s - pad )
        border = cfg.get( "border" ) or ""
        thickness = max( 1, int( cfg.get( "thickness" ) or 1 ) )
        style = cfg.get( "style", "pie" )

        # Full circular silhouette so empty regions stay round (not a
        # vanishing pizza on a square window).
        canvas.create_oval(
            *box,
            fill=cfg["bg"],
            outline=border if border else cfg["bg"],
            width=thickness if border else 1,
        )

        if fraction <= 0:
            return

        # Tk treats extent == +/-360 as empty; stay just under a full turn.
        extent = -359.999 * fraction
        inset = thickness if border else 0
        inner = ( pad + inset, pad + inset, s - pad - inset, s - pad - inset )

        if style == "ring":
            # Hollow ring: stroke the remaining arc over the disc.
            canvas.create_arc(
                *inner, start=90, extent=extent, style=tk.ARC,
                outline=color, width=max( 2, thickness + 1 ),
            )
        else:
            # Filled pie: remaining yellow disc wedge over the bg circle.
            canvas.create_arc(
                *inner, start=90, extent=extent, style=tk.PIESLICE,
                outline=color, fill=color,
            )
            # Redraw border on top so the pie never covers it.
            if border:
                canvas.create_oval(
                    *box, fill="", outline=border, width=thickness,
                )


def make_indicator( cfg ):
    shape = cfg["shape"]
    if shape == "bar":
        return BarIndicator( cfg )
    if shape == "arc":
        return ArcIndicator( cfg )
    raise ValueError( f"Unknown indicator shape: {shape!r}" )


# ======================================================================
# Timer (controller/model): one window + state machine per binding
# ======================================================================

class Timer:

    def __init__( self, app, binding ):
        self.app = app
        self.key_spec = binding["key"]
        self.key = parse_key( binding["key"] )
        self.indicator_name = binding["indicator"]
        self.duration = float( binding["duration"] )
        self.x = binding["x"]
        self.y = binding["y"]

        self.cfg = INDICATORS[binding["indicator"]]
        self.indicator = make_indicator( self.cfg )
        w, h = self.indicator.size()

        self.win = tk.Toplevel( app.root )
        self.win.overrideredirect( True )
        self.win.attributes( '-topmost', True )
        self.win.attributes( '-alpha', TRANSPARENCY )
        self.win.configure( bg=self.cfg["bg"] )
        self.win.geometry( f"{w}x{h}+{self.x}+{self.y}" )

        self.canvas = tk.Canvas(
            self.win, width=w, height=h, bg=self.cfg["bg"],
            highlightthickness=0, bd=0,
        )
        self.canvas.pack()
        self.canvas.bind( '<Button-1>', self.start_drag )
        self.canvas.bind( '<B1-Motion>', self.drag )

        self.state = "IDLE"          # IDLE | RUNNING | EXPIRING
        self.trigger_time = 0.0
        self.expire_start = 0.0
        self.win.withdraw()

    # ---- Display helpers ------------------------------------------
    def displayed( self, fraction ):
        """Convert elapsed fraction into the shown fill fraction."""
        if self.cfg.get( "behavior", "drain" ) == "fill":
            return fraction
        return 1.0 - fraction

    def render_placeholder( self ):
        self.indicator.render( self.canvas, 0.5, self.cfg["fg"] )

    # ---- Dragging -------------------------------------------------
    def start_drag( self, event ):
        self._drag_x = event.x
        self._drag_y = event.y

    def drag( self, event ):
        x = self.win.winfo_x() + event.x - self._drag_x
        y = self.win.winfo_y() + event.y - self._drag_y
        self.win.geometry( f"+{x}+{y}" )
        self.x, self.y = x, y

    # ---- Lifecycle ------------------------------------------------
    def trigger( self, now ):
        if self.state == "RUNNING" and RETRIGGER == "ignore":
            return
        self.trigger_time = now
        self.state = "RUNNING"
        self.win.deiconify()
        self.win.lift()

    def update( self, now ):
        if self.state == "RUNNING":
            elapsed = now - self.trigger_time
            fraction = clamp( elapsed / self.duration, 0.0, 1.0 )
            self.indicator.render(
                self.canvas, self.displayed( fraction ), self.cfg["fg"]
            )
            if elapsed >= self.duration:
                self.state = "EXPIRING"
                self.expire_start = now
                play_sound( self.cfg.get( "sound" ) )

        elif self.state == "EXPIRING":
            t_ms = ( now - self.expire_start ) * 1000.0
            if t_ms >= FLASH_DURATION_MS:
                self.state = "IDLE"
                if self.app.arranging:
                    self.render_placeholder()
                else:
                    self.win.withdraw()
            else:
                on = int( t_ms // FLASH_INTERVAL_MS ) % 2 == 0
                color = self.cfg["flash_color"] if on else self.cfg["fg"]
                self.indicator.render( self.canvas, 1.0, color )


# ======================================================================
# Application
# ======================================================================

class CdShapeApp:

    def __init__( self ):
        self.root = tk.Tk()
        self.root.withdraw()
        self.action_queue = queue.Queue()
        self.arranging = False

        self.timers = [ Timer( self, b ) for b in BINDINGS ]

        self.k_arrange = parse_key( ARRANGE_KEY ) if ARRANGE_KEY else None
        self.k_exit = parse_key( EXIT_KEY )

        self.setup_global_hotkeys()
        self.update()
        self.print_instructions()

    # ---- Hotkeys (listener thread -> queue) -----------------------
    def setup_global_hotkeys( self ):
        def on_key_press( key ):
            if self.k_arrange is not None and key == self.k_arrange:
                self.action_queue.put( ( 'arrange', None ) )
                return
            if key == self.k_exit:
                self.action_queue.put( ( 'exit', None ) )
                return
            for i, timer in enumerate( self.timers ):
                if key == timer.key:
                    self.action_queue.put( ( 'trigger', i ) )

        self.listener = keyboard.Listener( on_press=on_key_press )
        self.listener.daemon = True
        self.listener.start()

    def drain_actions( self ):
        try:
            while True:
                event, arg = self.action_queue.get_nowait()
                if event == 'exit':
                    self.exit_app()
                    return False
                if event == 'arrange':
                    self.toggle_arrange()
                elif event == 'trigger':
                    self.timers[arg].trigger( time.perf_counter() )
        except queue.Empty:
            pass
        return True

    def toggle_arrange( self ):
        self.arranging = not self.arranging
        for timer in self.timers:
            if self.arranging:
                timer.win.deiconify()
                timer.win.lift()
                timer.render_placeholder()
            elif timer.state == "IDLE":
                timer.win.withdraw()
        state = "ON" if self.arranging else "OFF"
        print( f"{YELLOW}Arrange mode {state}{RESET}" )

    # ---- Main loop ------------------------------------------------
    def update( self ):
        if not self.drain_actions():
            return
        now = time.perf_counter()
        for timer in self.timers:
            timer.update( now )
        self.root.after( UPDATE_INTERVAL_MS, self.update )

    def exit_app( self ):
        print( f"{RED}cdshape exiting{RESET}" )
        print( f"{CYAN}Final positions:{RESET}" )
        for timer in self.timers:
            print(
                f"  {YELLOW}{key_label( timer.key_spec )}{RESET} "
                f"{timer.indicator_name} -> x={MAGENTA}{timer.x}{RESET}, "
                f"y={MAGENTA}{timer.y}{RESET}"
            )
        self.listener.stop()
        self.root.quit()
        self.root.destroy()

    def print_instructions( self ):
        print( f"{GREEN}cdshape initialized and ready!{RESET}" )
        print( f"{CYAN}Cooldown triggers:{RESET}" )
        for timer in self.timers:
            print(
                f"  {YELLOW}{key_label( timer.key_spec )}{RESET} - "
                f"{timer.indicator_name} ({MAGENTA}{timer.duration:g}s{RESET})"
            )
        if self.k_arrange is not None:
            print( f"  {YELLOW}{key_label( ARRANGE_KEY )}{RESET} - Toggle arrange mode (drag to reposition)" )
        print( f"  {YELLOW}{key_label( EXIT_KEY )}{RESET} - Exit" )

    def run( self ):
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            self.exit_app()


def main():
    print( f"{CYAN}Starting cooldown shape overlays...{RESET}" )
    CdShapeApp().run()


if __name__ == "__main__":
    main()
