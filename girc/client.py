
import errno
import json
import logging
import random
import string
import time
import weakref
from base64 import b64encode, b64decode

import gevent.queue
import gevent.pool
import gevent.event
import gevent.lock
from gevent import socket

from girc import message
from girc import replycodes
from girc.handler import Handler, BoundHandler
from girc.server_properties import ServerProperties
from girc.channel import Channel
from girc.chunkprioqueue import ChunkedPriorityQueue
from girc.common import send_fd, recv_fd


DEFAULT_PORT = 6667


class ConnectionClosed(Exception):
	def __str__(self):
		return "The connection was unexpectedly closed"


class Client(object):
	_socket = None
	started = False

	# some insight into the state of _recv_loop to allow for smooth connection handoff
	_recv_buf = ''
	_kill_recv = False
	_stopping = False

	REGISTRATION_TIMEOUT = 5
	WAIT_FOR_MESSAGES_TIMEOUT = 10
	PING_IDLE_TIME = 60
	PING_TIMEOUT = 30

	def __init__(self, hostname, nick, port=DEFAULT_PORT, password=None, nickserv_password=None,
		         ident=None, real_name=None, stop_handler=[], logger=None, version='girc', time='local',
		         twitch=False):
		"""Create a new IRC connection to given host and port.
		ident and real_name are optional args that control how we report ourselves to the server
		(they both default to nick).
		Similarly, version and time control our response to interrogative commands. Either can be set
			None to disable response. Time defaults to 'local' (use local time) but 'utc' is also an option.
		nick is the initial nick we set, though of course that can be changed later.
		password is the server password, ie. as set by a PASS command.
		nickserv_password will be sent in a Privmsg to NickServ with "IDENTIFY" after connecting.
		stop_handler is a callback that will be called upon the client exiting for any reason
			The callback should take args (client, ex) where client is this client and ex is the fatal error,
				or None for a clean disconnect.
			You may alternatively pass in a list of multiple callbacks.
			Note that after instantiation you can add/remove further disconnect callbacks
			by manipulating the client.stop_handlers set.
		twitch=True sets some special behaviour for better operation with twitch.tv's unique variant of IRC.
		"""
		self.hostname = hostname
		self.port = port
		self.password = password
		self.nickserv_password = nickserv_password
		self.ident = ident or nick
		self.real_name = real_name or nick
		self.version = version
		self.time = time
		self._channels = {}
		self._users = weakref.WeakValueDictionary()

		self._recv_queue = gevent.queue.Queue()
		self._send_queue = ChunkedPriorityQueue()
		# Message priorities are used as follows:
		# -2: Critical registration messages with strict ordering
		# -1: PONGs sent in reply to PINGs, required to not get disconnected
		# 0: Other high-priority tasks - changing NICK, sending idle PINGs, etc
		# >0: User messages
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

		if twitch:
			message.Message(self, 'CAP', 'REQ', 'twitch.tv/membership twitch.tv/commands twitch.tv/tags').send()

		if self.nickserv_password:
			self.msg('NickServ', 'IDENTIFY {}'.format(self.nickserv_password), priority=0)

		# Register Handler methods
		Handler.register_all(self, self)

	@classmethod
	def _from_handoff(cls, sock, recv_buf, channels, **init_args):
		"""Alternate constructor that takes the args needed to resume a connection handed off
		from another Client instance. Used to implement graceful restart without dropping connections.
		sock is the connection to inherit.
		recv_buf may contain data that was read from the socket but not processed (eg. a partial line)
		channels is a list of joined channels
		init args must match the ones from the handing off Client.
		"""
		client = cls(**init_args)
		client.logger.info("Initializing client from handoff args ({} channels)".format(len(channels)))
		client._socket = sock
		client._recv_buf = b64decode(recv_buf)
		client.stop_handlers.add(lambda client: client._socket.close())

		for name in channels:
			channel = client.channel(name)
			channel._join() # note we don't send a JOIN
			message.Message(client, 'NAMES', name).send() # re-sync user list

		def handoff_start():
			client.started = True
			client.logger.debug("Starting using alternate handoff start")
			client._start_greenlets()
		client.start = handoff_start

		return client

	@classmethod
	def from_instance_handoff(cls, client):
		"""Takes a running client instance and creates a new Client instance without closing the connection."""
		client._prepare_for_handoff()
		new_client = cls._from_handoff(client._socket, **client._get_handoff_data())
		client._finalize_handoff()
		return new_client

	@classmethod
	def from_sock_handoff(cls, recv_sock, **init_args):
		"""Takes a unix socket connection and uses it to receive a connection handoff.
		Expects the remote process to send it a socket fd and handoff data - see client.handoff_to_sock()
		While most init args are provided by handoff data, others (eg. logger) can be passed in as extra kwargs.
		Note this method will block until the connection is closed.
		"""
		connection = None
		try:
			# receive fd from other process
			fd = recv_fd(recv_sock)
			connection = socket.fromfd(fd, socket.AF_INET, socket.SOCK_STREAM)

			# receive other args as json
			handoff_data = ''
			s = True
			while s: # loop until closed
				s = recv_sock.recv(4096)
				handoff_data += s
			handoff_data = json.loads(handoff_data)
			handoff_data = {k: v.encode('utf-8') if isinstance(v, unicode) else v
			                for k, v in handoff_data.items()}

			handoff_data.update(init_args)
			return cls._from_handoff(connection, **handoff_data)
		except Exception:
			if connection:
				connection.close()
			raise

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
					nick_msg.send(priority=0)
					# by waiting for messages, we force ourselves to wait until the Nick() has been processed
					self.wait_for_messages(priority=0)
				except Exception:
					self.quit("Unrecoverable error while changing nick", priority=-1)
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

	def increment_nick(self, nick):
		"""For a given nick, return "incremented" nick by following rules:
			If nick is of form /.*\|\d+/ (ie. ends in '|' then a number), add random digit to number
			Otherwise, append | and a random digit.
		This keeps nick length minimized while still performing well when 100s of clients are all
		following the same algorithm for one constested nick.
		"""
		parts = nick.split('|')
		if parts[-1] and not parts[-1].isdigit():
			parts.append('')
		parts[-1] += random.choice(string.digits)
		return '|'.join(parts)

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

	def _send(self, message, callback, priority):
		"""A low level interface to send a message. You normally want to use Message.send() instead.
		Callback is called after message is sent, and takes args (client, message).
		Callback may be None.
		"""
		self.logger.debug("Queuing message {} at prio {}".format(message, priority))
		if self._stopping:
			self.logger.debug("Dropping message as we are stopping")
			return
		self._send_queue.put((priority, (message, callback)))

	def _start_greenlets(self):
		"""Start standard greenlets that should always run, and put them in a dict indexed by their name,
		to allow special operations to refer to them specifically."""
		self._named_greenlets = {
			name: self._group.spawn(getattr(self, name))
			for name in ('_send_loop', '_recv_loop', '_idle_watchdog')
		}

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

		# registration is a delicate dance...
		with self._nick_lock, self._send_queue.limit_to(-1):
			# by limiting to 0, we block all messages except pongs and registration
			reg_done = gevent.event.Event()
			reg_handlers = set()

			@self.handler(command=replycodes.replies.WELCOME, sync=True)
			def reg_got_welcome(client, msg):
				reg_done.set()
				for handler in reg_handlers:
					handler.unregister(self)
			reg_handlers.add(reg_got_welcome)

			# Some anal servers require sending registration messages in a precise order
			# and/or can't handle PINGs being sent during registration. This makes the standard
			# nick-setting behaviour unsuitable. We're pretty sure we won't get a NICK
			# forced change from the server during registration, so we only need to special-case
			# handle a NICKNAMEINUSE message, and send the Nick() message manually.
			@self.handler(command=replycodes.errors.NICKNAMEINUSE, sync=True)
			def reg_nick_in_use(client, msg):
				self._nick = self.increment_nick(self._nick)
				message.Nick(self, self._nick).send(priority=-2)
			reg_handlers.add(reg_nick_in_use)

			if self.password:
				message.Message(self, 'PASS', self.password).send(priority=-2)
			message.Nick(self, self._nick).send(priority=-2)
			message.User(self, self.ident, self.real_name).send(priority=-2)

			self._start_greenlets()

			if not reg_done.wait(self.REGISTRATION_TIMEOUT):
				ex = Exception("Registration timeout")
				self.stop(ex)
				raise ex

			self.logger.debug("Registration complete")

	def _idle_watchdog(self):
		"""Sends a ping if no activity for PING_IDLE_TIME seconds.
		Disconnect if there is no response within PING_TIMEOUT seconds."""
		try:
			while True:
				if self._activity.wait(self.PING_IDLE_TIME):
					self._activity.clear()
					continue
				self.logger.info("No activity for {}s, sending PING".format(self.PING_IDLE_TIME))
				if not self.wait_for_messages(self.PING_TIMEOUT, priority=0):
					self.logger.error("No response to watchdog PING after {}s".format(self.PING_TIMEOUT))
					self.stop(ConnectionClosed())
					return
		except Exception as ex:
			self.logger.exception("error in _idle_watchdog")
			self.stop(ex)

	def _recv_loop(self):
		error = None
		try:
			while True:
				if self._kill_recv:
					return
				self._recv_waiting = True
				try:
					data = self._socket.recv(4096)
				except socket.error as ex:
					if ex.errno == errno.EINTR: # retry on EINTR
						continue
					raise
				finally:
					self._recv_waiting = False
				if not data:
					self.logger.info("no data from recv, socket closed")
					break
				lines = (self._recv_buf + data).split('\r\n')
				self._recv_buf = lines.pop() # everything after final \r\n
				if lines:
					self._activity.set()
				for line in lines:
					self._process(line)
		except Exception as ex:
			self.logger.exception("error in _recv_loop")
			error = ex
		if self._recv_buf:
			self.logger.warning("recv stream cut off mid-line, unused data: {!r}".format(self._recv_buf))
		self.stop(error or ConnectionClosed())

	def _send_loop(self):
		send_queue = self._send_queue
		try:
			while True:
				priority, (message, callback) = send_queue.get()
				line = "{}\r\n".format(message.encode())
				self.logger.debug("Sending message: {!r}".format(line))
				try:
					self._socket.sendall(line)
				except socket.error as ex:
					if ex.errno == errno.EPIPE:
						self.logger.info("failed to send, socket closed")
						self.stop(ConnectionClosed())
						return
					raise
				self._activity.set()
				if callback is not None:
					self._group.spawn(callback, self, message)
				if message.command == 'QUIT':
					self.logger.info("QUIT sent, client shutting down")
					self.stop()
					return
		except Exception as ex:
			self.logger.exception("error in _send_loop")
			self.stop(ex)

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
		def normalize(handler):
			# handler might be a Handler, BoundHandler or "sync"
			return handler.handler if isinstance(handler, BoundHandler) else handler
		# build dependency graph
		graph = {handler: set() for handler in self.message_handlers}
		graph['sync'] = set()
		for handler in self.message_handlers:
			for other in map(normalize, handler.after):
				if other in graph:
					graph[handler].add(other)
			for other in map(normalize, handler.before):
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
		if self._stopping:
			return self.wait_for_stop()
		self._stopping = True

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
				handler.unregister_for_client(self)
			# queues might contain some final messages
			self._send_queue = None
			self._recv_queue = None

			# act of setting _stopped will make wait_for_stop()s fire
			if ex:
				self._stopped.set_exception(ex)
			else:
				self._stopped.set(None)

		gevent.spawn(_stop).join()

	def msg(self, to, content, priority=16, block=False):
		"""Shortcut to send a Privmsg. See Message.send()"""
		message.Privmsg(self, to, content).send(priority=priority, block=block)

	def quit(self, msg=None, priority=16, block=True):
		"""Shortcut to send a Quit. See Message.send().
		Note that sending a quit automatically stops the client."""
		message.Quit(self, msg).send(priority=priority)
		if block:
			self.wait_for_stop()

	def channel(self, name):
		"""Fetch a channel object, or create it if it doesn't exist.
		Note that the channel is not joined automatically."""
		name = self.normalize_channel(name)
		if name not in self._channels:
			Channel(self, name) # this will register itself into _channels
		return self._channels[name]

	@property
	def joined_channels(self):
		"""Returns a list of channels we are currently joined to"""
		return set(channel for channel in self._channels.values() if channel.joined)

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

	def wait_for_messages(self, timeout=None, priority=16):
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

	@Handler(command=message.ISupport, sync=True)
	def recv_support(self, client, msg):
		self.server_properties.update(msg.properties)

	@Handler(command=message.Ping)
	def on_ping(self, client, msg):
		message.Pong(client, msg.payload).send(priority=-1)

	@Handler(command=replycodes.errors.NICKNAMEINUSE, sync=True)
	def nick_in_use(self, client, msg):
		bad_nick = msg.params[1]
		self.logger.debug("Nick {!r} in use (our nick: {!r} -> {!r})".format(bad_nick, self._nick, self._new_nick))
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
		# since the first part had to be done before sync but this part may block,
		# we spawn a seperate greenlet to finish later.
		@self._group.spawn
		def wait_and_increment():
			with self._nick_lock:
				# now that we've waited for any other operations to finish, let's double check
				# that we're still talking about the same nick
				if bad_nick != self._nick:
					return
				self.nick = self.increment_nick(self._nick)

	@Handler(command='NICK', sender=matches_nick, sync=True)
	def forced_nick_change(self, client, msg):
		if msg.sender == self._new_nick:
			# we are changing, and this was sent after our change was recieved so we must respect it.
			self._new_nick = msg.nickname
		elif msg.sender == self._nick:
			# either we aren't changing and everything is fine, or we are changing but this was
			# sent before the NICK command was processed by the server, so we change our old value
			# so further forced_nick_changes and matches_nick() still works.
			self._nick = msg.nickname

	@Handler(command='JOIN', sender=matches_nick, sync=True)
	def forced_join(self, client, msg):
		for name in msg.channels:
			channel = self.channel(name)
			channel._join()

	@Handler(command='PRIVMSG', ctcp=lambda v: v and v[0].upper() == 'VERSION')
	def ctcp_version(self, client, msg):
		if self.version:
			message.Notice(self, msg.sender, ('VERSION', self.version)).send()

	@Handler(command='PRIVMSG', ctcp=lambda v: v and v[0].upper() == 'TIME')
	def ctcp_time(self, client, msg):
		if self.time is 'utc':
			now = time.gmtime()
		elif self.time is 'local':
			now = time.localtime()
		else:
			return
		now = time.strftime('%s|%F %T', now)
		message.Notice(self, msg.sender, ('TIME', now)).send()

	@Handler(command='PRIVMSG', ctcp=lambda v: v and v[0].upper() == 'PING')
	def ctcp_ping(self, client, msg):
		cmd, arg = msg.ctcp
		message.Notice(self, msg.sender, ('PING', arg)).send()

	def _get_handoff_data(self):
		"""Collect all data needed for a connection handoff and return as dict.
		Make sure _prepare_for_handoff has been called first."""
		return dict(
			recv_buf = b64encode(self._recv_buf),
			channels = [channel.name for channel in self._channels.values() if channel.joined],
			hostname = self.hostname,
			nick = self._nick,
			port = self.port,
			password = self.password,
			ident = self.ident,
			real_name = self.real_name,
		)

	def _prepare_for_handoff(self):
		"""Stop operations and prepare for a connection handoff.
		Note that, among other things, this stops the client from responding to PINGs from the server,
		and so effectively begins a timeout until the server drops the connection."""
		# wait until we aren't changing nick, then permanently acquire the lock to prevent further changes
		# (note that forced_nick_change could still change it, but that's ok because we're stopping recv_loop)
		self._nick_lock.acquire()

		self._named_greenlets['_idle_watchdog'].kill(block=True)
		self._kill_recv = True # recv_loop will exit after processing current lines
		if self._recv_waiting:
			# recv_loop is stuck in a socket.recv call and should be bumped out
			self._named_greenlets['_recv_loop'].kill(socket.error(errno.EINTR, "recv_loop is being killed"), block=False)
		self._named_greenlets['_recv_loop'].get()

		# we are now no longer recving messages - we set a trap on _send(), then wait for send_queue to drain.
		# in practice, things should be unlikely to hit trap.
		def trap(*a, **k): raise Exception("Connection is being handed off, messages cannot be sent")
		self._send = trap
		# since we need to clear send queue, it makes no sense to try to hand off while it is limited
		if self._send_queue.get_limit() is not None:
			raise Exception("Can't hand off while send queue is limited")
		# We re-use the activity flag to check queue after each message is sent
		while True:
			self._activity.clear()
			if self._send_queue.empty():
				break
			self._activity.wait()

		# final state: recv loop is stopped, send loop is hung as no further messages can be queued and queue is empty

	def _finalize_handoff(self):
		"""Actually report stop once we have fully handed off."""
		self.stop()

	def handoff_to_sock(self, send_sock):
		"""Takes a unix socket and hands off connection to other process via it.
		Note that the receiving end will not complete until you close the connection."""
		self._prepare_for_handoff()
		handoff_data = json.dumps(self._get_handoff_data())

		send_fd(send_sock, self._socket)
		send_sock.sendall(handoff_data)

		self._finalize_handoff()
