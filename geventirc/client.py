
import logging
import errno
from collections import defaultdict

import gevent.queue
import gevent.pool
from gevent import socket

from geventirc import message

IRC_PORT = 194
IRCS_PORT = 994


class Client(object):
    _socket = None
    started = False
    stopped = False

    def __init__(self, hostname, nick, port=IRC_PORT, password=None,
                 local_hostname=None, server_name=None, real_name=None,
                 stop_handler=[], logger=None):
        """Create a new IRC connection to given host and port.
        local_hostname, server_name and real_name are optional args
            that control how we report ourselves to the server
        nick is the initial nick we set, though of course that can be changed later
        stop_handler is a callback that will be called upon the client exiting for any reason
            The callback should take one arg - this client.
            You may alternatively pass in a list of multiple callbacks.
            Note that after instantiation you can add/remove further disconnect callbacks
            by manipulating the client.stop_handlers set.
        """
        self.hostname = hostname
        self.port = port
        self.nick = nick
		self.password = password
        self.real_name = real_name or nick
        self.local_hostname = local_hostname or socket.gethostname()
        self.server_name = server_name or 'gevent-irc'

        self._recv_queue = gevent.queue.Queue()
        self._send_queue = gevent.queue.Queue()
        self._group = gevent.pool.Group()
        self._handlers = defaultdict(set)
        self.stop_handlers = set()

		if not logger:
			self.logger = logging.getLogger(__name__).getChild(type(self).__name__)

        if callable(stop_handler):
            self.stop_handlers.add(stop_handler)
        else:
            self.stop_handlers.update(stop_handler)

    def add_handler(self, *args):
        """Add callback to be called upon any of a list of commands being recieved.
        Callback should take args (client, message) and may return True to de-register itself.
        If no commands given, the callback is checked for an attr "commands" instead.
        If that is also not present (or empty), callback is called for all commands.

		Can be called in one of two ways; either directly:
			add_handler(callback, cmd, cmd, ...)
		or as a decorator:
			@add_handler(cmd, cmd, ...)
			def callback(client, message):
				...

		Commands can be string, int (for numeric replies) or message.Command subclass
        """
		# the tricky part - work out if a callback was given
		if (args # no args, no callback
		    and not isinstance(args[0], messages.Command) # if first arg is a Command, no callback
		    and callable(args[0]) # first arg needs to be callable to be a callback
		   ):
			callback = args[0]
			commands = args[1:]
		else:
			callback = None
			commands = args

		def _add_handler(self, callback):
			if not commands:
				commands = getattr(callback, 'commands', [])
			self.logger.info("Registering handler {} for commands {}".format(callback, commands))
			if not commands:
				commands = [None] # None represents "all commands"
			for command in commands:
				if isinstance(command, messages.Command):
					command = command.command
				if command is not None:
					command = str(command).upper()
				self._handlers[command].add(callback)

		if callback is None:
			return _add_handler
		return _add_handler(callback)

    def send(self, message, callback=None, block=False):
		"""Send message. If callback given, call when message sent.
		Callback takes args (client, message)
		If block=True, waits until message is sent before returning.
		You cannot pass both callback and block=True (callback is ignored).
		Note that if you simply need to ensure message Y is sent after message X,
		waiting is not required - messages are always sent in submitted order.
		"""
		if block:
			event = gevent.event.Event()
			callback = event.set
        self._send_queue.put((message, callback))
		if block:
			event.wait()

    def start(self):
        if self.stopped:
            self.logger.info("Ignoring start() - already stopped (please create a new Client instead)")
            return
        if self.started:
            self.logger.info("Ignoring start() - already started")
            return
        self.started = True
		self.logger.info("Starting client for {self.nick} on {self.hostname}:{self.port}".format(self=self))
		try:
			self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
			self.stop_handlers.add(lambda self: self._socket.close())
			self._socket.connect((self.hostname, self.port))
		except Exception:
			self.logger.exception("Error while connecting client")
			self.stop()
			return
		self._group.spawn(self._send_loop)
		self._group.spawn(self._recv_loop)
		if self.password:
			self.send(message.Pass(self.password))
		self.send(message.Nick(self.nick))
		self.send(message.User(self.nick,
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
                    self.logger.info("failed to recv, socket closed")
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
                message, callback = self._send_queue.get()
                line = message.encode()
                self.logger.debug("Sending message: {!r}".format(line))
                try:
                    self._socket.sendall(line)
                except socket.error as ex:
                    if ex.errno == errno.EPIPE:
                        self.logger.info("failed to send, socket closed")
                        break
                    raise
				if callback is not None:
					self._group.spawn(callback, self, message)
                if message.command == 'QUIT':
                    self.logger.info("QUIT sent, client shutting down")
					break
        except Exception:
            logger.exception("error in _send_loop")
        self.stop()

    def _process(self, line):
    	self.logger.debug("Received message: {!r}".format(line))
		line = line.strip()
		if not line:
			return
        try:
            msg = message.decode(line)
        except Exception:
            logging.warning("Could not decode message from server: {!r}".format(line), exc_info=True)
            return
		self.logger.debug("Handling message: {}".format(msg))
        handlers = self._handlers[None] | self._handlers[msg.command]
        for handler in handlers:
            self._group.spawn(self._handler_wrapper, handler, msg)

	def _handler_wrapper(self, handler, msg):
		self.logger.debug("Calling handler {} with message: {}".format(handler, msg))
		try:
			ret = handler(self, msg)
		except Exception:
			self.logger.exception("Handler {} failed".format(handler))
		if ret:
			for handler_set in self._handlers.values():
				if handler in handler_set:
					handler_set.remove(handler)

    def stop(self):
        self.stopped = True
        # we spawn a child greenlet so things don't screw up if current greenlet is in self._group
        def _stop():
            self._group.kill()
            if self._socket is not None:
                self._socket.close()
                self._socket = None
            for fn in self.stop_handlers:
                fn(self)
        gevent.spawn(_stop).join()

    def join(self):
        """Wait for client to exit"""
        event = gevent.event.Event()
        self.stop_handlers.add(lambda self: event.set())
        event.wait()

    def msg(self, to, content, callback=None, block=False):
		"""Shortcut to send a Privmsg. See send()"""
        self.send_message(message.Privmsg(to, content))

    def quit(self, msg=None, callback=None, block=False):
		"""Shortcut to send a Quit. See send()"""
        self.send_message(message.Quit(msg))

	def wait_for(self, ):
		"""Block until a message matching given args is received.
		The matching message is returned.
		The following args control matching:
