from __future__ import absolute_import

import logging
import errno
from collections import defaultdict

import gevent.queue
import gevent.pool
from gevent import socket

from geventirc import message
from geventirc import replycode
from geventirc import handlers

IRC_PORT = 194
IRCS_PORT = 994

logger = logging.getLogger(__name__)


class Client(object):
    _socket = None
    started = False
    stopped = False

    def __init__(self, hostname, nick, port=IRC_PORT,
                 local_hostname=None, server_name=None, real_name=None,
                 disconnect_handler=[]):
        """Create a new IRC connection to given host and port.
        local_hostname, server_name and real_name are optional args
            that control how we report ourselves to the server
        nick is the initial nick we set, though of course that can be changed later
        disconnect_handler is a callback that will be called upon the client exiting for any reason
            The callback should take one arg - this client.
            You may alternatively pass in a list of multiple callbacks.
            Note that after instantiation you can add/remove further disconnect callbacks
            by manipulating the client.disconnect_handlers set.
        """
        self.hostname = hostname
        self.port = port
        self.nick = nick
        self.real_name = real_name or nick
        self.local_hostname = local_hostname or socket.gethostname()
        self.server_name = server_name or 'gevent-irc'

        self._recv_queue = gevent.queue.Queue()
        self._send_queue = gevent.queue.Queue()
        self._group = gevent.pool.Group()
        self._handlers = defaultdict(set)
        self._global_handlers = set()
        self._disconnect_handlers = set()

        if callable(disconnect_handler):
            self._disconnect_handlers.add(disconnect_handler)
        else:
            self._disconnect_handlers.update(disconnect_handler)

    def add_handler(self, to_call, *commands):
        """Add callback to be called upon any of *commands being recieved.
        Callback should take args (client, message)
        If no *commands given, the callback is checked for an attr "commands" instead.
        If that is also not present (or empty), callback is called for all commands.
        """
        if not commands:
            commands = getattr(to_call, 'commands', [])

        if not commands:
            self._global_handlers.add(to_call)

        for command in commands:
            command = str(command).upper()
            self._handlers[command].add(to_call)

    def handler(self, *commands):
        """Alternate form of add_handler, returns a decorator"""
        def _handler(fn):
            self.add_handler(fn, *commands)
            return fn
        return _handler

    def _handle(self, msg):
        handlers = self._global_handlers | self._handlers[msg.command]
        for handler in handlers:
            self._group.spawn(handler, self, msg)

    def send_message(self, message):
        self._send_queue.put(message)

    def start(self):
        if self.stopped:
            logger.info("Ignoring start() - already stopped (please create a new Client instead)")
            return
        if self.started:
            logger.info("Ignoring start() - already started")
            return
        self.started = True
        logger.info('connecting to %r:%d', self.hostname, self.port)
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.connect((self.hostname, self.port))
        self._group.spawn(self._send_loop)
        self._group.spawn(self._recv_loop)
        self.send_message(message.Nick(self.nick))
        self.send_message(message.User(self.nick,
                                       self.local_hostname,
                                       self.server_name,
                                       self.real_name))

    def _recv_loop(self):
        partial = ''
        try:
            while True:
                try:
                    data = self._socket.recv(4096)
                except socket.error as ex:
                    if ex.errno == errno.EINTR: # retry on EINTR
                        continue
                    raise
                if not data:
                    logger.info("failed to recv, socket closed")
                    break
                lines = (partial+data).split('\r\n')
                partial = lines.pop() # everything after final \r\n
                for line in lines:
                    self._process(line)
        except Exception:
            logger.exception("error in _recv_loop")
        if partial:
            logger.warning("recv stream cut off mid-line, unused data: %r", partial)
        self.stop(self)

    def _send_loop(self):
        try:
            while True:
                message = self._send_queue.get()
                line = message.encode()
                logger.debug("Sending message: %r", line)
                try:
                    self._socket.sendall(line)
                except socket.error as ex:
                    if ex.errno == errno.EPIPE:
                        logger.info("failed to send, socket closed")
                        break
                    raise
                if message.command == 'QUIT':
                    logger.info("QUIT sent, client shutting down")
                    self.stop()
        except Exception:
            logger.exception("error in _send_loop")
        self.stop(self)

    def _process(self, line):
        logging.debug("Received message: %r", line)
        try:
            msg = message.CTCPMessage.decode(line)
        except Exception:
            logging.warning("Could not decode message from server: %r", line, exc_info=True)
            return
        self._handle(msg)

    def stop(self):
        self.stopped = True
        # we spawn a child greenlet so things don't screw up if current greenlet is in self._group
        def _stop():
            self._group.kill()
            if self._socket is not None:
                self._socket.close()
                self._socket = None
            for fn in self.disconnect_handlers:
                fn(self)
        gevent.spawn(_stop).join()

    def join(self):
        """Wait for client to exit"""
        event = gevent.event.Event()
        self.disconnect_handlers.add(lambda self: event.set())
        event.wait()

    def msg(self, to, content):
        self.send_message(message.PrivMsg(to, content))

    def quit(self, msg=None):
        self.send_message(message.Quit(msg))

