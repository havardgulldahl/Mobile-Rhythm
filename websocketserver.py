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
import asyncore

class WebSocket(asyncore.dispatcher_with_send):
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

    def _init__(self, client, server):
        asyncore.dispatcher_with_send.__init__(self)
        self.client = client
        self.server = server
        self.handshaken = False
        self.header = ""
        self.data = ""

    def set_server(self, server):
        self.server = server

    def handle_read(self):
        logging.info("Getting data from client")
        data = self.recv(1024)
        if not self.handshaken:
            self.header += data
            if self.header.find('\r\n\r\n') != -1:
                parts = self.header.split('\r\n\r\n', 1)
                self.header = parts[0]
                if self.dohandshake(self.header, parts[1]):
                    logging.info("Handshake successful")
                    self.handshaken = True           
        else:
            self.data += data
            validated = []
            msgs = self.data.split('\xff')
            self.data = msgs.pop()
            for msg in msgs:
                if msg[0] == '\x00':
                    self.onmessage(msg[1:]) 
                    
    def dohandshake(self, header, key=None): 
        logging.debug("Begin handshake: %s" % header) 
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
            logging.debug("Using challenge + response")
            challenge = struct.pack('!I', part_1) + struct.pack('!I', part_2) + key
            response = hashlib.md5(challenge).digest()
            handshake = WebSocket.handshake + response
        else:
            logging.warning("Not using challenge + response")
            handshake = WebSocket.handshake
        handshake = handshake % {'origin': origin, 'port': self.server.port, 
                                    'bind': self.server.socketbind}
        logging.debug("Sending handshake %s" % handshake)   
        self.out_buffer = handshake
        return True
                     
    def onmessage(self, data):
        logging.info("Got message: %s" % data)

    def send(self, data):
        logging.info("Sent message: %s" % data)
        self.out_buffer = "\x00%s\xff" % data

class WebSocketServer(asyncore.dispatcher):
    def __init__(self, bind, port, cls=WebSocket):
        asyncore.dispatcher.__init__(self)
        self.port = port
        self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
        self.bind((bind, port))
        self.socketbind = bind
        self.cls = cls
        self.connections = {}
        self.listen(5)
        logging.info("Listening on %s" % self.port)
        self.running = True

    def handle_accept(self):
        logging.debug("New client connection")
        client, address = self.accept()
        fileno = client.fileno()
        self.connections[fileno] = self.cls(client)
        self.connections[fileno].set_server(self)

def SetupWebSocket(host, port):
    logging.info("Starting WebSocketServer on %s, port %s", host, port)
    server = WebSocketServer("localhost", 9004) 
    asyncore.loop()
    return server

def process_websocket(loop):
    asyncore.loop(timeout=3, count=loop)

if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s - %(levelname)s - %(message)s")
    server = SetupWebSocket("localhost", 9004) 
    def signal_handler(signal, frame):
        logging.info("Caught Ctrl+C, shutting down...")
        server.running = False
        server.close()
        sys.exit()
    signal.signal(signal.SIGINT, signal_handler)
