#!/usr/bin/env python
# -*- encoding: utf8 -*-
#
# Mobile Rhythm - web interface to Rhythmbox for mobile devices
# Copyright (C) 2007 Michael Gratton.
# Copyright (C) 2010 Håvard Gulldahl
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

from websocketserver import WebSocket, WebSocketServer, process_websocket

# try to load avahi, don't complain if it fails
try:
    import dbus
    import avahi
    use_mdns = True
except:
    use_mdns = False


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
        self._update_entry(player.get_playing_entry())

    def _playing_entry_changed_cb(self, player, entry):
        self._update_entry(entry)

    def _extra_metadata_changed_cb(self, db, entry, field, metadata):
        if entry == self.player.get_playing_entry():
            self._update_entry(entry)

    def _update_entry(self, entry):
        if not self.server:
            return
        if entry:
            artist   = self.db.entry_get(entry, rhythmdb.PROP_ARTIST)
            album    = self.db.entry_get(entry, rhythmdb.PROP_ALBUM)
            title    = self.db.entry_get(entry, rhythmdb.PROP_TITLE)
            duration = self.db.entry_get(entry, rhythmdb.PROP_DURATION)
            eid      = self.db.entry_get(entry, rhythmdb.PROP_ENTRY_ID)
            stream = None
            stream_title = \
                self.db.entry_request_extra_metadata(entry,
                                                     'rb:stream-song-title')
            if stream_title:
                stream = title
                title = stream_title
                if not artist:
                    artist = self.db.\
                        entry_request_extra_metadata(entry,
                                                     'rb:stream-song-artist')
                if not album:
                    album = self.db.\
                            entry_request_extra_metadata(entry,
                                                         'rb:stream-song-album')
            self.server.set_playing(artist, album, title, stream,duration,eid)
            self.server._ws_update()
        else:
            self.server.set_playing(None, None, None, None,None,None)

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

def make_websockserver(hostname, port, main):
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((hostname, port))
    sock.listen(1)
    logging.warning("simple websockserver upat port %s:%s", hostname, port)
    #main.register_websockserver(sock)
    gobject.io_add_watch(sock, gobject.IO_IN, websocklistener, main)

def websocklistener(sock, iotype, main):
    '''Asynchronous connection listener. Starts a handler for each connection.'''
    logging.warning("Got incoming")
    conn, addr = sock.accept()
    logging.warning("Connected to %s" , addr)
    L = WebSocket(conn, sock.getsockname(), main)
    logging.warning("Created new websockclient: %s", L)
    #L.set_server(sock)
    #main.register_websockclient(L)
    gobject.io_add_watch(conn, gobject.IO_IN, L.readsock)
    #gobject.io_add_watch(conn, gobject.IO_IN, handler)
    return True

class MobileRhythmServer(object):

    def __init__(self, hostname, port, plugin):
        self.plugin = plugin
        self.running = True
        self.artist = None
        self.album = None
        self.title = None
        self.stream = None
        self.duration = None
        self.eid      = None
        self._wsbuffer = cStringIO.StringIO()
        self._httpd = make_webserver(hostname, port)
        self._watch_httpd_id = gobject.io_add_watch(self._httpd.socket,
                                                    gobject.IO_IN,
                                                    self._idle_httpd_cb)
        if not hostname:
            hostname = "localhost"
        #self.websocket = WebSocketServer(hostname, port+1, conn_cb=self._ws_new_client) 
        self.websocket = make_websockserver(hostname, port+1, self)
        self._websockets = {}
        self._watched_clients = []
        #self._watch_websocket_id = gobject.timeout_add(200, self._idle_websocket_cb)
        #self._watch_websocket_id = gobject.idle_add(self._idle_websocket_cb)
        #self._watch_websocket_id = gobject.io_add_watch(self.websocket.socket,
        #     gobject.IO_IN | gobject.IO_OUT | gobject.IO_PRI | gobject.IO_ERR | gobject.IO_HUP,
        #                                         self._idle_websocket_cb,
        #                                         priority=gobject.PRIORITY_HIGH_IDLE)
        #self._update_websocket_id = gobject.timeout_add(5000, self._ws_update)

    def register_client(self, client):
        logging.warning("new client! %s" , client)
        client.onmessage = self._wsmessage
        client.writable = self._ws_check_pending
        client.handle_write = self._ws_push
        client.handle_error = self._ws_error
        self._websockets[client.fileno()] = client
        self._watched_clients.append(gobject.io_add_watch(client,
                                                          gobject.IO_IN | gobject.IO_OUT,
                                                          self._idle_websocket_cb2))


    def shutdown(self):
        gobject.source_remove(self._watch_httpd_id)
        gobject.source_remove(self._watch_websocket_id)
        self.running = False
        self.plugin = None

    def set_playing(self, artist, album, title, stream,duration,eid):
        self.artist = artist
        self.album = album
        self.title = title
        self.stream = stream
        self.duration = duration
        self.eid = eid

    def _send(self, data):
        #self.websocket.send(json.dumps(data))
        self._wsbuffer.write(json.dumps(data))

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

    def _ws_update(self):
        logging.warning("sending update")
        self._send({"playing": {"artist": self.artist,
                                "album": self.album,
                                "title": self.title,
                                "stream": self.stream,
                                "duration": self.duration,
                                "eid": self.eid}})

    def _open(self, filename):
        filename = os.path.join(os.path.dirname(__file__), filename)
        return open(filename)

    def _idle_httpd_cb(self, source, cb_condition):
        if not self.running:
            return False
        self._httpd.handle_request()
        return True

    def _idle_websocket_cb(self, source=None, cb_condition=None):
        if not self.running:
            return False
        logging.warning(source)
        logging.warning(cb_condition)
        gtk.gdk.threads_enter()
        logging.warning("processing websocket")
        process_websocket(5) 
        gtk.gdk.threads_leave()
        return True

    def _idle_websocket_cb2(self, source=None, cb_condition=None):
        if not self.running:
            return False
        if source.closed():
            return False
        gtk.gdk.threads_enter()
        logging.warning("processing websocket client")
        process_websocket(3) 
        gtk.gdk.threads_leave()
        return True

    def _wsmessage(self, data):
        message = json.loads(data)
        logging.warning("Got data: %s" % data)
        self.websocket.send("Got data: %s" % data)
        sys.stdout.write("got d: %s" % data)
        domain, action, args = message.split(":")
        if action == 'play':		
            if not player.get_playing():
                if not player.get_playing_source():
                    return self._play_entry(args)
                else:
                    return self._play(args)
            else:
                return self._pause(args)
        elif action == 'pause':
            player.pause()
        elif action == 'play-entry':
            return self._play_entry(args)
        elif action == 'next':
            player.do_next()
        elif action == 'prev':
            player.do_previous()
        elif action == 'stop':
            player.stop()
        elif action == 'set-vol':
            return self._setvolume(args)
        elif action == 'get-vol':
            return self._getvolume(args)
        elif action == 'vol-up':
            player.set_volume(player.get_volume() + 0.1)
        elif action == 'vol-down':
            player.set_volume(player.get_volume() - 0.1)
        elif action == 'get-playlist':
            return self._make_playlist()
        elif action == 'get-playing':
            return self._getplaying()
        elif action == 'set-play-time':
            return self._setplaypos(args)

    def _make_playlist(self):
        db = self.plugin.db
        #artist = '2pac'
        #qresult = (rhythmdb.QUERY_PROP_EQUALS, rhythmdb.PROP_ARTIST_FOLDED,artist.encode('utf-8'))

        libquery = (rhythmdb.QUERY_PROP_EQUALS, rhythmdb.PROP_TYPE,db.entry_type_get_by_name('song'))

        playlist_rows = self._player_search(libquery)
        if playlist_rows.get_size() == 0:
            return self._send({"playlist":[]})
        playlist = []
        for row in playlist_rows:
            entry = row[0]
            item = {"track_number":db.entry_get(entry, rhythmdb.PROP_TRACK_NUMBER),
                    "title":db.entry_get(entry, rhythmdb.PROP_TITLE),
                    "eid":db.entry_get(entry, rhythmdb.PROP_ENTRY_ID),
                    "title":db.entry_get(entry, rhythmdb.PROP_TITLE),
                    "artist":db.entry_get(entry, rhythmdb.PROP_ARTIST),
                    "album":db.entry_get(entry, rhythmdb.PROP_ALBUM),
                    "duration":db.entry_get(entry, rhythmdb.PROP_DURATION),
                    "genre":db.entry_get(entry, rhythmdb.PROP_GENRE),
            }
            playlist.append(item)
        self._send({"playlist":playlist})

    def _player_search(self, search):
        #"""perform a player search"""
        db = self.plugin.db
        query = db.query_new()
        db.query_append(query, search)
        query_model = db.query_model_new_empty()
        db.do_full_query_parsed(query_model, query)
        return query_model;

    def _setvolume(self, value):
        player = self.plugin.player
        player.set_volume(float(value))
        return self._getvolume()

    def _getvolume(self):
        player = self.plugin.player
        self._send({"current_vol":player.get_volume()})

    def _pause(self):
        player = self.plugin.player
        player.pause()
        self._send({"pause":True})

    def _play(self):
        player = self.plugin.player
        player.play()
        self._send({"playing":True})

    def _play_entry(self, args):
        player = self.plugin.player
        shell = self.plugin.shell
        db = self.plugin.db
        pentry = db.entry_lookup_by_id(int(args))
        sys.stdout.write('location value received : %s' %params['location'][0])
        sys.stdout.write('entry title: %s' % db.entry_get(pentry, rhythmdb.PROP_ARTIST))
        player.play_entry(pentry)
        self._send({"playing":True})

    def _setplaypos(self, position):
        player = self.plugin.player
        player.set_playing_time(int(position))
        return self._getplaypos()

    def _getplaypos(self):
        player = self.plugin.player
        self._send({"playtime":player.get_playing_time()})

    def _getplaying(self):
        player = self.plugin.player
        playing = None
        if self.stream or self.title:
            playing = {}
            for z in ("eid", "title", "artist", "album", "duration"):
                playing[z] = getattr(self, z)
            if self.stream:
                playing["stream"] = self.stream
        self._send({"current":playing})

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


