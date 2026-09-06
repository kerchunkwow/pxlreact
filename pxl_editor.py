"""
pxl_editor.py - standalone DearPyGui editor for profile.json.

Run `python pxl_editor.py` (separate process; the core app is not required to be running). Edits
are held in memory until Save, which serializes to a temp file, validates it with the same
pxl_config.load_profile the core uses at startup, and atomically replaces profile.json only on
success - an invalid profile can never reach disk. Apply changes to a running core with the
Ctrl+R reload hotkey.

Tabs: Reactions, Actions, Rotations, Wincheck. Each rotation carries its own source key (the
former Remaps tier). "Pick" buttons capture the mouse position and pixel color after a short
countdown (move the cursor onto the target pixel during it).
"""

import json
import os
import threading
import time

import dearpygui.dearpygui as dpg

from pxl_config import ConfigError, load_profile, PROFILE_PATH
from pxl_lib import get_mouse_pos, get_pixel_color

REACTION_TYPES = ( "react_if_not_color", "react_if_color" )
PICK_DELAY = 3

GREEN_C = ( 120, 220, 120, 255 )
RED_C = ( 235, 110, 110, 255 )
GREY_C = ( 150, 150, 150, 255 )


def _load_raw( path = PROFILE_PATH ):
    with open( path, "r", encoding = "utf-8" ) as fh:
        return json.load( fh )


class ProfileEditor:

    def __init__( self, path = PROFILE_PATH ):
        self.path = path
        self.profile = _load_raw( path )
        self.sel_reaction = None
        self.sel_action = None
        self.sel_rotation = None

    # =========================================================== small helpers

    def _status( self, text, color = GREY_C ):
        dpg.set_value( 'ed_status', text )
        dpg.configure_item( 'ed_status', color = color )

    def _cb_set( self, sender, app_data, user_data ):
        """Generic field callback: write app_data into (container, key) from user_data."""
        container, key = user_data
        container[ key ] = app_data

    def _cb_set_color( self, sender, app_data, user_data ):
        """Color callback: read via get_value (0-255 floats) and store an int [r, g, b]."""
        container, key = user_data
        container[ key ] = [ int( round( v ) ) for v in dpg.get_value( sender )[ :3 ] ]

    def _pick( self, sender, app_data, user_data ):
        """
        Pixel picker: after a countdown, capture the mouse position and pixel color into config.
        `user_data` is ( container, x_key, y_key, color_key_or_None, ( x_tag, y_tag, color_tag ) ).
        """
        container, x_key, y_key, color_key, tags = user_data

        def _worker():
            for i in range( PICK_DELAY, 0, -1 ):
                self._status( f"picking in {i}... hover the target pixel" )
                time.sleep( 1 )
            x, y = get_mouse_pos()
            rgb = get_pixel_color( x, y )
            container[ x_key ] = x
            container[ y_key ] = y
            dpg.set_value( tags[ 0 ], x )
            dpg.set_value( tags[ 1 ], y )
            if color_key is not None and rgb is not None:
                container[ color_key ] = list( rgb )
                dpg.set_value( tags[ 2 ], ( *rgb, 255 ) )
            self._status( f"picked ({x}, {y}) = {rgb}", GREEN_C )

        threading.Thread( target = _worker, daemon = True ).start()

    def _unique_name( self, container, base ):
        if base not in container:
            return base
        n = 2
        while f"{base}_{n}" in container:
            n += 1
        return f"{base}_{n}"

    def _rename_key( self, container, old, new ):
        """Rename a dict key preserving order; returns False if the new name collides."""
        if new == old:
            return True
        if not new or new in container:
            self._status( f"name '{new}' is empty or already exists", RED_C )
            return False
        items = [ ( new, v ) if k == old else ( k, v ) for k, v in container.items() ]
        container.clear()
        container.update( items )
        return True

    # ============================================================ save / revert

    def _save( self ):
        # Prune empty ready lists so the cooldown shorthand validation applies
        for data in self.profile[ 'reactions' ].values():
            if 'ready' in data and not data[ 'ready' ]:
                del data[ 'ready' ]

        tmp = f"{self.path}.tmp"
        try:
            with open( tmp, "w", encoding = "utf-8" ) as fh:
                json.dump( self.profile, fh, indent = 2 )
            load_profile( tmp )
        except ConfigError as exc:
            self._status( f"NOT saved: {exc}", RED_C )
            try:
                os.remove( tmp )
            except OSError:
                pass
            return
        os.replace( tmp, self.path )
        self._status( f"saved {time.strftime( '%H:%M:%S' )} - Ctrl+R in pxlreact to apply", GREEN_C )

    def _revert( self ):
        self.profile = _load_raw( self.path )
        self.sel_reaction = None
        self.sel_action = None
        self.sel_rotation = None
        self._rebuild_all()
        self._status( "reverted to last saved profile.json" )

    # ================================================================ reactions

    def _reaction_names( self ):
        return list( self.profile[ 'reactions' ] )

    def _select_reaction( self, sender, app_data, user_data = None ):
        self.sel_reaction = app_data if isinstance( app_data, str ) else user_data
        self._build_reaction_form()

    def _add_reaction( self ):
        name = self._unique_name( self.profile[ 'reactions' ], "new_reaction" )
        self.profile[ 'reactions' ][ name ] = {
            "enabled": False, "x": 0, "y": 0, "type": "react_if_not_color",
            "color": [ 255, 0, 255 ], "tolerance": 4000, "confirm": 0.1, "cooldown": 1,
            "ignore_colors": [], "press": "1",
        }
        self.sel_reaction = name
        self._refresh_reaction_list()
        self._build_reaction_form()

    def _delete_reaction( self ):
        if self.sel_reaction and self.sel_reaction in self.profile[ 'reactions' ]:
            del self.profile[ 'reactions' ][ self.sel_reaction ]
            self.sel_reaction = None
            self._refresh_reaction_list()
            self._build_reaction_form()

    def _rename_reaction( self ):
        new = dpg.get_value( 'rx_name' ).strip()
        if self.sel_reaction and self._rename_key( self.profile[ 'reactions' ], self.sel_reaction, new ):
            self.sel_reaction = new
            self._refresh_reaction_list()

    def _refresh_reaction_list( self ):
        dpg.configure_item( 'rx_list', items = self._reaction_names() )
        if self.sel_reaction:
            dpg.set_value( 'rx_list', self.sel_reaction )

    def _build_reaction_form( self ):
        dpg.delete_item( 'rx_form', children_only = True )
        name = self.sel_reaction
        if not name or name not in self.profile[ 'reactions' ]:
            dpg.add_text( "select a reaction", parent = 'rx_form', color = GREY_C )
            return
        d = self.profile[ 'reactions' ][ name ]
        d.setdefault( 'ready', [] )
        d.setdefault( 'ignore_colors', [] )
        p = 'rx_form'

        with dpg.group( horizontal = True, parent = p ):
            dpg.add_input_text( default_value = name, tag = 'rx_name', width = 200 )
            dpg.add_button( label = "Rename", callback = lambda: self._rename_reaction() )
            dpg.add_checkbox( label = "enabled", default_value = d.get( 'enabled', True ),
                              callback = self._cb_set, user_data = ( d, 'enabled' ) )

        with dpg.group( horizontal = True, parent = p ):
            dpg.add_input_int( label = "x", default_value = d.get( 'x', 0 ), width = 110,
                               tag = 'rx_x', callback = self._cb_set, user_data = ( d, 'x' ) )
            dpg.add_input_int( label = "y", default_value = d.get( 'y', 0 ), width = 110,
                               tag = 'rx_y', callback = self._cb_set, user_data = ( d, 'y' ) )
            dpg.add_button( label = f"Pick ({PICK_DELAY}s)", callback = self._pick,
                            user_data = ( d, 'x', 'y', 'color', ( 'rx_x', 'rx_y', 'rx_color' ) ) )

        with dpg.group( horizontal = True, parent = p ):
            dpg.add_color_edit( default_value = ( *d.get( 'color', [ 0, 0, 0 ] ), 255 ),
                                label = "color", no_alpha = True, width = 200, tag = 'rx_color',
                                callback = self._cb_set_color, user_data = ( d, 'color' ) )
            dpg.add_input_int( label = "tolerance", default_value = d.get( 'tolerance', 4000 ),
                               width = 110, callback = self._cb_set, user_data = ( d, 'tolerance' ) )

        dpg.add_combo( list( REACTION_TYPES ), label = "type", default_value = d.get( 'type', REACTION_TYPES[ 0 ] ),
                       width = 200, parent = p, callback = self._cb_set, user_data = ( d, 'type' ) )

        dpg.add_input_text( label = "press", default_value = d.get( 'press', '' ), width = 80,
                            parent = p, callback = self._cb_set, user_data = ( d, 'press' ) )

        with dpg.group( horizontal = True, parent = p ):
            dpg.add_input_float( label = "confirm", default_value = d.get( 'confirm', 0.0 ), width = 110,
                                 format = "%.2f", callback = self._cb_set, user_data = ( d, 'confirm' ) )
            dpg.add_input_float( label = "cast_time", default_value = d.get( 'cast_time', 0.0 ), width = 110,
                                 format = "%.2f", callback = self._cb_set, user_data = ( d, 'cast_time' ) )
            dpg.add_input_float( label = "cooldown", default_value = d.get( 'cooldown' ) or 0.0, width = 110,
                                 format = "%.2f", callback = self._cb_set, user_data = ( d, 'cooldown' ) )

        dpg.add_text( "ready checks (ANDed with top-level cooldown; empty = cooldown only)",
                      parent = p, color = GREY_C )
        dpg.add_group( tag = 'rx_ready_rows', parent = p )
        with dpg.group( horizontal = True, parent = p ):
            dpg.add_button( label = "Add cooldown check",
                            callback = lambda: self._add_ready( { "type": "cooldown", "cooldown": 5 } ) )
            dpg.add_button( label = "Add color check",
                            callback = lambda: self._add_ready(
                                { "type": "color", "px": 0, "py": 0, "color": [ 255, 0, 255 ],
                                  "tolerance": 4000 } ) )
            dpg.add_button( label = "Add reaction check",
                            callback = lambda: self._add_reaction_ready() )
        self._build_ready_rows()

        dpg.add_text( "ignore colors (benign off-colors that must not trigger)", parent = p, color = GREY_C )
        dpg.add_group( tag = 'rx_ignore_rows', parent = p )
        dpg.add_button( label = "Add ignore color", parent = p,
                        callback = lambda: self._add_ignore() )
        self._build_ignore_rows()

    def _peer_reaction_names( self ):
        """Other reaction names available as peer ready-check targets (excludes the selected one)."""
        return [ n for n in self._reaction_names() if n != self.sel_reaction ]

    def _add_reaction_ready( self ):
        peers = self._peer_reaction_names()
        self._add_ready( {
            "type": "reaction",
            "reaction": peers[ 0 ] if peers else "",
            "settle_ratio": 0.15,
        } )

    def _add_ready( self, spec ):
        self.profile[ 'reactions' ][ self.sel_reaction ][ 'ready' ].append( spec )
        self._build_ready_rows()

    def _del_ready( self, sender, app_data, user_data ):
        specs = self.profile[ 'reactions' ][ self.sel_reaction ][ 'ready' ]
        specs.pop( user_data )
        self._build_ready_rows()

    def _build_ready_rows( self ):
        dpg.delete_item( 'rx_ready_rows', children_only = True )
        specs = self.profile[ 'reactions' ][ self.sel_reaction ][ 'ready' ]
        peers = self._peer_reaction_names()
        for i, spec in enumerate( specs ):
            with dpg.group( horizontal = True, parent = 'rx_ready_rows' ):
                dpg.add_button( label = "X", callback = self._del_ready, user_data = i )
                rtype = spec.get( 'type', 'cooldown' )
                if rtype == 'cooldown':
                    dpg.add_text( "cooldown", color = GREY_C )
                    dpg.add_input_float( default_value = spec.get( 'cooldown', 5 ), width = 100,
                                         format = "%.2f", callback = self._cb_set,
                                         user_data = ( spec, 'cooldown' ) )
                elif rtype == 'reaction':
                    dpg.add_text( "reaction", color = GREY_C )
                    peer = spec.get( 'reaction', '' )
                    items = list( peers )
                    if peer and peer not in items:
                        items.append( peer )
                    default_peer = peer if peer in items else ( items[ 0 ] if items else "" )
                    if default_peer and spec.get( 'reaction' ) != default_peer:
                        spec[ 'reaction' ] = default_peer
                    dpg.add_combo( items, default_value = default_peer, width = 140,
                                   callback = self._cb_set, user_data = ( spec, 'reaction' ) )
                    dpg.add_input_float( label = "settle", default_value = spec.get( 'settle_ratio', 0.15 ),
                                         width = 90, format = "%.2f", callback = self._cb_set,
                                         user_data = ( spec, 'settle_ratio' ) )
                    dpg.add_text( "of peer CD", color = GREY_C )
                else:
                    dpg.add_text( "color", color = GREY_C )
                    dpg.add_input_int( default_value = spec.get( 'px', 0 ), width = 90,
                                       tag = f'rx_rd_{i}_px', callback = self._cb_set,
                                       user_data = ( spec, 'px' ) )
                    dpg.add_input_int( default_value = spec.get( 'py', 0 ), width = 90,
                                       tag = f'rx_rd_{i}_py', callback = self._cb_set,
                                       user_data = ( spec, 'py' ) )
                    dpg.add_color_edit( default_value = ( *spec.get( 'color', [ 0, 0, 0 ] ), 255 ),
                                        no_alpha = True, no_inputs = True, tag = f'rx_rd_{i}_c',
                                        callback = self._cb_set_color, user_data = ( spec, 'color' ) )
                    dpg.add_input_int( label = "tol", default_value = spec.get( 'tolerance', 4000 ),
                                       width = 90, callback = self._cb_set,
                                       user_data = ( spec, 'tolerance' ) )
                    dpg.add_button( label = "Pick", callback = self._pick,
                                    user_data = ( spec, 'px', 'py', 'color',
                                                  ( f'rx_rd_{i}_px', f'rx_rd_{i}_py', f'rx_rd_{i}_c' ) ) )

    def _add_ignore( self ):
        self.profile[ 'reactions' ][ self.sel_reaction ][ 'ignore_colors' ].append( [ 255, 0, 255 ] )
        self._build_ignore_rows()

    def _del_ignore( self, sender, app_data, user_data ):
        self.profile[ 'reactions' ][ self.sel_reaction ][ 'ignore_colors' ].pop( user_data )
        self._build_ignore_rows()

    def _cb_set_ignore( self, sender, app_data, user_data ):
        colors, i = user_data
        colors[ i ] = [ int( round( v ) ) for v in dpg.get_value( sender )[ :3 ] ]

    def _build_ignore_rows( self ):
        dpg.delete_item( 'rx_ignore_rows', children_only = True )
        colors = self.profile[ 'reactions' ][ self.sel_reaction ][ 'ignore_colors' ]
        for i, color in enumerate( colors ):
            with dpg.group( horizontal = True, parent = 'rx_ignore_rows' ):
                dpg.add_button( label = "X", callback = self._del_ignore, user_data = i )
                dpg.add_color_edit( default_value = ( *color, 255 ), no_alpha = True,
                                    callback = self._cb_set_ignore, user_data = ( colors, i ) )

    # ================================================================== actions

    def _select_action( self, sender, app_data, user_data = None ):
        self.sel_action = app_data if isinstance( app_data, str ) else user_data
        self._build_action_form()

    def _add_action( self ):
        name = self._unique_name( self.profile[ 'actions' ], "new_action" )
        self.profile[ 'actions' ][ name ] = { "key": "1", "cooldown": 1, "cast_time": 0,
                                              "color_check": [] }
        self.sel_action = name
        self._refresh_action_list()
        self._build_action_form()

    def _delete_action( self ):
        name = self.sel_action
        if not name:
            return
        used_by = [ r for r, cfg in self.profile[ 'rotations' ].items()
                    if name in cfg.get( 'actions', [] ) ]
        if used_by:
            self._status( f"cannot delete '{name}': used by rotation(s) {used_by}", RED_C )
            return
        del self.profile[ 'actions' ][ name ]
        self.sel_action = None
        self._refresh_action_list()
        self._build_action_form()

    def _rename_action( self ):
        old = self.sel_action
        new = dpg.get_value( 'ac_name' ).strip()
        if old and self._rename_key( self.profile[ 'actions' ], old, new ):
            for cfg in self.profile[ 'rotations' ].values():
                seq = cfg.get( 'actions', [] )
                for i, aname in enumerate( seq ):
                    if aname == old:
                        seq[ i ] = new
            self.sel_action = new
            self._refresh_action_list()

    def _refresh_action_list( self ):
        dpg.configure_item( 'ac_list', items = list( self.profile[ 'actions' ] ) )
        if self.sel_action:
            dpg.set_value( 'ac_list', self.sel_action )

    def _build_action_form( self ):
        dpg.delete_item( 'ac_form', children_only = True )
        name = self.sel_action
        if not name or name not in self.profile[ 'actions' ]:
            dpg.add_text( "select an action", parent = 'ac_form', color = GREY_C )
            return
        d = self.profile[ 'actions' ][ name ]
        d.setdefault( 'color_check', [] )
        p = 'ac_form'

        with dpg.group( horizontal = True, parent = p ):
            dpg.add_input_text( default_value = name, tag = 'ac_name', width = 200 )
            dpg.add_button( label = "Rename", callback = lambda: self._rename_action() )

        with dpg.group( horizontal = True, parent = p ):
            dpg.add_input_text( label = "key (or left/right/middle)", default_value = d.get( 'key', '' ),
                                width = 80, callback = self._cb_set, user_data = ( d, 'key' ) )
        with dpg.group( horizontal = True, parent = p ):
            dpg.add_input_float( label = "cooldown", default_value = d.get( 'cooldown', 0.0 ), width = 110,
                                 format = "%.2f", callback = self._cb_set, user_data = ( d, 'cooldown' ) )
            dpg.add_input_float( label = "cast_time", default_value = d.get( 'cast_time', 0.0 ), width = 110,
                                 format = "%.2f", callback = self._cb_set, user_data = ( d, 'cast_time' ) )

        dpg.add_text( "color checks (all must pass; match off = pixel must NOT be this color)",
                      parent = p, color = GREY_C )
        dpg.add_group( tag = 'ac_cc_rows', parent = p )
        dpg.add_button( label = "Add color check", parent = p,
                        callback = lambda: self._add_cc() )
        self._build_cc_rows()

    def _add_cc( self ):
        self.profile[ 'actions' ][ self.sel_action ][ 'color_check' ].append(
            { "px": 0, "py": 0, "color": [ 255, 0, 255 ], "tolerance": 4000 } )
        self._build_cc_rows()

    def _del_cc( self, sender, app_data, user_data ):
        self.profile[ 'actions' ][ self.sel_action ][ 'color_check' ].pop( user_data )
        self._build_cc_rows()

    def _build_cc_rows( self ):
        dpg.delete_item( 'ac_cc_rows', children_only = True )
        checks = self.profile[ 'actions' ][ self.sel_action ][ 'color_check' ]
        for i, cc in enumerate( checks ):
            with dpg.group( horizontal = True, parent = 'ac_cc_rows' ):
                dpg.add_button( label = "X", callback = self._del_cc, user_data = i )
                dpg.add_input_int( default_value = cc.get( 'px', 0 ), width = 90,
                                   tag = f'ac_cc_{i}_px', callback = self._cb_set,
                                   user_data = ( cc, 'px' ) )
                dpg.add_input_int( default_value = cc.get( 'py', 0 ), width = 90,
                                   tag = f'ac_cc_{i}_py', callback = self._cb_set,
                                   user_data = ( cc, 'py' ) )
                dpg.add_color_edit( default_value = ( *cc.get( 'color', [ 0, 0, 0 ] ), 255 ),
                                    no_alpha = True, no_inputs = True, tag = f'ac_cc_{i}_c',
                                    callback = self._cb_set_color, user_data = ( cc, 'color' ) )
                dpg.add_input_int( label = "tol", default_value = cc.get( 'tolerance', 4000 ),
                                   width = 90, callback = self._cb_set, user_data = ( cc, 'tolerance' ) )
                dpg.add_checkbox( label = "match", default_value = cc.get( 'match', True ),
                                  callback = self._cb_set, user_data = ( cc, 'match' ) )
                dpg.add_button( label = "Pick", callback = self._pick,
                                user_data = ( cc, 'px', 'py', 'color',
                                              ( f'ac_cc_{i}_px', f'ac_cc_{i}_py', f'ac_cc_{i}_c' ) ) )

    # ================================================================ rotations

    def _select_rotation( self, sender, app_data, user_data = None ):
        self.sel_rotation = app_data if isinstance( app_data, str ) else user_data
        self._build_rotation_form()

    def _add_rotation( self ):
        name = self._unique_name( self.profile[ 'rotations' ], "new_rotation" )
        self.profile[ 'rotations' ][ name ] = { "key": "", "actions": [] }
        self.sel_rotation = name
        self._refresh_rotation_list()
        self._build_rotation_form()

    def _delete_rotation( self ):
        name = self.sel_rotation
        if not name:
            return
        del self.profile[ 'rotations' ][ name ]
        self.sel_rotation = None
        self._refresh_rotation_list()
        self._build_rotation_form()

    def _rename_rotation( self ):
        old = self.sel_rotation
        new = dpg.get_value( 'ro_name' ).strip()
        if old and self._rename_key( self.profile[ 'rotations' ], old, new ):
            self.sel_rotation = new
            self._refresh_rotation_list()

    def _refresh_rotation_list( self ):
        dpg.configure_item( 'ro_list', items = list( self.profile[ 'rotations' ] ) )
        if self.sel_rotation:
            dpg.set_value( 'ro_list', self.sel_rotation )

    def _seq_items( self ):
        seq = self.profile[ 'rotations' ][ self.sel_rotation ][ 'actions' ]
        return [ f"{i}: {name}" for i, name in enumerate( seq ) ]

    def _seq_index( self ):
        """Index of the selected sequence row, or None (rows are '<index>: <action>' strings)."""
        value = dpg.get_value( 'ro_seq' )
        if not value:
            return None
        return int( value.split( ':', 1 )[ 0 ] )

    def _seq_move( self, delta ):
        i = self._seq_index()
        seq = self.profile[ 'rotations' ][ self.sel_rotation ][ 'actions' ]
        if i is None or not ( 0 <= i + delta < len( seq ) ):
            return
        seq[ i ], seq[ i + delta ] = seq[ i + delta ], seq[ i ]
        dpg.configure_item( 'ro_seq', items = self._seq_items() )
        dpg.set_value( 'ro_seq', f"{i + delta}: {seq[ i + delta ]}" )

    def _seq_remove( self ):
        i = self._seq_index()
        seq = self.profile[ 'rotations' ][ self.sel_rotation ][ 'actions' ]
        if i is None or i >= len( seq ):
            return
        seq.pop( i )
        dpg.configure_item( 'ro_seq', items = self._seq_items() )

    def _seq_append( self ):
        action = dpg.get_value( 'ro_add_combo' )
        if action:
            self.profile[ 'rotations' ][ self.sel_rotation ][ 'actions' ].append( action )
            dpg.configure_item( 'ro_seq', items = self._seq_items() )

    def _build_rotation_form( self ):
        dpg.delete_item( 'ro_form', children_only = True )
        name = self.sel_rotation
        if not name or name not in self.profile[ 'rotations' ]:
            dpg.add_text( "select a rotation", parent = 'ro_form', color = GREY_C )
            return
        d = self.profile[ 'rotations' ][ name ]
        d.setdefault( 'key', '' )
        d.setdefault( 'actions', [] )
        p = 'ro_form'

        with dpg.group( horizontal = True, parent = p ):
            dpg.add_input_text( default_value = name, tag = 'ro_name', width = 200 )
            dpg.add_button( label = "Rename", callback = lambda: self._rename_rotation() )

        dpg.add_input_text( label = "key (physical key this rotation captures)",
                            default_value = d.get( 'key', '' ), width = 80, parent = p,
                            callback = self._cb_set, user_data = ( d, 'key' ) )

        dpg.add_text( "sequence (first ready action fires)", parent = p, color = GREY_C )
        dpg.add_listbox( self._seq_items(), tag = 'ro_seq', width = 300, num_items = 8, parent = p )
        with dpg.group( horizontal = True, parent = p ):
            dpg.add_button( label = "Up", callback = lambda: self._seq_move( -1 ) )
            dpg.add_button( label = "Down", callback = lambda: self._seq_move( 1 ) )
            dpg.add_button( label = "Remove", callback = lambda: self._seq_remove() )
        with dpg.group( horizontal = True, parent = p ):
            dpg.add_combo( list( self.profile[ 'actions' ] ), tag = 'ro_add_combo', width = 220 )
            dpg.add_button( label = "Add action", callback = lambda: self._seq_append() )

    # ================================================================= wincheck

    def _build_wincheck_tab( self ):
        dpg.delete_item( 'wc_rows', children_only = True )
        markers = self.profile[ 'wincheck' ][ 'markers' ]
        for i, m in enumerate( markers ):
            with dpg.group( horizontal = True, parent = 'wc_rows' ):
                dpg.add_button( label = "X", callback = self._del_marker, user_data = i )
                dpg.add_input_int( default_value = m.get( 'x', 0 ), width = 90, tag = f'wc_{i}_x',
                                   callback = self._cb_set, user_data = ( m, 'x' ) )
                dpg.add_input_int( default_value = m.get( 'y', 0 ), width = 90, tag = f'wc_{i}_y',
                                   callback = self._cb_set, user_data = ( m, 'y' ) )
                dpg.add_color_edit( default_value = ( *m.get( 'color', [ 0, 0, 0 ] ), 255 ),
                                    no_alpha = True, no_inputs = True, tag = f'wc_{i}_c',
                                    callback = self._cb_set_color, user_data = ( m, 'color' ) )
                dpg.add_input_int( label = "tol (0 = exact)", default_value = m.get( 'tolerance', 0 ),
                                   width = 90, callback = self._cb_set, user_data = ( m, 'tolerance' ) )
                dpg.add_button( label = "Pick", callback = self._pick,
                                user_data = ( m, 'x', 'y', 'color',
                                              ( f'wc_{i}_x', f'wc_{i}_y', f'wc_{i}_c' ) ) )

    def _del_marker( self, sender, app_data, user_data ):
        markers = self.profile[ 'wincheck' ][ 'markers' ]
        if len( markers ) <= 1:
            self._status( "wincheck needs at least one marker", RED_C )
            return
        markers.pop( user_data )
        self._build_wincheck_tab()

    def _add_marker( self ):
        self.profile[ 'wincheck' ][ 'markers' ].append(
            { "x": 0, "y": 0, "color": [ 255, 0, 255 ], "tolerance": 0 } )
        self._build_wincheck_tab()

    # ==================================================================== build

    def _rebuild_all( self ):
        self._refresh_reaction_list()
        self._build_reaction_form()
        self._refresh_action_list()
        self._build_action_form()
        self._refresh_rotation_list()
        self._build_rotation_form()
        dpg.set_value( 'wc_target', self.profile[ 'wincheck' ][ 'target_window' ] )
        self._build_wincheck_tab()

    def build( self ):
        with dpg.window( tag = 'ed_root' ):
            with dpg.group( horizontal = True ):
                dpg.add_button( label = "Save", callback = lambda: self._save() )
                dpg.add_button( label = "Revert", callback = lambda: self._revert() )
                dpg.add_text( '', tag = 'ed_status', color = GREY_C )

            with dpg.tab_bar():
                with dpg.tab( label = "Reactions" ):
                    with dpg.group( horizontal = True ):
                        with dpg.child_window( width = 220 ):
                            dpg.add_listbox( self._reaction_names(), tag = 'rx_list', width = -1,
                                             num_items = 14, callback = self._select_reaction )
                            with dpg.group( horizontal = True ):
                                dpg.add_button( label = "Add", callback = lambda: self._add_reaction() )
                                dpg.add_button( label = "Delete", callback = lambda: self._delete_reaction() )
                        with dpg.child_window():
                            dpg.add_group( tag = 'rx_form' )

                with dpg.tab( label = "Actions" ):
                    with dpg.group( horizontal = True ):
                        with dpg.child_window( width = 220 ):
                            dpg.add_listbox( list( self.profile[ 'actions' ] ), tag = 'ac_list',
                                             width = -1, num_items = 14, callback = self._select_action )
                            with dpg.group( horizontal = True ):
                                dpg.add_button( label = "Add", callback = lambda: self._add_action() )
                                dpg.add_button( label = "Delete", callback = lambda: self._delete_action() )
                        with dpg.child_window():
                            dpg.add_group( tag = 'ac_form' )

                with dpg.tab( label = "Rotations" ):
                    with dpg.group( horizontal = True ):
                        with dpg.child_window( width = 220 ):
                            dpg.add_listbox( list( self.profile[ 'rotations' ] ), tag = 'ro_list',
                                             width = -1, num_items = 14, callback = self._select_rotation )
                            with dpg.group( horizontal = True ):
                                dpg.add_button( label = "Add", callback = lambda: self._add_rotation() )
                                dpg.add_button( label = "Delete", callback = lambda: self._delete_rotation() )
                        with dpg.child_window():
                            dpg.add_group( tag = 'ro_form' )

                with dpg.tab( label = "Wincheck" ):
                    dpg.add_input_text( label = "target window title",
                                        default_value = self.profile[ 'wincheck' ][ 'target_window' ],
                                        tag = 'wc_target', width = 300, callback = self._cb_set,
                                        user_data = ( self.profile[ 'wincheck' ], 'target_window' ) )
                    dpg.add_text( "marker pixels (ALL must match for pxlreact to act)", color = GREY_C )
                    dpg.add_group( tag = 'wc_rows' )
                    dpg.add_button( label = "Add marker", callback = lambda: self._add_marker() )

        self._build_reaction_form()
        self._build_action_form()
        self._build_rotation_form()
        self._build_wincheck_tab()
        dpg.set_primary_window( 'ed_root', True )


def run( max_frames = None ):
    dpg.create_context()
    dpg.create_viewport( title = 'pxlreact profile editor', width = 980, height = 720 )
    editor = ProfileEditor()
    editor.build()
    dpg.setup_dearpygui()
    dpg.show_viewport()
    if max_frames is None:
        dpg.start_dearpygui()
    else:
        for _ in range( max_frames ):
            if not dpg.is_dearpygui_running():
                break
            dpg.render_dearpygui_frame()
    dpg.destroy_context()


if __name__ == "__main__":
    run()
