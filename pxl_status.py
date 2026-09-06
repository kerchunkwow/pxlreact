"""
pxl_status.py provides the StatusHub: a thread-safe store the core publishes runtime state into,
consumed by the DPG status bar (see pxl_statusbar.py).

The hub is the single reporting funnel for gameplay state. Publishers call its record methods
instead of printing; gameplay events produce no terminal output (the status bar is the display).
The hub deliberately does NOT import dearpygui, so a headless run (statusbar disabled) carries
no GUI dependency.

Publishers / threads:
- main poll loop: set_active(), reaction fires (via PxlReactionRegistry._log_reaction)
- PxlRemapper thread: ability fires (record_ability), presses dropped during a cast (record_drop)
Consumers: the status bar render thread reads snapshots under the same lock.
"""

import threading
import time

# Seconds the status bar frame stays flashed after a press is dropped during a cast
FLASH_SECONDS = 0.6


class StatusHub:

    def __init__( self ):
        self._lock = threading.Lock()

        self.active = False

        # Set while the app is paused (F12); the status bar shows PAUSED in place of ACTIVE/INACTIVE
        self.paused = False

        # Armed by attach(); lets the status bar show live cast-lock state
        self.cast_lock = None

        # reaction name -> { 'delta', 'time', 'color' } (last firing)
        self.reactions = {}

        # Ordered reaction view for live CD display: [ ( reaction_name, PxlReaction ) ]
        self.reaction_view = []

        # Ordered rotation view for live display: [ ( rotation_name, Rotation ) ]
        self.rotation_view = []

        # Most recently fired ability: ( name, "HH:MM:SS" ) or None
        self.last_ability = None

        # monotonic deadline while the frame flash is active (press dropped during a cast)
        self.flash_until = 0.0

    # ------------------------------------------------------------------ wiring

    def attach( self, cast_lock ):
        """Give the hub the shared CastLock so consumers can show live cast state."""
        self.cast_lock = cast_lock

    def set_reactions( self, names ):
        """
        (Re)register the reaction last-fire rows from an iterable of enabled reaction names.
        Called at startup and again after a profile reload.
        """
        with self._lock:
            self.reactions = { name: { 'delta': '--', 'time': '', 'color': None }
                               for name in names }

    def set_reaction_view( self, reaction_view ):
        """
        (Re)register the reaction rows: `reaction_view` is [ ( reaction_name, PxlReaction ) ].
        PxlReaction objects are shared with the poll loop so cooldown/readiness state is read live.
        """
        with self._lock:
            self.reaction_view = list( reaction_view )

    def set_rotation_view( self, rotation_view ):
        """
        (Re)register the rotation rows: `rotation_view` is [ ( rotation_name, Rotation ) ].
        Rotation objects are shared with the remapper so cooldown state is read live.
        """
        with self._lock:
            self.rotation_view = list( rotation_view )

    # -------------------------------------------------------------- publishers

    def set_active( self, active ):
        self.active = active

    def set_paused( self, paused ):
        self.paused = paused

    def record_reaction( self, name, delta_text, rgb ):
        """One reaction firing."""
        now = time.strftime( "%H:%M:%S", time.localtime() )
        with self._lock:
            row = self.reactions.setdefault( name, { 'delta': '--', 'time': '', 'color': None } )
            row.update( delta = delta_text, time = now, color = rgb )

    def record_ability( self, name ):
        """A rotation selected and fired this ability; it becomes the headline bar content."""
        now = time.strftime( "%H:%M:%S", time.localtime() )
        with self._lock:
            self.last_ability = ( name, now )

    def record_drop( self ):
        """A press arrived during a cast and was dropped; flash the status bar frame."""
        with self._lock:
            self.flash_until = time.perf_counter() + FLASH_SECONDS

    # --------------------------------------------------------------- consumers

    def snapshot( self ):
        """
        Consistent copy of the displayable state for one render frame. Rotation and reaction objects
        are shared references (their cooldown fields are read without the hub lock; a torn read is
        harmless).
        """
        with self._lock:
            return {
                'active': self.active,
                'paused': self.paused,
                'casting': self.cast_lock.active() if self.cast_lock is not None else False,
                'reactions': { name: dict( row ) for name, row in self.reactions.items() },
                'reaction_view': list( self.reaction_view ),
                'rotation_view': list( self.rotation_view ),
                'last_ability': self.last_ability,
                'flash': time.perf_counter() < self.flash_until,
            }
