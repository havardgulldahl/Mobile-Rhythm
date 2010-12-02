# -*- encoding: utf8 -*-
#
# Simple WebSockets in Python
# By Håvard Gulldahl <havard@gulldahl.no>
# GPL2+
# Based on example by David Arthur
# https://gist.github.com/512987
#

import struct
import socket
import hashlib
import sys
import re
import logging
import signal
#import asyncore

class WebSocket(object):#asyncore.dispatcher_with_send):
    handshake = (
        "HTTP/1.1 101 Web Socket Protocol Handshake\r\n"
        "Upgrade: WebSocket\r\n"
        "Connection: Upgrade\r\n"
        "WebSocket-Origin: %(origin)s\r\n"
        "WebSocket-Location: ws://%(bind)s:%(port)s/\r\n"
        "Sec-Websocket-Origin: %(origin)s\r\n"
        "Sec-Websocket-Location: ws://%(bind)s:%(port)s/\r\n"
        "\r\n"
    )
    handshaken = False
    header = ""
    data = ""
    server = None

    def __init__(self, client, bindinfo, server):
        #asyncore.dispatcher_with_send.__init__(self)
        self.client = client
        self.bindinfo = bindinfo
        self.server = server
        self.handshaken = False
        self.header = ""
        self.data = ""

    def readsock(self, *args):
        logging.warning("Getting data from client: %s", (self.bindinfo))
        data = self.client.recv(1024)
        if not self.handshaken:
            self.header += data
            if self.header.find('\r\n\r\n') != -1:
                parts = self.header.split('\r\n\r\n', 1)
                self.header = parts[0]
                if self.dohandshake(self.header, parts[1]):
                    logging.warning("Handshake successful")
                    self.handshaken = True           
        else:
            self.data += data
            validated = []
            msgs = self.data.split('\xff')
            self.data = msgs.pop()
            for msg in msgs:
                if msg[0] == '\x00':
                    self.onmessage(msg[1:]) 
        return True
                    
    def dohandshake(self, header, key=None): 
        logging.warning("Begin handshake: %s" % header) 
        digitRe = re.compile(r'[^0-9]')
        spacesRe = re.compile(r'\s')
        part_1 = part_2 = origin = None
        for line in header.split('\r\n')[1:]:
            name, value = line.split(': ', 1)
            if name.lower() == "sec-websocket-key1":
                key_number_1 = int(digitRe.sub('', value))
                spaces_1 = len(spacesRe.findall(value))
                if spaces_1 == 0:
                    return False
                if key_number_1 % spaces_1 != 0:
                    return False
                part_1 = key_number_1 / spaces_1
            elif name.lower() == "sec-websocket-key2":
                key_number_2 = int(digitRe.sub('', value))
                spaces_2 = len(spacesRe.findall(value))
                if spaces_2 == 0:
                    return False
                if key_number_2 % spaces_2 != 0:
                    return False
                part_2 = key_number_2 / spaces_2
            elif name.lower() == "origin":
                origin = value
        if part_1 and part_2:
            logging.warning("Using challenge + response")
            challenge = struct.pack('!I', part_1) + struct.pack('!I', part_2) + key
            response = hashlib.md5(challenge).digest()
            handshake = WebSocket.handshake + response
        else:
            logging.warning("Not using challenge + response")
            handshake = WebSocket.handshake
        handshake = handshake % {'origin': origin, 'port': self.bindinfo[1],
                                    'bind': self.bindinfo[0] }
        logging.warning("Sending handshake %s" % handshake)   
        self.client.send(handshake)
        return True
                     
    def onmessage(self, data):
        logging.warning("Got message: %s" % data)

    def close(self):
        logging.warning("websocket closed")
        self.client.close()

    def send(self, data):
        logging.warning("Sent message: %s" % data)
        self.client.send("\x00%s\xff" % data)

class WebSocketServer(object):#asyncore.dispatcher):
    def __init__(self, bind, port, cls=WebSocket, conn_cb=None):
        #asyncore.dispatcher.__init__(self)
        self.port = port
        self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
        #asyncore.dispatcher.set_reuse_addr(self)
        self.bind((bind, port))
        self.socketbind = bind
        self.cls = cls
        self.connections = {}
        self.connection_callback = conn_cb
        self.listen(5)
        logging.warning("Listening on %s" % self.port)
        self.running = True

    def handle_accept(self):
        logging.warning("New client connection")
        client, address = self.accept()
        fileno = client.fileno()
        self.connections[fileno] = self.cls(client)
        self.connections[fileno].set_server(self)
        if self.connection_callback is not None:
            self.connection_callback(self.connections[fileno])

def SetupWebSocket(host, port):
    logging.info("Starting WebSocketServer on %s, port %s", host, port)
    server = WebSocketServer(host, port)
    #asyncore.loop()
    return server

def process_websocket(loop):
    logging.warning("Running asyncore loop %i times", loop)
    #asyncore.loop(timeout=3, count=loop)
    #asyncore.poll(0.001)
    #asyncore.loop(timeout=1, count=loop)

if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s - %(levelname)s - %(message)s")
    server = SetupWebSocket("192.168.0.20", 9004) 
    def signal_handler(signal, frame):
        logging.info("Caught Ctrl+C, shutting down...")
        server.running = False
        server.close()
        sys.exit()
    signal.signal(signal.SIGINT, signal_handler)
