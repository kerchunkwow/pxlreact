"""
pxl_capture.py - debug-only screen-region capture for diagnosing reaction misfires.

ISOLATION / HOW TO UNPLUG:
This module is entirely optional and self-contained. It is imported only when `capture.enabled` is
true in settings.toml; with capture off, none of this code is touched and the core uses the plain,
capture-free `PxlReaction` via the default `build_reaction` factory - there is no per-trigger flag
checking anywhere in the core path.

To remove the feature permanently: delete this file, the capture block in
`PxlReactionRegistry.__init__`, and the [capture] section of settings.toml. No other changes needed.
"""
import glob
import os
import queue
import re
import shutil
import threading
import time
from collections import Counter

import mss
import mss.tools

from pxlreactHL import PxlReaction, build_readiness
from pxl_config import get_settings
from pxl_lib import describe_color, color_name, get_color_difference
from ansi import *


class SnapshotCapture:
    """
    Grabs a screen region on the calling (poll) thread - fast, in-memory - and offloads PNG encoding
    and disk writes to a worker thread, so the trigger path is barely delayed.
    """

    def __init__( self, out_dir = "captures", workers = 1 ):
        os.makedirs( out_dir, exist_ok = True )
        self.out_dir = out_dir

        # mss instances are single-thread; only the poll thread (which constructs this) calls grab()
        self._sct = mss.MSS()
        self._q = queue.Queue()
        self._stop = threading.Event()
        self._workers = [ threading.Thread( target = self._run, name = 'SnapshotSave', daemon = True )
                          for _ in range( workers ) ]
        for t in self._workers:
            t.start()

        print( f"📸 {GREEN}capture mode ON{RESET} -> {CYAN}{out_dir}{RESET}" )

    def capture( self, name, bbox, color = None ):
        """
        Grab `bbox` = (left, top, width, height) and queue it for saving. Must be called on the poll
        thread (the one that created this instance) and BEFORE the reaction key is sent, so the saved
        frame reflects the pre-trigger state. The grab is in-memory (~1-3 ms); encoding/writing the
        PNG happens on the worker thread.
        """
        mon = { "left": bbox[ 0 ], "top": bbox[ 1 ], "width": bbox[ 2 ], "height": bbox[ 3 ] }
        img = self._sct.grab( mon )
        cstr = "_{}-{}-{}".format( *color ) if color else ""
        stamp = time.strftime( "%Y%m%d_%H%M%S" ) + f"_{int( time.time() * 1000 ) % 1000:03d}"
        path = os.path.join( self.out_dir, f"{name}_{stamp}{cstr}.png" )
        self._q.put( ( bytes( img.rgb ), img.size, path ) )

    def _run( self ):
        while not self._stop.is_set():
            try:
                raw, size, path = self._q.get( timeout = 0.5 )
            except queue.Empty:
                continue
            try:
                mss.tools.to_png( raw, size, output = path )
            except Exception as exc:
                print( f"{RED}snapshot save failed: {exc}{RESET}" )
            finally:
                self._q.task_done()

    def stop( self ):
        self._stop.set()


class CapturingPxlReaction( PxlReaction ):
    """
    A PxlReaction that snapshots its monitored region immediately before firing. All trigger logic
    lives in the base class; this override adds only the pre-trigger grab, with no flag checks.
    """

    def __init__( self, *args, capture, bbox, **kwargs ):
        super().__init__( *args, **kwargs )
        self.capture = capture
        self.bbox = bbox

    def trigger( self ):
        # Grab the pre-trigger frame first, then run the normal (clean) trigger which sends the key
        self.capture.capture( self.name, self.bbox, self.pxl.rgb )
        super().trigger()


def make_capturing_factory( capture, width, height ):
    """
    Return a reaction factory matching the core's
    `build_reaction(pixel, data, name, trigger_log, cast_lock)` signature, producing
    CapturingPxlReaction instances whose capture bbox is centered on each reaction's monitored pixel.
    """
    def factory( pixel, data, name, trigger_log, cast_lock ):
        sx, sy = data[ 'sx' ], data[ 'sy' ]
        bbox = ( sx - width // 2, sy - height // 2, width, height )
        return CapturingPxlReaction(
            pxl = pixel,
            reaction_type = data[ 'type' ],
            reaction_color = data[ 'reaction_color' ],
            tolerance = data[ 'tolerance' ],
            reaction = data[ 'reaction' ],
            readiness = build_readiness( data, app = pixel.app ),
            confirm = data[ 'confirm' ],
            ignore_colors = data[ 'ignore_colors' ],
            name = name,
            trigger_log = trigger_log,
            cast_time = data[ 'cast_time' ],
            cast_lock = cast_lock,
            capture = capture,
            bbox = bbox,
        )
    return factory


# Trailing "r-g-b" token embedded in each capture filename by SnapshotCapture.capture()
_COLOR_RE = re.compile( r'(\d{1,3})-(\d{1,3})-(\d{1,3})$' )


def _parse_capture( path ):
    """Return ( reaction_name, rgb_tuple_or_None ) parsed from a capture filename."""
    stem = os.path.splitext( os.path.basename( path ) )[ 0 ]
    reaction = stem.split( '_', 1 )[ 0 ]
    m = _COLOR_RE.search( stem )
    rgb = tuple( int( g ) for g in m.groups() ) if m else None
    return reaction, rgb


def report_captures( out_dir = "captures", collapse_tolerance = None ):
    """
    Sort loose capture PNGs in `out_dir` into per-color subdirectories, then print a summary.
    `collapse_tolerance` defaults to the settings default color tolerance.

    The triggering color is read from each filename (the trailing `_r-g-b` token). Within each
    reaction, colors are clustered by `collapse_tolerance` so near-identical shades share one folder;
    the folder is named `<hue>_<r>-<g>-<b>` after the cluster's most common shade and nested under the
    reaction (e.g. `captures/CV1/purple_186-139-207/`). Folders with many images are the obvious
    ignore-worthy colors. Already-sorted images (those inside subfolders) are left untouched, so the
    routine is safe to re-run as new captures accumulate.
    """
    if collapse_tolerance is None:
        collapse_tolerance = get_settings()[ 'color' ][ 'default_tolerance' ]

    loose = sorted( glob.glob( os.path.join( out_dir, "*.png" ) ) )
    if not loose:
        print( f"{YELLOW}No unsorted captures in {CYAN}{out_dir}{RESET}.{RESET}" )
        return

    by_reaction = {}
    for path in loose:
        reaction, rgb = _parse_capture( path )
        if rgb is not None:
            by_reaction.setdefault( reaction, [] ).append( ( rgb, path ) )

    print( f"\n{B_CYAN}=== Sorting captures into color folders ({out_dir}) ==={RESET}" )
    for reaction in sorted( by_reaction ):
        # Greedy clustering by tolerance; each cluster tracks an anchor (for the distance test), a
        # Counter of exact shades (to pick a representative), and the source paths to move.
        clusters = []
        for rgb, path in by_reaction[ reaction ]:
            for cluster in clusters:
                if get_color_difference( rgb, cluster[ 'anchor' ] ) <= collapse_tolerance:
                    cluster[ 'counts' ][ rgb ] += 1
                    cluster[ 'paths' ].append( path )
                    break
            else:
                clusters.append( { 'anchor': rgb, 'counts': Counter( { rgb: 1 } ), 'paths': [ path ] } )

        clusters.sort( key = lambda c: len( c[ 'paths' ] ), reverse = True )

        for cluster in clusters:
            rep = cluster[ 'counts' ].most_common( 1 )[ 0 ][ 0 ]
            folder = f"{color_name( rep )}_{rep[ 0 ]}-{rep[ 1 ]}-{rep[ 2 ]}"
            dest = os.path.join( out_dir, reaction, folder )
            os.makedirs( dest, exist_ok = True )

            moved = 0
            for path in cluster[ 'paths' ]:
                target = os.path.join( dest, os.path.basename( path ) )
                if os.path.exists( target ):
                    continue
                try:
                    shutil.move( path, target )
                    moved += 1
                except OSError as exc:
                    print( f"{RED}move failed for {os.path.basename( path )}: {exc}{RESET}" )

            total = len( glob.glob( os.path.join( dest, "*.png" ) ) )
            shades = len( cluster[ 'counts' ] )
            note = f" {YELLOW}(+{shades - 1} near){RESET}" if shades > 1 else ""
            print( f"  {describe_color( rep )} -> {CYAN}{reaction}/{folder}{RESET}  "
                   f"{MAGENTA}{total}{RESET} file(s) ({GREEN}+{moved}{RESET}){note}" )


if __name__ == "__main__":
    import sys
    report_captures( sys.argv[ 1 ] if len( sys.argv ) > 1 else "captures" )
