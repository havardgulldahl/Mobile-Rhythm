#!/usr/bin/env python
# -*- encoding: utf8 -*-
#
# Mobile Rhythm - web interface to Rhythmbox for mobile devices
# Copyright (C) 2007 Michael Gratton.
# Copyright (C) 2010 Håvard Gulldahl - websocket stuff
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#

import cStringIO
import cgi
import fnmatch
import os
import sys
import time
import json
import hashlib
import asyncore
import logging

def getfqdn(name=''):
      return name
import socket
socket.getfqdn=getfqdn

import math
import BaseHTTPServer

import gtk
import gobject

import rb
import rhythmdb

from websocketserver import WebSocket, make_websocketserver

# try to load avahi, don't complain if it fails
try:
    import dbus
    import avahi
    use_mdns = True
except:
    use_mdns = False

SEARCH_MASK = ("Any", "Artist", "Title", "Album", "Genre")
SEARCH_PROPS = ("Any", rhythmdb.PROP_ARTIST, rhythmdb.PROP_TITLE,
                rhythmdb.PROP_ALBUM, rhythmdb.PROP_GENRE)
SEARCH_PROPS_ANY = (rhythmdb.PROP_ARTIST, rhythmdb.PROP_TITLE,
                    rhythmdb.PROP_ALBUM, rhythmdb.PROP_GENRE, rhythmdb.PROP_LOCATION)

class MobileRhythmPlugin(rb.Plugin):
    entrygroup = None
    server = None

    def __init__(self):
        super(MobileRhythmPlugin, self).__init__()

    def activate (self, shell):
        self.db = shell.props.db
        self.shell = shell
        self.player = shell.get_player()
        self.shell_cb_ids = (
            self.player.connect ('playing-song-changed',
                                 self._playing_entry_changed_cb),
            self.player.connect ('playing-source-changed',
                                 self._source_changed_cb),
            self.player.connect ('playing-changed',
                                 self._playing_changed_cb)
            )
        self.db_cb_ids = (
            self.db.connect ('entry-extra-metadata-notify',
                             self._extra_metadata_changed_cb)
            ,)
        self.port = 8005
        self.server = MobileRhythmServer('', self.port, self)
        self._mdns_publish()

    def deactivate(self, shell):
        self._mdns_withdraw()
        try: 
            self.server.shutdown()
        except:
            pass
        self.server = None
        try:
            self.websocket.running = False
        except: 
            pass
        self.websocket = None

        for id in self.shell_cb_ids:
            self.player.disconnect(id)

        for id in self.db_cb_ids:
            self.db.disconnect(id)

        self.player = None
        self.shell = None
        self.db = None

    def _mdns_publish(self):
        if use_mdns:
            bus = dbus.SystemBus()
            avahi_bus = bus.get_object(avahi.DBUS_NAME, avahi.DBUS_PATH_SERVER)
            avahi_svr = dbus.Interface(avahi_bus, avahi.DBUS_INTERFACE_SERVER)

            servicetype = '_http._tcp'
            servicename = 'Mobile Rhythmbox on %s' % (socket.gethostname())

            eg_path = avahi_svr.EntryGroupNew()
            eg_obj = bus.get_object(avahi.DBUS_NAME, eg_path)
            self.entrygroup = dbus.Interface(eg_obj,
                                             avahi.DBUS_INTERFACE_ENTRY_GROUP)
            self.entrygroup.AddService(avahi.IF_UNSPEC,
                                       avahi.PROTO_UNSPEC,
                                       0,
                                       servicename,
                                       servicetype,
                                       "",
                                       "",
                                       dbus.UInt16(self.port),
                                       ())
            self.entrygroup.Commit()

    def _mdns_withdraw(self):
        if use_mdns and self.entrygroup != None:
            self.entrygroup.Reset()
            self.entrygroup.Free()
            self.entrygroup = None

    def _playing_changed_cb(self, player, playing):
        if not self.server:
            return
        self.server.signal_playing_changed(player, playing)
        #self._update_entry(player.get_playing_entry())

    def _source_changed_cb(self, player, new_source):
        if not self.server:
            return
        logging.warning("source changed: %s", new_source)
        self.server.signal_source_changed(player, new_source)

    def _playing_entry_changed_cb(self, player, entry):
        if not self.server:
            return
        self.server.signal_entry_changed(player, entry)
        #self._update_entry(entry)

    def _extra_metadata_changed_cb(self, db, entry, field, metadata):
        if not self.server:
            return
        self.server.signal_metadata_changed(entry)
        #if entry == self.player.get_playing_entry():
        #    self._update_entry(entry)

class SimpleWebServer(BaseHTTPServer.BaseHTTPRequestHandler):
    server_version = "Mobile Rhythm Web Gui"
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(open(resolve_path("index.html")).read())

def make_webserver(hostname, port):
    httpd = BaseHTTPServer.HTTPServer((hostname, port), SimpleWebServer)
    httpd.allow_reuse_address = 1
    httpd.timeout = 2000
    logging.warning("simple webserver up at port #%i", port)
    return httpd

class MobileRhythmServer(object):
    _nowplaying = None

    def __init__(self, hostname, port, plugin):
        self.plugin = plugin
        self.running = True
        self._watchlist = []
        self._wsbuffer = cStringIO.StringIO()
        self._httpd = make_webserver(hostname, port)
        self._watchlist.append(gobject.io_add_watch(self._httpd.socket,
                                                    gobject.IO_IN,
                                                    self._idle_httpd_cb))
        if not hostname:
            hostname = "0.0.0.0"
        self.websocket = make_websocketserver(hostname, port+1)
        self._watchlist.append(gobject.io_add_watch(self.websocket, gobject.IO_IN, self.websocklistener))
        self._websockets = {}
        self._playlists = {}

    def websocklistener(self, sock, *args):
        '''Asynchronous connection listener. Starts a handler for each connection.'''
        logging.warning("Got incoming")
        conn, addr = sock.accept()
        logging.warning("Connected to %s" , addr)
        L = WebSocket(conn, sock.getsockname())
        logging.warning("Created new websockclient: %s", L)
        self.register_client(L)
        self._watchlist.append(gobject.io_add_watch(conn, gobject.IO_IN, L.readsock))
        return True

    def register_client(self, client):
        logging.warning("new client! %s" , client)
        client.onmessage = self._wsmessage
        client.writable = self._ws_check_pending
        client.handle_write = self._ws_push
        client.handle_error = self._ws_error
        self._websockets[client.addr] = client

    def shutdown(self):
        for z in self._watchlist:
            gobject.source_remove(z)
        self.running = False
        self.plugin = None

    def get_now_playing(self):
        entry = self.plugin.player.get_playing_entry()
        if not entry:
            self._nowplaying = None
            return None
        else:
            e = {"stream":None}
            lookup = {"artist": rhythmdb.PROP_ARTIST,
                      "album":rhythmdb.PROP_ALBUM,
                      "title":rhythmdb.PROP_TITLE,
                      "duration":rhythmdb.PROP_DURATION,
                      "eid":rhythmdb.PROP_ENTRY_ID,
                      "rating":rhythmdb.PROP_RATING
            }
            for z, i in lookup.items():
                e[z] = self.plugin.db.entry_get(entry, i)
            stream_title = \
                self.plugin.db.entry_request_extra_metadata(entry,
                                                     'rb:stream-song-title')
            if stream_title:
                e["stream"] = e["title"]
                e["title"] = stream_title
                if not e["artist"]:
                    e["artist"] = self.plugin.db.\
                        entry_request_extra_metadata(entry,
                                                     'rb:stream-song-artist')
                if not e["album"]:
                    e["album"] = self.plugin.db.\
                            entry_request_extra_metadata(entry,
                                                         'rb:stream-song-album')
            self._nowplaying = e
            return e

    def signal_playing_changed(self, player, playing):
        self._ws_update(playing=playing)

    def signal_entry_changed(self, player, entry):
        self._ws_update()

    def signal_metadata_changed(self, entry):
        self._ws_update()

    def signal_source_changed(self, player, new_source):
        pass

    def _send(self, data, receiver=None):
        d = json.dumps(data)
        if receiver is None: # send to all
            for z in self._websockets.values():
                try:
                    z.send(d)
                except socket.error, e:
                    logging.warning("socket %s won't take our data", z)
        else:
            self._websockets[receiver].send(d)
        #self._wsbuffer.write(json.dumps(data))

    def _ws_check_pending(self):
        i =  len(self._wsbuffer.getvalue()) 
        logging.warning("Check pending: %i" % i)
        return i > 0

    def _ws_push(self):
        logging.warning("pushing buffer")
        buf = self._wsbuffer.getvalue()
        self._wsbuffer.seek(0)
        self._wsbuffer.truncate()
        self.websocket.send(buf)

    def _ws_error(self, errtype=None, value=None, traceback=None):
        logging.warning("WebSocket Exception: %s %s", errtype, value)
        logging.exception(traceback)

    def _ws_update(self, playing=None):
        logging.warning("sending update, playing=%s", playing)
        action = playing and "playing" or "paused"
        args = self.get_now_playing()
        if args is not None:
            if action is None: action = "playing"
            #args["playingtime"] = self.plugin.player.get_playing_time()
        else:
            if action is None: action = "stopped"
        self._send({"domain": "player",
                    "action": action,
                    "args": args})

    def _open(self, filename):
        filename = os.path.join(os.path.dirname(__file__), filename)
        return open(filename)

    def _idle_httpd_cb(self, source, cb_condition):
        if not self.running:
            return False
        self._httpd.handle_request()
        return True

    def _wsmessage(self, data):
        logging.warning("Got data: %s" , repr(data))
        try:
            message = json.loads(data)
        except Exception, (e):
            logging.exception(e)
            return False
        #available methods
        # 
# 'add_play_order',
# 'chain',
# 'connect',
# 'connect_after',
# 'connect_object',
# 'connect_object_after',
# 'construct_child',
# 'disconnect',
# 'disconnect_by_func',
# 'do_add_child',
# 'do_construct_child',
# 'do_get_internal_child',
# 'do_next',
# 'do_parser_finished',
# 'do_previous',
# 'do_set_name',
# 'emit',
# 'emit_stop_by_name',
# 'freeze_notify',
# 'get_active_source',
# 'get_data',
# 'get_internal_child',
# 'get_mute',
# 'get_name',
# 'get_orientation',
# 'get_playback_state',
# 'get_playing',
# 'get_playing_entry',
# 'get_playing_path',
# 'get_playing_song_duration',
# 'get_playing_source',
# 'get_playing_time',
# 'get_playing_time_string',
# 'get_properties',
# 'get_property',
# 'get_volume',
# 'handler_block',
# 'handler_block_by_func',
# 'handler_disconnect',
# 'handler_is_connected',
# 'handler_unblock',
# 'handler_unblock_by_func',
# 'jump_to_current',
# 'notify',
# 'parser_finished',
# 'pause',
# 'play',
# 'play_entry',
# 'playpause',
# 'props',
# 'ref_accessible',
# 'remove_play_order',
# 'seek',
# 'set_data',
# 'set_mute',
# 'set_name',
# 'set_orientation',
# 'set_playback_state',
# 'set_playing_source',
# 'set_playing_time',
# 'set_properties',
# 'set_property',
# 'set_selected_source',
# 'set_volume',
# 'set_volume_relative',
# 'stop',
# 'stop_emission',
# 'thaw_notify',
# 'toggle_mute',
# 'weak_ref']
   #def ctrl_rate(self, rating):
        
        #if self.__item_entry is not None:
            #db = self.__shell.props.db
            #try:
                #db.set(self.__item_entry, rhythmdb.PROP_RATING, rating)
            #except gobject.GError, e:
                #log.debug("rating failed: %s" % str(e))
    #
    #def ctrl_toggle_playing(self):
        #
        #sp = self.__shell.get_player()
        #
        #try:
            #sp.playpause()
        #except gobject.GError, e:
            #log.debug("toggle play pause failed: %s" % str(e))
                #
    #def ctrl_toggle_repeat(self):
        #
        #sp = self.__shell.get_player()
        #
        #now = sp.props.play_order
        #
        #next = PLAYERORDER_TOGGLE_MAP_REPEAT.get(now, now)
            #
        #self.__gconf.set_string("/apps/rhythmbox/state/play_order", next)
    #
        ## update state within a short time (don't wait for scheduled poll)
        #gobject.idle_add(self.poll)
        #
    #def ctrl_toggle_shuffle(self):
        
        #sp = self.__shell.get_player()
        
        #now = sp.props.play_order
        ##
        #next = PLAYERORDER_TOGGLE_MAP_SHUFFLE.get(now, now)
        #
        #self.__gconf.set_string("/apps/rhythmbox/state/play_order", next)
        if message["action"] == 'play':		
            player = self.plugin.player
            if not player.get_playing():
                if not player.get_playing_source():
                    return self._play_entry(message["args"])
                else:
                    return self._play()
            else:
                return self._pause()
        elif message["action"] == 'playpause':
            self.plugin.player.playpause()
        elif message["action"] == 'pause':
            self.plugin.player.pause()
        elif message["action"] == 'play-entry':
            return self._play_entry(message["args"])
        elif message["action"] == 'next':
            self.plugin.player.do_next()
        elif message["action"] == 'previous':
            self.plugin.player.do_previous()
        elif message["action"] == 'stop':
            self.plugin.player.stop()
        elif message["action"] == 'get-state':
            return self._getstate()
        elif message["action"] == 'get-nowplaying':
            return self._getnowplaying()
        elif message["action"] == 'set-mute':
            return self._setmute(message["args"])
        elif message["action"] == 'get-mute':
            return self._getmute()
        elif message["action"] == 'set-vol':
            return self._setvolume(message["args"])
        elif message["action"] == 'get-vol':
            return self._getvolume()
        elif message["action"] == 'vol-up':
            self.plugin.player.set_volume(player.get_volume() + 0.1)
        elif message["action"] == 'vol-down':
            self.plugin.player.set_volume(player.get_volume() - 0.1)
        elif message["action"] == 'search-library':
            return self._search_library(**message["args"])
        elif message["action"] == 'get-playingtime':
            return self._getplayingtime()
        elif message["action"] == 'set-playingtime':
            return self._setplayingtime(message["args"])
        elif message["action"] == 'get-queue':
            return self._get_next_entries()
        elif message["action"] == 'get-playlists':
            return self._get_playlists()
        elif message["action"] == 'set-playlist':
            return self._set_playlist(message["args"])
        else:
            logging.warning("Uknown action: %s", message["action"])

    def _search_library(self, proptype, query=None,  offset=0, limit=None, fuzzy=True):
        logging.warning("query_library:%s %s %s %s", proptype, query, offset, limit)
        db = self.plugin.db
        #artist = '2pac'
        #qresult = (rhythmdb.QUERY_PROP_EQUALS, rhythmdb.PROP_ARTIST_FOLDED,artist.encode('utf-8'))

        qp = fuzzy and rhythmdb.QUERY_PROP_LIKE or rhythmdb.QUERY_PROP_EQUALS

        if proptype == "album":
            libquery = (qp, rhythmdb.PROP_ALBUM, query and query.encode('utf-8') or "unknown")
        elif proptype == "artist":
            libquery = (qp, rhythmdb.PROP_ARTIST_FOLDED, query and query.encode('utf-8') or "unknown")
        elif proptype == "song":
            libquery = (qp, rhythmdb.PROP_TITLE_FOLDED, query and query.encode('utf-8') or "unknown")
        elif proptype == "test":
            libquery = (rhythmdb.QUERY_PROP_EQUALS, rhythmdb.PROP_ARTIST, db.entry_type_get_by_name("song"))
        else:
            logging.error("Unknown proptype: %s", proptype)
            return

        playlist_rows = self._player_search(libquery)
        if playlist_rows.get_size() == 0:
            return self._send({"domain":"library",
                                "action": proptype,
                                "args" :{"query":query, "offset":offset, "limit":limit, "result":[]}})
        playlist = []
        for row in playlist_rows:
            entry = row[0]
            item = self.get_simple_object_from_entry(entry)
            playlist.append(item)
        self._send({"domain":"library",
                    "action":proptype,
                    "args" :{"query":query, "offset":offset, "limit":limit, "result":playlist}})
                    

    def _player_search(self, search):
        #"""perform a player search"""
        db = self.plugin.db
        query = db.query_new()
        db.query_append(query, search)
        query_model = db.query_model_new_empty()
        db.do_full_query_parsed(query_model, query)
        return query_model;

    def _get_simple_object_from_entry(self, entry):
        db = self.plugin.db
        item = {"track_number":db.entry_get(entry, rhythmdb.PROP_TRACK_NUMBER),
                "title":db.entry_get(entry, rhythmdb.PROP_TITLE),
                "eid":db.entry_get(entry, rhythmdb.PROP_ENTRY_ID),
                "title":db.entry_get(entry, rhythmdb.PROP_TITLE),
                "artist":db.entry_get(entry, rhythmdb.PROP_ARTIST),
                "album":db.entry_get(entry, rhythmdb.PROP_ALBUM),
                "duration":db.entry_get(entry, rhythmdb.PROP_DURATION),
                "rating":db.entry_get(entry, rhythmdb.PROP_RATING),
                "genre":db.entry_get(entry, rhythmdb.PROP_GENRE),
        }
        return item

    def _get_next_entries(self, entry=None, cnt=5):
        """Gets the next entries to be played from both active source and queue
        
        Uses each source's query-model.
        entry = entry to start from (as a kind of offset)
        cnt = number of entries to return
        """

        player = self.plugin.player
        if entry is None:
            try:
                entry = player.get_playing_entry()
            except:
                pass
        if not entry:
            self._send({"domain":"player", "action":"playqueue", "args":[]})
            return

        entries = [entry]
        
        queue = player.get_property("queue-source")
        if queue:
            querymodel = queue.get_property("query-model")
            l = querymodel.get_next_from_entry(entry)
            while l and len(entries) <= cnt:
                entries.append(l)
                l = querymodel.get_next_from_entry(l)
        source = player.get_property("source")
        if source:
            querymodel = source.get_property("query-model")
            l = querymodel.get_next_from_entry(entry)
            while l and len(entries) <= cnt:
                entries.append(l)
                l = querymodel.get_next_from_entry(l)

        #return entries
        simple_entries = [self._get_simple_object_from_entry(e) for e in entries]
        self._send({"domain":"player", "action":"playqueue", "args":simple_entries})

    def _get_playlists(self):
        player = self.plugin.player
        playlists = []
        playlist_model_entries = [x for x in 
            list(self.plugin.shell.props.sourcelist.props.model)
            if list(x)[2] == "Playlists"]
        if playlist_model_entries:
            playlist_iter = playlist_model_entries[0].iterchildren()
            i = 0
            for playlist_item in playlist_iter:
                logging.warning("got playlist iten: %s", playlist_item[1])
                plid = hashlib.md5(playlist_item[2]).hexdigest()
                playlists.append({"name":playlist_item[2], "plid":plid})
                #print "Playlist image: %s, name: %s, source: %s" % (playlist_item[1], playlist_item[2],
                #playlist_item[3])
                self._playlists[plid] = playlist_item[3]
                i += 1
        self._send({"domain":"playlist", "action":"get", "args":playlists})

    def _set_playlist(self, plid):
        player = self.plugin.player
        shell = self.plugin.shell
        player.stop()
        shell.props.sourcelist.select(self._playlists.get(plid, None))
        logging.warning("chose new source/playlist : %s", plid)
# start playing from the beginning
        player.play()
# or, if you've got a specific RhythmDBEntry that you want to play
        #player.play_entry(entry)

    def _getvolume(self):
        player = self.plugin.player
        self._send({"domain":"player", "action": "current_volume", "args":player.get_volume()})

    def _setvolume(self, value):
        player = self.plugin.player
        player.set_volume(float(value))
        return self._getvolume()

    def _getmute(self):
        self._send({"domain":"player",
                    "action":"mute_state",
                    "args" : self.plugin.player.get_mute()})

    def _setmute(self, state):
        self.plugin.player.set_mute(state)
        self._getmute()

    def _getstate(self):
        player = self.plugin.player
        eid = self._nowplaying and self._nowplaying["eid"] or None
        playing = player.get_playing()
        try:
            playingtime = player.get_playing_time()
        except:
            playingtime = None

        if playing: pstate = "playing"
        elif playingtime is None: pstate = "stopped"
        else: pstate = "paused"

        self._send({"domain":"player", 
                    "action": "state", 
                    "args": { "volume": player.get_volume(),
                              "playing_state": pstate,
                              "mute_state": player.get_mute(),
                              "playingtime": playingtime,
                              "eid": eid }}) # TODO: get eid from player. objecT

    def _getnowplaying(self):
        self._send({"domain":"player", "action": "nowplaying", "args":self._nowplaying})

    def _pause(self):
        player = self.plugin.player
        player.pause()
        self._send({"domain":"player", "action": "pause", "args":True})

    def _play(self):
        player = self.plugin.player
        player.play()

    def _play_entry(self, args):
        player = self.plugin.player
        shell = self.plugin.shell
        db = self.plugin.db
        pentry = db.entry_lookup_by_id(int(args))
        sys.stdout.write('location value received : %s' %params['location'][0])
        sys.stdout.write('entry title: %s' % db.entry_get(pentry, rhythmdb.PROP_ARTIST))
        player.play_entry(pentry)

    def _setplayingtime(self, position):
        player = self.plugin.player
        player.set_playing_time(int(position))
        return self._getplayingtime()

    def _getplayingtime(self):
        player = self.plugin.player
        try:
            self._send({"domain":"player", "action":"playingtime", "args":player.get_playing_time()})
        except Exception, e:
            logging.error("Could not get playing time")
            logging.exception(e)

    def _get_cover_art(self, entry=None):
        if entry is None:
            try:
                entry = self.plugin.player.get_playing_entry()
            except:
                pass
        if entry:
            db = self.plugin.db
            cover_art = db.entry_request_extra_metadata(entry, "rb:coverArt")
            logging.warning("Got cover art: %s", cover_art)
            self._send({"domain":"metadata", 
                        "action":"coverart", 
                        "args":{"eid":db.entry_get(entry, rhythmdb.PROP_ENTRY_ID),
                                "art":cover_art}})
                                               

    def _handle_stock(self, environ, response):
        path = environ['PATH_INFO']
        stock_id = path[len('/stock/'):]

        icons = gtk.icon_theme_get_default()
        iconinfo = icons.lookup_icon(stock_id, 24, ())
        if not iconinfo:
            iconinfo = icons.lookup_icon(stock_id, 32, ())
        if not iconinfo:
            iconinfo = icons.lookup_icon(stock_id, 48, ())
        if not iconinfo:
            iconinfo = icons.lookup_icon(stock_id, 16, ())

        if iconinfo:
            filename = iconinfo.get_filename()
            icon = open(filename)
            lastmod = time.gmtime(os.path.getmtime(filename))
            lastmod = time.strftime("%a, %d %b %Y %H:%M:%S +0000", lastmod)
            response_headers = [('Content-type','image/png'),
                                ('Last-Modified', lastmod)]
            response('200 OK', response_headers)
            return icon
        else:
            response_headers = [('Content-type','text/plain')]
            response('404 Not Found', response_headers)
            return 'Stock not found: %s' % stock_id

    def _handle_static(self, environ, response):
        rpath = environ['PATH_INFO']

        path = rpath.replace('/', os.sep)
        path = os.path.normpath(path)
        if path[0] == os.sep:
            path = path[1:]

        path = resolve_path(path)

        # this seems to cause a segfault
        #f = self.plugin.find_file(path)
        #print str(f)

        if os.path.isfile(path):
            lastmod = time.gmtime(os.path.getmtime(path))
            lastmod = time.strftime("%a, %d %b %Y %H:%M:%S +0000", lastmod)
		
	    content_type = 'text/css'
	    if fnmatch.fnmatch(path, '*.js'):
		content_type = 'text/javascript'
	    elif fnmatch.fnmatch(path, '*.xml'):
                content_type = 'text/xml'
	    elif fnmatch.fnmatch(path, '*.png'):
                content_type = 'image/png'
	    elif fnmatch.fnmatch(path, '*.ico'):
                content_type = 'image/ico'
	    elif fnmatch.fnmatch(path, '*.html'):
                content_type = 'text/html'

            response_headers = [('Content-type',content_type),
                                ('Last-Modified', lastmod)]
            response('200 OK', response_headers)
            return open(path)
        else:
            response_headers = [('Content-type','text/plain')]
            response('404 Not Found', response_headers)
            return 'File not found: %s' % rpath


def return_redirect(path, environ, response):
    if not path.startswith('/'):
        path_prefix = environ['REQUEST_URI']
        if path_prefix.endswith('/'):
            path = path_prefix + path
        else:
            path = path_prefix.rsplit('/', 1)[0] + path
    scheme = environ['wsgi.url_scheme']
    if 'HTTP_HOST' in environ:
        authority = environ['HTTP_HOST']
    else:
        authority = environ['SERVER_NAME']
    port = environ['SERVER_PORT']
    if ((scheme == 'http' and port != '80') or
        (scheme == 'https' and port != '443')):
        authority = '%s:%s' % (authority, port)
    location = '%s://%s%s' % (scheme, authority, path)
    status = '303 See Other'
    response_headers = [('Content-Type', 'text/plain'),
                        ('Location', location)]
    response(status, response_headers)
    return [ 'Redirecting...' ]

def resolve_path(path):
    return os.path.join(os.path.dirname(__file__), path)


