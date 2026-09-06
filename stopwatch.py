"""Standalone always-on-top overlay stopwatch.

A borderless, semi-transparent tkinter window that displays elapsed time as
MM:SS.mmm and is controlled entirely through global hotkeys (works even when
the window is not focused). Fully configurable via the globals below; no
external dependencies beyond `pynput` and the standard library.
"""

import queue
import time
import tkinter as tk

from pynput import keyboard


# ======================================================================
# Configuration
# ======================================================================

# ---- Keybinds -------------------------------------------------------
# Each key is a single character (e.g. "a") or a named key (e.g. "f9",
# "space", "esc"). Binding start/pause/resume to the same key yields a
# natural toggle: press cycles begin -> pause -> resume based on state.
KEY_START  = "f9"
KEY_PAUSE  = "f9"
KEY_RESUME = "f9"
KEY_RESET  = "f10"
KEY_EXIT   = "f11"

# ---- Hold-timer mode ------------------------------------------------
# When enabled, press-and-hold KEY_START to time: press starts, release
# pauses, then the display auto-resets after HOLD_RESET_DELAY_MS.
HOLD_TIMER_MODE     = False
HOLD_RESET_DELAY_MS = 1000   # 0 disables the post-release reset

# ---- Appearance -----------------------------------------------------
WINDOW_WIDTH  = 160
WINDOW_HEIGHT = 40
INITIAL_X     = 100
INITIAL_Y     = 100
BACKGROUND_COLOR   = "black"
FONT_FAMILY        = "Consolas"
FONT_SIZE          = 16
FONT_WEIGHT        = "bold"
TRANSPARENCY       = 0.8      # 0.0 (invisible) - 1.0 (opaque)
UPDATE_INTERVAL_MS = 10

# ---- Time -> color --------------------------------------------------
# DEFAULT_COLOR applies from 0; each TIME_COLORS entry takes effect once
# elapsed seconds >= its threshold (later thresholds win).
DEFAULT_COLOR = "lime"
TIME_COLORS = {
    1.0: "magenta",
    5.0: "yellow",
}

# ---- Auto-reset -----------------------------------------------------
AUTO_RESET_SECONDS = None     # None or 0 disables auto-reset
AUTO_RESET_LOOP    = False    # restart from zero after each auto-reset


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
# Key parsing / labels
# ======================================================================

def parse_key( spec ):
    """Resolve a config key string to a pynput key object.

    Accepts a named key ("f9", "space", "esc") or a single character
    ("a", "1"). Named keys resolve against pynput's `Key` enum.
    """
    named = getattr( keyboard.Key, spec, None )
    if named is not None:
        return named
    return keyboard.KeyCode.from_char( spec )


def key_label( spec ):
    """Human-readable label for a config key string (for instructions)."""
    return spec.upper()


def color_for( elapsed ):
    """Pick the display color for the current elapsed time."""
    color = DEFAULT_COLOR
    for threshold in sorted( TIME_COLORS ):
        if elapsed >= threshold:
            color = TIME_COLORS[threshold]
    return color


class Stopwatch:

    def __init__( self ):
        # Resolve configured keys once.
        self.k_start  = parse_key( KEY_START )
        self.k_pause  = parse_key( KEY_PAUSE )
        self.k_resume = parse_key( KEY_RESUME )
        self.k_reset  = parse_key( KEY_RESET )
        self.k_exit   = parse_key( KEY_EXIT )

        # Timing state.
        self.start_time = None
        self.elapsed_time = 0.0
        self.running = False

        # Thread-safe hand-off from the pynput listener to the Tk loop.
        self.action_queue = queue.Queue()

        # Window setup.
        self.root = tk.Tk()
        self.root.title( "Stopwatch" )
        self.root.geometry(
            f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}+{INITIAL_X}+{INITIAL_Y}"
        )
        self.root.configure( bg=BACKGROUND_COLOR )
        self.root.attributes( '-topmost', True )
        self.root.attributes( '-alpha', TRANSPARENCY )
        self.root.overrideredirect( True )

        self.time_label = tk.Label(
            self.root,
            text="00:00.000",
            font=( FONT_FAMILY, FONT_SIZE, FONT_WEIGHT ),
            fg=DEFAULT_COLOR,
            bg=BACKGROUND_COLOR,
        )
        self.time_label.pack( expand=True )

        # Draggable window.
        for widget in ( self.root, self.time_label ):
            widget.bind( '<Button-1>', self.start_drag )
            widget.bind( '<B1-Motion>', self.drag_window )

        self.setup_global_hotkeys()
        self.update_display()
        self.print_instructions()

    # ---- Window dragging ------------------------------------------
    def start_drag( self, event ):
        self.drag_start_x = event.x
        self.drag_start_y = event.y

    def drag_window( self, event ):
        x = self.root.winfo_x() + event.x - self.drag_start_x
        y = self.root.winfo_y() + event.y - self.drag_start_y
        self.root.geometry( f"+{x}+{y}" )

    # ---- Hotkeys (listener thread -> queue) -----------------------
    def setup_global_hotkeys( self ):
        def on_key_press( key ):
            if HOLD_TIMER_MODE:
                if key == self.k_start:
                    self.action_queue.put( ( 'start', ) )
                elif key == self.k_reset:
                    self.action_queue.put( ( 'reset', ) )
                elif key == self.k_exit:
                    self.action_queue.put( ( 'exit', ) )
                return

            # A key may be bound to several actions; queue every candidate
            # for this key as one group. The Tk loop fires the single action
            # valid for the current state (so one shared key toggles).
            candidates = []
            if key == self.k_exit:
                candidates.append( 'exit' )
            if key == self.k_reset:
                candidates.append( 'reset' )
            if key == self.k_pause:
                candidates.append( 'pause' )
            if key == self.k_resume:
                candidates.append( 'resume' )
            if key == self.k_start:
                candidates.append( 'start' )
            if candidates:
                self.action_queue.put( tuple( candidates ) )

        def on_key_release( key ):
            if HOLD_TIMER_MODE and key == self.k_start:
                self.action_queue.put( ( 'hold_release', ) )

        if HOLD_TIMER_MODE:
            self.listener = keyboard.Listener(
                on_press=on_key_press, on_release=on_key_release
            )
        else:
            self.listener = keyboard.Listener( on_press=on_key_press )
        self.listener.daemon = True
        self.listener.start()

    def drain_actions( self ):
        """Apply queued key-action groups on the Tk thread, state-gated."""
        try:
            while True:
                candidates = self.action_queue.get_nowait()
                self.dispatch_group( candidates )
        except queue.Empty:
            pass

    def dispatch_group( self, candidates ):
        """Fire the first candidate action valid for the current state."""
        for action in candidates:
            if self.try_action( action ):
                break

    def try_action( self, action ):
        """Attempt one action; return True if it fired."""
        if action == 'exit':
            self.exit_app()
            return True
        if action == 'reset':
            self.reset()
            return True
        if action == 'hold_release':
            self.pause()
            if HOLD_RESET_DELAY_MS > 0:
                self.root.after( HOLD_RESET_DELAY_MS, self.reset )
            return True
        if action == 'start':
            # Valid only from a fresh (zeroed, stopped) state.
            if not self.running and self.elapsed_time == 0.0:
                self.begin()
                return True
        elif action == 'resume':
            # Valid only when paused with accumulated time.
            if not self.running and self.elapsed_time > 0.0:
                self.begin()
                return True
        elif action == 'pause':
            if self.running:
                self.pause()
                return True
        return False

    # ---- State transitions ----------------------------------------
    def begin( self ):
        self.start_time = time.perf_counter() - self.elapsed_time
        self.running = True
        print( f"{GREEN}Stopwatch running{RESET}" )

    def pause( self ):
        if self.running:
            self.running = False
            print( f"{YELLOW}Stopwatch paused{RESET}" )

    def reset( self ):
        self.running = False
        self.elapsed_time = 0.0
        self.start_time = None
        self.time_label.config( text="00:00.000", fg=DEFAULT_COLOR )
        print( f"{CYAN}Stopwatch reset{RESET}" )

    def exit_app( self ):
        print( f"{RED}Stopwatch exiting{RESET}" )
        self.listener.stop()
        self.root.quit()
        self.root.destroy()

    # ---- Main display loop ----------------------------------------
    def update_display( self ):
        self.drain_actions()

        if self.running and self.start_time is not None:
            self.elapsed_time = time.perf_counter() - self.start_time

            if AUTO_RESET_SECONDS and self.elapsed_time >= AUTO_RESET_SECONDS:
                self.reset()
                if AUTO_RESET_LOOP:
                    self.begin()

        minutes = int( self.elapsed_time // 60 )
        seconds = self.elapsed_time % 60
        time_str = f"{minutes:02d}:{seconds:06.3f}"

        self.time_label.config(
            text=time_str, fg=color_for( self.elapsed_time )
        )

        self.root.after( UPDATE_INTERVAL_MS, self.update_display )

    def print_instructions( self ):
        print( f"{GREEN}Stopwatch initialized and ready!{RESET}" )
        print( f"{CYAN}Controls:{RESET}" )
        if HOLD_TIMER_MODE:
            print( f"  {YELLOW}{key_label( KEY_START )}{RESET} - Hold to time (press start, release stop)" )
            print( f"  {YELLOW}{key_label( KEY_RESET )}{RESET} - Reset" )
            print( f"  {YELLOW}{key_label( KEY_EXIT )}{RESET} - Exit" )
        else:
            print( f"  {YELLOW}{key_label( KEY_START )}{RESET} - Start" )
            print( f"  {YELLOW}{key_label( KEY_PAUSE )}{RESET} - Pause" )
            print( f"  {YELLOW}{key_label( KEY_RESUME )}{RESET} - Resume" )
            print( f"  {YELLOW}{key_label( KEY_RESET )}{RESET} - Reset" )
            print( f"  {YELLOW}{key_label( KEY_EXIT )}{RESET} - Exit" )

    def run( self ):
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            self.exit_app()


def main():
    print( f"{CYAN}Starting lightweight stopwatch overlay...{RESET}" )
    if HOLD_TIMER_MODE:
        print( f"{MAGENTA}Hold timer mode enabled{RESET}" )
    Stopwatch().run()


if __name__ == "__main__":
    main()
