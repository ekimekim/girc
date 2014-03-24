from __future__ import absolute_import

import logging
import errno

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

    def __init__(self, hostname, nick, port=IRC_PORT,
                 local_hostname=None, server_name=None, real_name=None,
                 disconnect_handler=None):
        self.hostname = hostname
        self.port = port
        self.nick = nick
        self._disconnect_handler = disconnect_handler or (lambda client: client.stop())
        self._socket = None
        self.real_name = real_name or nick
        self.local_hostname = local_hostname or socket.gethostname()
        self.server_name = server_name or 'gevent-irc'
        self._recv_queue = gevent.queue.Queue()
        self._send_queue = gevent.queue.Queue()
        self._group = gevent.pool.Group()
        self._handlers = {}
        self._global_handlers = set()

    def add_handler(self, to_call, *commands):
        if not commands:
            if hasattr(to_call, 'commands'):
                commands = to_call.commands
            else:
                self._global_handlers.add(to_call)
                return

        for command in commands:
            command = str(command).upper()
            self._handlers.setdefault(command, set()).add(to_call)

	def handler(self, *commands):
		"""Alternate form of add_handler, returns a decorator"""
		def _handler(fn):
			self.add_handler(fn, *commands)
			return fn
		return _handler

    def _handle(self, msg):
        handlers = self._global_handlers | self._handlers.get(msg.command, set())
        for handler in handlers:
            self._group.spawn(handler, self, msg)

    def send_message(self, msg):
        self._send_queue.put(msg.encode())

    def start(self):
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
                    logger.info("recv socket closed")
                    break
				lines = (partial+data).split('\r\n')
				partial = lines.pop() # everything after final \r\n
				for line in lines:
					self._process(line)
        except socket.error:
            logger.exception("Error while reading from recv socket")
        if partial:
            logger.warning("recv stream cut off mid-line, unused data: %r", partial)
        self._disconnect_handler(self)

    def _send_loop(self):
        while True:
            message = self._send_queue.get()
            line = message.encode()
            logger.debug("Sending message: %r", line)
            self._socket.sendall(line)
            if message.command == 'QUIT':
                logger.info("QUIT sent, client shutting down")
                self.stop()

    def _process(self, line):
        logging.debug("Received message: %r", line)
        try:
            msg = message.CTCPMessage.decode(line)
        except Exception:
            logging.warning("Could not decode message from server: %r", line, exc_info=True)
        self._handle(msg)

    def stop(self):
        # we spawn a child greenlet so things don't screw up if current greenlet is in self._group
        def _stop():
            self._group.kill()
            if self._socket is not None:
                self._socket.close()
                self._socket = None
        gevent.spawn(_stop).join()

    def join(self):
        self._group.join()

    def msg(self, to, content):
        self.send_message(message.PrivMsg(to, content))

    def quit(self, msg=None):
        self.send_message(message.Quit(msg))


if __name__ == '__main__':

    class MeHandler(object):
        commands = ['PRIVMSG']

        def __call__(self, client, msg):
            if client.nick == msg.params[0]:
                nick, _, _ = msg.prefix_parts
                client.send_message(
                        message.Me(nick, "do nothing it's just a bot"))

    nick = 'geventbot'
    client = Client('irc.freenode.net', nick, port=6667)
    client.add_handler(handlers.ping_handler, 'PING')
    client.add_handler(handlers.JoinHandler('#flood!'))
    # client.add_handler(hello.start, '001')
    client.add_handler(handlers.ReplyWhenQuoted("I'm just a bot"))
    client.add_handler(handlers.print_handler)
    client.add_handler(handlers.nick_in_user_handler, replycode.ERR_NICKNAMEINUSE)
    client.add_handler(handlers.ReplyToDirectMessage("I'm just a bot"))
    client.add_handler(MeHandler())
    client.start()
    client.join()


