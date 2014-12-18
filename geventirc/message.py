
import sys
import random
import re

import gevent

from geventirc import replycodes
from geventirc.common import classproperty, subclasses, iterable, int_equals


class InvalidMessage(Exception):
	def __init__(self, data, message):
		self.data = data
		self.message = message
		super(InvalidMessage, self).__init__(data, message)
	def __str__(self):
		return "Message {self.data!r} could not be parsed: {self.message}".format(self=self)


def decode(line, client):
	"""Decode a message. Client is needed as some messages require server properties to parse correctly."""
	sender = None
	user = None
	host = None

	line = line.strip('\r\n')
	for c in '\r\n\0':
		if c in line:
			raise InvalidMessage(line, "Illegal character {!r}".format(c))
	words = filter(None, line.split(' '))

	if words[0].startswith(':'):
		prefix = words.pop(0)
		prefix = prefix[1:] # strip leading :
		if '@' in prefix:
			prefix, host = prefix.split('@', 1)
		if '!' in prefix:
			prefix, user = prefix.split('!', 1)
		sender = prefix

	if not words:
		raise InvalidMessage(line, "no command given")
	command = words.pop(0).upper()

	params = []
	while words:
		if words[0].startswith(':'):
			params.append(' '.join(words)[1:])
			break
		params.append(words.pop(0))

	try:
		return Message(client, command, *params, sender=sender, user=user, host=host)
	except Exception:
		ex_type, ex, tb = sys.exc_info()
		new_ex = InvalidMessage(line, "{cls.__name__}: {ex}".format(cls=type(ex), ex=ex))
		raise type(new_ex), new_ex, tb


class MessageDispatchMeta(type):
	"""A metatype that overrides Message() so we can dispatch the construction out to the revelant Command."""
	# I originally tried to implement this with Message.__new__ but had problems with multiple calls to __init__

	def __call__(self, client, *args, **kwargs):
		if self is Message: # only Message is special
			return self.dispatch(client, *args, **kwargs)
		return super(MessageDispatchMeta, self).__call__(client, *args, **kwargs)

	def dispatch(self, client, command, *params, **kwargs):
		command = command.upper()
		for subcls in subclasses(Command):
			if str(subcls.command).upper() == command or int_equals(subcls.command, command):
				return subcls(client, params=params, **kwargs)
		# no matching command, default to generic Message()
		return super(MessageDispatchMeta, self).__call__(client, command, *params, **kwargs)


class Message(object):
	__metaclass__ = MessageDispatchMeta

	def __init__(self, client, command, *params, **kwargs):
		"""Takes optional kwargs sender, user, host and ctcp
		sender, user, host are the args that form the message prefix.
		Note that the constructor will automatically return the appropriate Message subclass
		if the command is recognised.
		"""
		# due to limitations of python2, we take generic kwargs and pull out our desired args manually
		self.client = client
		self.command = command
		self.params = params
		self.sender = kwargs.pop('sender', None)
		self.user = kwargs.pop('user', None)
		self.host = kwargs.pop('host', None)
		if kwargs:
			raise TypeError("Unexpected kwargs: {}".format(kwargs))

	def encode(self):
		parts = [self.command]
		if self.sender or self.user or self.host:
			prefix = ':{}'.format(self.sender or '')
			if self.user:
				prefix += '!{}'.format(self.user)
			if self.host:
				prefix += '@{}'.format(self.host)
			parts = [prefix] + parts
		if self.params:
			params = list(self.params)
			last_param = params.pop()
			parts += params + [':{}'.format(last_param)]
		return ' '.join(map(str, parts))

	def send(self, callback=None, block=False):
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
		self.client._send(self, callback)
		if block:
			event.wait()

	def __eq__(self, other):
		if not isinstance(other, Message):
			return False
		ATTRS = {'sender', 'user', 'host', 'command', 'params'}
		return all(getattr(self, attr) == getattr(other, attr) for attr in ATTRS)

	def __str__(self):
		return "<{cls.__name__} ({self.sender!r}, {self.user!r}, {self.host!r}) {args}>".format(
			self = self,
			cls = type(self),
			args = [self.command] + list(self.params)
		)

	@property
	def code(self):
		"""If command is a numeric code, returns the string representation, otherwise None"""
		return replycodes.codes.get(self.command, None)


class Command(Message):
	"""Helper subclass that known commands inherit from"""

	def __init__(self, client, *args, **kwargs):
		"""We allow params to be set via command-specific args (see from_args)
		or directly with params kwarg (this is mainly useful when decoding)"""
		EXTRACT = {'sender', 'user', 'host', 'params'}
		extracted = {}
		for key in EXTRACT:
			if key in kwargs:
				extracted[key] = kwargs.pop(key)
		if 'params' in extracted:
			params = extracted.pop('params')
			if args or kwargs:
				raise TypeError("Recieved params as well as unexpected args and kwargs: {}, {}".format(args, kwargs))
			super(Command, self).__init__(client, self.command, *params, **extracted)
		else:
			super(Command, self).__init__(client, self.command, **extracted)
			self.params = self.from_args(*args, **kwargs)

	def from_args(self, *args, **kwargs):
		"""Subclasses should provide this method, which should take the args you want users
		to pass into the constructor, and return a list of params.
		"""
		raise NotImplementedError

	@classproperty
	def command(cls):
		return cls.__name__.upper()


class Nick(Command):
	def from_args(self, nickname):
		return nickname,
	@property
	def nickname(self):
		return self.params[0]

class User(Command):
	def from_args(self, username, realname):
		# second and third params are unused, send 0
		return username, '0', '0', realname
	@property
	def username(self):
		return self.params[0]
	@property
	def realname(self):
		return self.params[3]

class Quit(Command):
	def from_args(self, msg=None):
		return () if msg is None else (msg,)
	@property
	def message(self):
		return self.params[0] if self.params else None

class Join(Command):
	def from_args(self, *channels):
		"""Channel specs can either be a name like "#foo" or a tuple of (name, key).
		Like most other functions here, if name does not start with "#" or "&",
		a "#" is automatically prepended."""
		nokeys = set()
		keys = {}
		for channel in channels:
			if isinstance(channel, basestring):
				nokeys.add(channel)
			else:
				name, key = channel
				keys[name] = key

		names, keys = zip(*keys.items()) if keys else [], []
		names += list(nokeys)
		names = map(self.client.normalize_channel, names)

		if not names:
			raise TypeError('No channels given')

		names = ','.join(names)
		keys = ','.join(keys)
		return (names, keys) if keys else (names,)
	@property
	def channels(self):
		return self.params[0].split(',')
	@property
	def keyed_channels(self):
		if len(self.params) == 1: return {}
		names = self.params[0].split(',')
		keys = self.params[1].split(',')
		return dict(zip(names, keys))

class Part(Command):
	def from_args(self, *channels):
		channels = map(self.client.normalize_channel, channels)
		return ','.join(channels),
	@property
	def channels(self):
		return self.params[0].split(',')

class Mode(Command):
	def from_args(self, target, *modes):
		"""Change mode flags for target (user or chan).
		Each mode should be in one of these forms:
			flag: A simple string flag. Sets that flag.
			-flag: Unsets the flag.
			(flag, arg): Sets flag with arg
			(-flag, arg): Unsets flag with arg
			(flag, arg, adding): Sets or unsets flag with arg depending on if adding is True
		Example:
			>>> m = Mode(client, '#foobar', '-s', 'n', ('b', 'someguy'))
			>>> m.params
			['#foobar', '-s+nb', 'someguy']
		"""
		currently_adding = True
		args = []
		modestr = ''
		for modespec in modes:
			if isinstance(modespec, basestring):
				modespec = modespec, None
			if len(modespec) == 2:
				mode, arg = modespec
				if mode.startswith('-'):
					mode = mode[1:]
					adding = False
				else:
					adding = True
			else:
				mode, arg, adding = modespec
			if currently_adding != adding:
				modestr += '+' if adding else '-'
				currently_adding = adding
			modestr += mode
			if arg is not None:
				args.append(arg)
		params = target, modestr
		if args:
			params += args,
		return params

	@property
	def target(self):
		return self.params[0]

	@property
	def modes(self):
		"""List of (mode, arg, adding) for modes being changed. arg is None if no arg."""
		target, modestr = self.params[:2]
		args = list(self.params[2:])
		result = []
		adding = True

		for c in modestr:
			param = None
			if c in '+-':
				adding = (c == '+')
				continue
			mode_type = self.client.server_properties.mode_type(c)
			if mode_type in ("list", "param-unset") or (adding and mode_type == "param"):
				if not args:
					message = "Not enough params: Could not get param for {}{}".format(('+' if adding else '-'), c)
					raise InvalidMessage(self.encode(), message)
				param = args.pop(0)
			# note we're not differentiating between "mode doesn't have param" and "mode unknown"
			result.append((c, param, adding))

		return result


class Privmsg(Command):
	def from_args(self, target, msg):
		"""Target can be user, channel or list of users
		msg can alternately be a tuple (ctcp_command, ctcp_arg)
		"""
		if not isinstance(target, basestring):
			target = ','.join(target)
		if not isinstance(msg, basestring):
			msg = '\x01{} {}\x01'.format(*msg)
		return target, msg

	@property
	def targets(self):
		return self.params[0].split(',')

	@property
	def payload(self):
		return self.params[1]

	@property
	def ctcp(self):
		"""Returns (ctcp_command, ctcp_arg) or None"""
		if not (self.payload.startswith('\x01') and self.payload.endswith('\x01')):
			return
		return self.payload.strip('\x01').split(' ', 1)

	@classmethod
	def action(cls, target, message):
		"""Helper constructor for action messages"""
		return cls(target, ('ACTION', message))

class List(Command):
	def from_args(self, *channels):
		channels = map(self.client.normalize_channel, channels)
		if not channels: return
		return ','.join(channels),
	@property
	def channels(self):
		return self.params[0].split(',') if self.params else None

class Kick(Command):
	def from_args(self, channel, nick, msg=None):
		channel = self.client.normalize_channel(channel)
		return (channel, nick) if msg is None else (channel, nick, msg)
	@property
	def channel(self):
		return self.params[0]
	@property
	def nick(self):
		return self.params[1]
	@property
	def message(self):
		return self.params[2] if len(self.params) > 2 else None

class Whois(Command):
	def from_args(self, *nicks, **kwargs):
		"""Takes a server kwarg that I cannot expose explicitly due to python2 limitations"""
		nicks = ','.join(nicks)
		if not nicks:
			raise TypeError("No nicks given")
		server = kwargs.pop('server', None)
		if kwargs:
			raise TypeError("Unexpected kwargs: {}".format(kwargs))
		return (nicks,) if server is None else (server, nicks)
	@property
	def nicks(self):
		return self.params[-1].split(',')
	@property
	def server(self):
		return self.params[0] if len(self.params) > 1 else None

class Ping(Command):
	def from_args(self, payload=None):
		if payload is None:
			payload = str(random.randrange(1, 2**31)) # range here is kinda arbitrary, this seems safe
		return payload,
	@property
	def payload(self):
		return self.params[0]

class Pong(Command):
	def from_args(self, payload):
		return payload,
	@property
	def payload(self):
		return self.params[0]

class ISupport(Command):
	command = replycodes.replies.ISUPPORT
	def from_args(self, properties):
		# i'm lazy, and if you want this you're doing something weird
		raise NotImplementedError("Client-side construction of ISUPPORT message is not supported")
	@property
	def properties(self):
		"""Returns server properties described by message as a dict.
		Keys that are set without a value are given the value True.
		Keys that are unset are given the value None.
		NOTE: You generally do not want to handle this message directly,
		instead use client.server_properties, which aggregates and provides helpers."""
		args = self.params[1:-1] # first arg is nick, last arg is "are supported by this server"
		# ISUPPORT args can have the following forms:
		# -KEY[=VALUE] : unset KEY
		# KEY[=] : set KEY true
		# KEY=VALUE : set KEY to value
		result = {}
		for arg in args:
			if '=' in arg:
				key, value = arg.split('=', 1)
			else:
				key, value = arg, ''
			if not value:
				value = True
			if key.startswith('-'):
				key = key[1:]
				value = None
			result[key] = value
		return result


regex_type = type(re.compile(''))
def match(message, command=None, params=None, **attr_args):
	"""Return True if message is considered a match according to args:
		command: A command or list of commands the message must match, or None for any command.
		         Commands can be a string, int or Command subclass.
		sender, user, host: A sender, user or host value that message must match.
		params: A list of values, which must match each param of the message respectively.
		        May instead be a callable which takes the params list and returns True/False.
		<other kwarg>: Any further kwargs are interpreted as attrs to lookup in the specific message
		               object (this only makes sense when command is also used), whose value must match.
	All args must match for a message to match.
	If not otherwise specified, the default for an arg is to match all.

	Value matches:
		Several times above, it is mentioned that a "value must match". The meaning of this
		depends on the type of the passed in match arg:
			string: Exact string match on the message value
			regex object (as returned by re.compile()): re.match() on message value
			callable: Function that takes a single arg (the message value) and returns True or False
			iterable: A list of the above, of which at least one must match.
			None: Match anything (useful with the params arg)

	Examples:
		Match any message:
			match(message)
		Match a Privmsg that begins with "foobar":
			match(message, command=Privmsg, message=re.compile("foobar.*"))
		Match a Nick or Mode message from users "alice" or "bob":
			match(message, command=[Nick, Mode], sender=["alice", "bob"])
		Match a Mode message which gives a person Op status:
			match(message, command='mode', modes=lambda modes: any(mode == 'o' and adding for mode, arg, adding in modes), remove=False)
		Match a RPL_TOPIC (332) where param 2 of 3 is "#mychan":
			match(message, command=332, params=[None, "#mychan", None])
		Match a RPL_NAMREPLY (353) where param 1 of any is "#mychan":
			match(message, command=353, params=lambda params: len(params) > 1 and params[1] == "#mychan")
	"""
	def match_value(match_spec, value):
		if match_spec is None:
			return True
		if isinstance(match_spec, basestring) or not iterable(match_spec):
			match_spec = [match_spec]
		for match_part in match_spec:
			if isinstance(match_part, regex_type):
				match_part = match_part.match
			if isinstance(match_part, basestring):
				match_value = match_part
				match_part = lambda v: match_value == v
			try:
				if match_part(value):
					return True
			except Exception:
				pass # a failed callable means False
		return False

	if command is not None:
		if isinstance(command, basestring) or not iterable(command):
			commands = [command]
		else:
			commands = command

		for command in commands:
			# is command a Command subclass?
			if isinstance(command, type) and issubclass(command, Command):
				command = command.command
			# is command an int?
			if int_equals(command, message.command):
				continue # it's a match
			# finally, check str
			command = str(command).upper()
			if command != message.command:
				return False

	if params is not None:
		if callable(params):
			if not params(message.params):
				return False
		else:
			if len(params) != len(message.params):
				return False
			for match_spec, value in zip(params, message.params):
				if not match_value(match_spec, value):
					return False

	for attr, match_spec in attr_args.items():
		if not match_value(match_spec, getattr(message, attr)):
			return False

	return True
