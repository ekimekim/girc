
import logging
import errno
import string
import random
import weakref

import gevent.queue
import gevent.pool
import gevent.event
import gevent.lock
from gevent import socket

from girc import message
from girc import replycodes
from girc.handler import Handler
from girc.server_properties import ServerProperties
from girc.channel import Channel


DEFAULT_PORT = 6667


class ConnectionClosed(Exception):
	def __str__(self):
		return "The connection was unexpectedly closed"


class Client(object):
	_socket = None
	started = False

	WAIT_FOR_MESSAGES_TIMEOUT = 10
	PING_IDLE_TIME = 60
	PING_TIMEOUT = 30

	def __init__(self, hostname, nick, port=DEFAULT_PORT, password=None, nickserv_password=None,
		         ident=None, real_name=None, stop_handler=[], logger=None):
		"""Create a new IRC connection to given host and port.
		ident and real_name are optional args that control how we report ourselves to the server
		(they both default to nick).
		nick is the initial nick we set, though of course that can be changed later.
		password is the server password, ie. as set by a PASS command.
		nickserv_password will be sent in a Privmsg to NickServ with "IDENTIFY" after connecting.
		stop_handler is a callback that will be called upon the client exiting for any reason
			The callback should take args (client, ex) where client is this client and ex is the fatal error,
				or None for a clean disconnect.
			You may alternatively pass in a list of multiple callbacks.
			Note that after instantiation you can add/remove further disconnect callbacks
			by manipulating the client.stop_handlers set.
		"""
		self.hostname = hostname
		self.port = port
		self.password = password
		self.nickserv_password = nickserv_password
		self.ident = ident or nick
		self.real_name = real_name or nick
		self._channels = {}
		self._users = weakref.WeakValueDictionary()

		self._recv_queue = gevent.queue.Queue()
		self._send_queue = gevent.queue.Queue()
		self._group = gevent.pool.Group()
		self._activity = gevent.event.Event() # set each time we send or recv, for idle watchdog
		self._stopped = gevent.event.AsyncResult() # contains None if exited cleanly, else set with exception
		self.message_handlers = set() # set of Handler objects
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
			logger = logging.getLogger(__name__).getChild(type(self).__name__)
		self.logger = logger

		if callable(stop_handler):
			self.stop_handlers.add(stop_handler)
		else:
			self.stop_handlers.update(stop_handler)

		# init messages
		if self.password:
			message.Pass(self, self.password).send()
		message.Nick(self, self.nick).send()
		message.User(self, self.ident, self.real_name).send()
		if self.nickserv_password:
			self.msg('NickServ', 'IDENTIFY {}'.format(self.nickserv_password))

		# default handlers

		@self.handler(command=message.ISupport, sync=True)
		def recv_support(client, msg):
			self.server_properties.update(msg.properties)

		@self.handler(command=message.Ping)
		def on_ping(client, msg):
			message.Pong(client, msg.payload).send()

		@self.handler(command=replycodes.errors.NICKNAMEINUSE, sync=True)
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

		@self.handler(command='NICK', sender=self.matches_nick, sync=True)
		def forced_nick_change(client, msg):
			if msg.sender == self._new_nick:
				# we are changing, and this was sent after our change was recieved so we must respect it.
				self._new_nick = msg.nickname
			elif msg.sender == self._nick:
				# either we aren't changing and everything is fine, or we are changing but this was
				# sent before the NICK command was processed by the server, so we change our old value
				# so further forced_nick_changes and matches_nick() still works.
				self._nick = msg.nickname

		@self.handler(command='JOIN', sender=self.matches_nick, sync=True)
		def forced_join(client, msg):
			for name in msg.channels:
				channel = self.channel(name)
				channel._join()

	@property
	def stopped(self):
		return self._stopped.ready()

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

	def handler(self, callback=None, **match_args):
		"""Add callback to be called upon a matching message being received.
		See geventirc.message.match() for match_args.
		Callback should take args (client, message) and may return True to de-register itself.

		If callback is not given, returns a decorator.
		ie.
			def foo(client, message):
				...
			client.handler(foo, **match_args)
		is identical to
			@client.handler(**match_args)
			def foo(client, message):
				...

		For more detail, see handler.Handler()
		This function simply creates a Handler() and immediately registers it with this client.
		"""
		return Handler(client=self, callback=callback, **match_args)

	def _send(self, message, callback):
		"""A low level interface to send a message. You normally want to use Message.send() instead.
		Callback is called after message is sent, and takes args (client, message).
		Callback may be None.
		"""
		self.logger.debug("Queuing message {}".format(message))
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
		except Exception as ex:
			self.logger.exception("Error while connecting client")
			self.stop(ex)
			raise
		for func in (self._send_loop, self._recv_loop, self._idle_watchdog):
			self._group.spawn(func)

	def _idle_watchdog(self):
		"""Sends a ping if no activity for PING_IDLE_TIME seconds.
		Disconnect if there is no response within PING_TIMEOUT seconds."""
		error = None
		try:
			while True:
				if self._activity.wait(self.PING_IDLE_TIME):
					self._activity.clear()
					continue
				self.logger.info("No activity for {}s, sending PING".format(self.PING_IDLE_TIME))
				if not self.wait_for_messages(self.PING_TIMEOUT):
					self.logger.error("No response to watchdog PING after {}s".format(self.PING_TIMEOUT))
					break
		except Exception as ex:
			self.logger.exception("error in _idle_watchdog")
			error = ex
		self.stop(error or ConnectionClosed())

	def _recv_loop(self):
		partial = ''
		error = None
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
		except Exception as ex:
			self.logger.exception("error in _recv_loop")
			error = ex
		if partial:
			self.logger.warning("recv stream cut off mid-line, unused data: {!r}".format())
		self.stop(error or ConnectionClosed())

	def _send_loop(self):
		error = None
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
		except Exception as ex:
			self.logger.exception("error in _send_loop")
		self.stop(error or ConnectionClosed())

	def _process(self, line):
		self.logger.debug("Received message: {!r}".format(line))
		line = line.strip()
		if not line:
			return
		try:
			msg = message.decode(line, self)
		except message.InvalidMessage:
			self.logger.warning("Could not decode message from server: {!r}".format(line), exc_info=True)
			return
		self.logger.debug("Getting handlers for message: {}".format(msg))
		self._dispatch_handlers(msg)

	def _dispatch_handlers(self, msg):
		"""Carefully builds a set of greenlets for all message handlers, obeying ordering metadata for each handler.
		Returns when all sync=True handlers have been executed."""
		# build dependency graph
		graph = {handler: set() for handler in self.message_handlers}
		graph['sync'] = set()
		for handler in self.message_handlers:
			for other in handler.after:
				if other in graph:
					graph[handler].add(other)
			for other in handler.before:
				if other in graph:
					graph[other].add(handler)
		# check for cycles
		def check_cycles(handler, chain=()):
			if handler in chain:
				chain_text = " -> ".join(map(str, chain + (handler,)))
				raise ValueError("Dependency cycle in handlers: {}".format(chain_text))
			chain += handler,
			for dep in graph[handler]:
				check_cycles(dep, chain)
		for handler in graph:
			check_cycles(handler)
		# set up the greenlets
		greenlets = {}
		def wait_and_handle(handler):
			for dep in graph[handler]:
				greenlets[dep].join()
			return handler.handle(self, msg)
		def wait_for_sync():
			for dep in graph['sync']:
				greenlets[dep].join()
		for handler in self.message_handlers:
			greenlets[handler] = self._group.spawn(wait_and_handle, handler)
		greenlets['sync'] = self._group.spawn(wait_for_sync)
		# wait for sync to finish
		greenlets['sync'].get()

	def stop(self, ex=None):
		if self.stopped:
			return
		if ex:
			self._stopped.set_exception(ex)
		else:
			self._stopped.set(None)

		# we spawn a child greenlet so things don't screw up if current greenlet is in self._group
		def _stop():

			self._group.kill()
			for fn in self.stop_handlers:
				fn(self)

			# post-stop: we clear a few structures to break reference loops
			# since they no longer make sense.
			for channel in self._channels.values():
				channel.client = None
			for user in self._users.values():
				user.client = None
			for handler in self.message_handlers.copy():
				handler.unregister_all(self)
			# queues might contain some final messages
			self._send_queue = None
			self._recv_queue = None

		gevent.spawn(_stop).join()

	def msg(self, to, content, block=False):
		"""Shortcut to send a Privmsg. See Message.send()"""
		message.Privmsg(self, to, content).send(block=block)

	def quit(self, msg=None, block=True):
		"""Shortcut to send a Quit. See Message.send().
		Note that sending a quit automatically stops the client."""
		message.Quit(self, msg).send()
		_stop = gevent.spawn(self.stop)
		if block:
			_stop.get()

	def channel(self, name):
		"""Fetch a channel object, or create it if it doesn't exist.
		Note that the channel is not joined automatically."""
		name = self.normalize_channel(name)
		if name not in self._channels:
			Channel(self, name) # this will register itself into _channels
		return self._channels[name]

	def wait_for(self, **match_args):
		"""Block until a message matching given args is received.
		The matching message is returned.
		See geventirc.message.match() for match_args"""
		result = gevent.event.AsyncResult()
		@self.handler(**match_args)
		def wait_callback(self, msg):
			result.set(msg)
			return True # unregister
		return result.get()

	def wait_for_stop(self):
		"""Wait for client to exit, raising if it failed"""
		self._stopped.get()

	def wait_for_messages(self, timeout=None):
		"""This function will attempt to block until the server has received and processed
		all current messages. We rely on the fact that servers will generally react to messages
		in order, and so we queue up a Ping and wait for the corresponding Pong."""
		# We're conservative here with our payload - 8 characters only, letters and digits,
		# and we assume it's case insensitive. This still gives us about 40 bits of information.
		# Also, some servers set the payload to their server name in the reply
		# and attach the payload as a second arg. Finally, we just dump a reasonable timeout
		# over the whole thing, just in case.
		payload = ''.join(random.choice(string.lowercase + string.digits) for x in range(8))
		received = gevent.event.Event()
		def match_payload(params):
			return any(value.lower() == payload for value in params)
		@self.handler(command=message.Pong, params=match_payload)
		def on_pong(client, msg):
			received.set()
			return True # unregister
		message.Ping(self, payload).send()
		if received.wait(self.WAIT_FOR_MESSAGES_TIMEOUT if timeout is None else timeout):
			return True
		self.logger.warning("Timed out while waiting for matching pong in wait_for_messages()")
		return False

	# aliases - the wait_for_* names are more descriptive, but they map to common async concepts:
	join = wait_for_stop
	sync = wait_for_messages

	def normalize_channel(self, name):
		"""Ensures that a channel name has a correct prefix, defaulting to the first entry in CHANTYPES."""
		if not name:
			raise ValueError("Channel name cannot be empty")
		if name[0] in self.server_properties.CHANTYPES:
			return name
		return "{prefix}{name}".format(name=name, prefix=self.server_properties.CHANTYPES[0])
