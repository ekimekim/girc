
import logging
import errno
import string
import random
from collections import defaultdict

import gevent.queue
import gevent.pool
import gevent.event
import gevent.lock
from gevent import socket

import message
import replycodes
from server_properties import ServerProperties


DEFAULT_PORT = 6667


class Client(object):
	_socket = None
	started = False
	stopped = False

	def __init__(self, hostname, nick, port=DEFAULT_PORT, password=None,
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
		self.password = password
		self.real_name = real_name or nick
		self.local_hostname = local_hostname or socket.gethostname()
		self.server_name = server_name or hostname

		self._recv_queue = gevent.queue.Queue()
		self._send_queue = gevent.queue.Queue()
		self._group = gevent.pool.Group()
		self.message_handlers = defaultdict(list) # maps handler to set of registered match_args
		                                          # we use list instead of set because dicts aren't hashable
		                                          # and this was the easist workaround
		self.stop_handlers = set()
		self.server_properties = ServerProperties()

		# NOTE: An aside about nicks
		# When our nick is changing, race cdns mean we aren't sure if the server is expecting
		# our old nick or our new nick. We have a few different attributes to address this:
		# self.nick: a lock-protected lookup of self._nick (the lock is self._nick_lock),
		#            it represents what we should refer to ourself as (and blocks if it's ambiguous)
		# self._nick: what we think the server thinks our nick is. when our nick is in the process
		#             of changing, this is the OLD nick.
		# self._new_nick: None unless it is in the process of changing. When changing, the nick we
		#                 are switching to.
		# We can only attempt to change our nick when it is not in the middle of changing (we do this
		# by holding self._nick_lock). If we get a forced nick change x -> y while changing, it changes
		# self._nick if x is the old nick, or self._new_nick if x is the new nick.
		# self.matches_nick() will always check both _nick and _new_nick.
		self._nick = nick
		self._nick_lock = gevent.lock.RLock()
		self._new_nick = None

		if not logger:
			self.logger = logging.getLogger(__name__).getChild(type(self).__name__)

		if callable(stop_handler):
			self.stop_handlers.add(stop_handler)
		else:
			self.stop_handlers.update(stop_handler)

		# init messages
		if self.password:
			message.Pass(self, self.password).send()
		message.Nick(self, self.nick).send()
		message.User(self, self.nick, self.local_hostname, self.server_name, self.real_name).send()

		# default handlers

		@self.add_handler(command=message.ISupport)
		def recv_support(client, msg):
			self.server_properties.update(msg.properties)

		@self.add_handler(command=message.Ping)
		def on_ping(client, msg):
			message.Pong(client, msg.payload).send()

		@self.add_handler(command=replycodes.errors.NICKNAMEINUSE)
		def nick_in_use(client, msg):
			bad_nick = msg.params[0]
			if self._new_nick:
				# if we're changing nicks, ignore it unless it matches the new one
				if bad_nick != self._new_nick:
					return
				# cancel current change and wait
				self._new_nick = self._nick
			else:
				# if we aren't changing nicks...
				if bad_nick != self._nick:
					return # this is some kind of race cdn
			# if we've made it here, we want to increment our nick
			with self._nick_lock:
				# now that we've waited for any other operations to finish, let's double check
				# that we're still talking about the same nick
				if bad_nick != self._nick:
					return
				self.nick = self.increment_nick(self._nick)

		@self.add_handler(command='NICK', sender=self.matches_nick)
		def forced_nick_change(client, msg):
			if msg.sender == self._new_nick:
				# we are changing, and this was sent after our change was recieved so we must respect it.
				self._new_nick = msg.nickname
			elif msg.sender == self._nick:
				# either we aren't changing and everything is fine, or we are changing but this was
				# sent before the NICK command was processed by the server, so we change our old value
				# so further forced_nick_changes and matches_nick() still works.
				self._nick = msg.nickname

	@property
	def nick(self):
		"""Get our current nick. May block if it is in the middle of being changed."""
		with self._nick_lock:
			return self._nick

	@nick.setter
	def nick(self, new_nick):
		"""Change the nick safely. Note this will block until the change is sent, acknowledged
		and self.nick is updated.
		"""
		with self._nick_lock:
			try:
				self._new_nick = new_nick
				nick_msg = message.Nick(self, new_nick)
				try:
					nick_msg.send()
					# by waiting for messages, we force ourselves to wait until the Nick() has been processed
					self.wait_for_messages()
				except Exception:
					self.quit("Unrecoverable error while changing nick")
					raise
				self._nick = self._new_nick # note that self._new_nick may not be new_nick, see forced_nick_change()
			finally:
				# either we completed successfully or we aborted
				# either way, we need to no longer be in the middle of changing nicks
				self._new_nick = None

	def matches_nick(self, value):
		"""A helper function for use in match args. It takes a value and returns whether that value
		matches the client's current nick.
		Note that you should use this, NOT self.nick, for checking incoming messages.
		This function will continue working when self.nick is ambiguous, whereas the latter will block.
		"""
		return value in (self._nick, self._new_nick)

	def add_handler(self, callback=None, **match_args):
		"""Add callback to be called upon a matching message being received.
		See geventirc.message.match() for match_args.
		Callback should take args (client, message) and may return True to de-register itself.
		If callback is already registered, it will trigger on either the new match_args or the existing ones.

		If callback is not given, returns a decorator.
		ie.
			def foo(client, message):
				...
			client.add_handler(foo, **match_args)
		is identical to
			@client.add_handler(**match_args)
			def foo(client, message):
				...
		"""
		def _add_handler(callback):
			self.logger.info("Registering handler {} with match args {}".format(callback, match_args))
			self.message_handlers[callback].append(match_args)
			return callback

		if callback is None:
			return _add_handler
		_add_handler(callback)

	def rm_handler(self, handler):
		"""Remove the given handler, if it is registered."""
		if handler in self.message_handlers:
			del self.message_handlers[handler]

	def _send(self, message, callback):
		"""A low level interface to send a message. You normally want to use Message.send() instead.
		Callback is called after message is sent, and takes args (client, message).
		Callback may be None.
		"""
		self._send_queue.put((message, callback))

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
					self.logger.info("no data from recv, socket closed")
					break
				lines = (partial+data).split('\r\n')
				partial = lines.pop() # everything after final \r\n
				for line in lines:
					self._process(line)
		except Exception:
			self.logger.exception("error in _recv_loop")
		if partial:
			self.logger.warning("recv stream cut off mid-line, unused data: %r", partial)
		self.stop()

	def _send_loop(self):
		try:
			while True:
				message, callback = self._send_queue.get()
				line = "{}\r\n".format(message.encode())
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
			self.logger.exception("error in _send_loop")
		self.stop()

	def _process(self, line):
		self.logger.debug("Received message: {!r}".format(line))
		line = line.strip()
		if not line:
			return
		try:
			msg = message.decode(line, self)
			self.logger.debug("Handling message: {}".format(msg))
			for handler, match_arg_set in self.message_handlers.items():
				if any(message.match(msg, **match_args) for match_args in match_arg_set):
					self._group.spawn(self._handler_wrapper, handler, msg)
		except message.InvalidMessage:
			logging.warning("Could not decode message from server: {!r}".format(line), exc_info=True)
			return

	def _handler_wrapper(self, handler, msg):
		self.logger.debug("Calling handler {} with message: {}".format(handler, msg))
		try:
			ret = handler(self, msg)
		except Exception:
			self.logger.exception("Handler {} failed".format(handler))
		if ret:
			self.rm_handler(handler)

	def stop(self):
		if self.stopped:
			return
		self.stopped = True
		# we spawn a child greenlet so things don't screw up if current greenlet is in self._group
		def _stop():
			self._group.kill()
			for fn in self.stop_handlers:
				fn(self)
		gevent.spawn(_stop).join()

	def wait_for_stop(self):
		"""Wait for client to exit"""
		event = gevent.event.Event()
		self.stop_handlers.add(lambda self: event.set())
		event.wait()

	def msg(self, to, content, block=False):
		"""Shortcut to send a Privmsg. See Message.send()"""
		message.Privmsg(self, to, content).send(block=block)

	def quit(self, msg=None, block=False):
		"""Shortcut to send a Quit. See Message.send().
		Note that sending a quit automatically stops the client."""
		message.Quit(self, msg).send()
		if block:
			self.wait_for_stop()

	def wait_for(self, **match_args):
		"""Block until a message matching given args is received.
		The matching message is returned.
		See geventirc.message.match() for match_args"""
		result = gevent.event.AsyncResult()
		@self.add_handler(**match_args)
		def wait_callback(self, msg):
			result.set(msg)
			return True # unregister
		return result.get()

	def wait_for_messages(self):
		"""This function will attempt to block until the server has received and processed
		all current messages. We rely on the fact that servers will generally react to messages
		in order, and so we queue up a Ping and wait for the corresponding Pong."""
		# We're conservative here with our payload - 8 characters only, letters and digits,
		# and we assume it's case insensitive. This still gives us about 40 bits of information.
		payload = ''.join(random.choice(string.lowercase + string.digits) for x in range(8))
		received = gevent.event.Event()
		@self.add_handler(command=message.Pong, payload=lambda value: value.lower() == payload)
		def on_pong(client, msg):
			received.set()
			return True # unregister
		received.wait()

	def normalize_channel(self, name):
		"""Ensures that a channel name has a correct prefix, defaulting to the first entry in CHANTYPES."""
		if not name:
			raise ValueError("Channel name cannot be empty")
		if name[0] in self.server_properties.CHANTYPES:
			return name
		return "{prefix}{name}".format(name=name, prefix=self.server_properties.CHANTYPES[0])
