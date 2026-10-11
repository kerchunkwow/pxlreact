"""
PxlWinCheck is a single-purpose class designed to answer, on demand, whether the project should be
acting: the correct application is in the foreground and every marker pixel is its expected color.

This performs no background work and holds no cached flag; each call to check() reads the live
window title and marker pixels so the result reflects the screen at the instant of the call (e.g. a
key press or pixel reaction), avoiding stale-flag false triggers during state transitions such as
loading screens.
"""

from pathlib import Path
import ctypes

from pxl_lib import ColorCondition

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def foreground_title():
    """
    Title of the foreground window via ctypes (the only thing pywin32 was used for). The buffer is
    per-call because check() runs concurrently on the poll and remapper threads.
    """
    buf = ctypes.create_unicode_buffer( 256 )
    _user32.GetWindowTextW( _user32.GetForegroundWindow(), buf, 256 )
    return buf.value


_foreground_title = foreground_title


def foreground_exe():
    """Basename of the process that owns the foreground window, or '' if it cannot be read."""
    hwnd = _user32.GetForegroundWindow()
    pid = ctypes.c_ulong( 0 )
    _user32.GetWindowThreadProcessId( hwnd, ctypes.byref( pid ) )
    if not pid.value:
        return ""
    handle = _kernel32.OpenProcess( _PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value )
    if not handle:
        return ""
    try:
        size = ctypes.c_ulong( 32768 )
        buf = ctypes.create_unicode_buffer( 32768 )
        if not _kernel32.QueryFullProcessImageNameW( handle, 0, buf, ctypes.byref( size ) ):
            return ""
        return Path( buf.value ).name
    finally:
        _kernel32.CloseHandle( handle )


class PxlWinCheck:

    def __init__( self, config ):
        """
        Args:
            config (dict): normalized `wincheck` profile section: `target_window` plus a `markers`
                list of { x, y, color, tolerance }. Markers guard against inadvertent reactions, so
                their tolerance defaults to 0 (exact match) at load time; all must pass (AND).
        """
        self.update( config )

    def update( self, config ):
        """
        Apply a (new) wincheck config in place, so shared references held by the remapper and poll
        loop survive a profile reload.
        """
        self.target_app = config[ 'target_window' ]
        self.markers = [
            ColorCondition( m[ 'x' ], m[ 'y' ], m[ 'color' ], m[ 'tolerance' ] )
            for m in config[ 'markers' ]
        ]

    def marker_ok( self ):
        return all( marker.passes() for marker in self.markers )

    def in_target_app( self ):
        return self.target_app == _foreground_title()

    def check( self ):
        return self.in_target_app() and self.marker_ok()

    def check_slow( self ):
        """
        Debug version of check that will tell us why the app is inactive; use if this stops working,
        which sometimes happens after updates or with overlay apps that make subtle color changes.
        """
        in_app = self.in_target_app()
        marker_ok = self.marker_ok()
        if not in_app or not marker_ok:
            print( f"PxlWinCheck: app: {in_app} marker: {marker_ok}" )
        return in_app and marker_ok
