from geventirc.common import classproperty, subclasses


class InvalidMessage(Exception):
    def __init__(self, data, message):
		self.data = data
		self.message = message
		super(InvalidMessage, self).__init__(data, message)
	def __str__(self):
		return "Message {self.data!r} could not be parsed: {self.message}".format(self=self)


def normalize_channel(name):
	if name.startswith('#') or name.startswith('&'):
		return name
	return '#{}'.format(name)


def decode(line):
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

	return cls(command, *params, sender=sender, user=user, host=host)


class Message(object):

	def __new__(cls, command, *params, **kwargs):
		for subcls in subclasses(Command):
			if subcls.command == command:
				return subcls(params=params, **kwargs)
		return object.__new__(command, *params, **kwargs)

    def __init__(self, command, *params, **kwargs):
		"""Takes optional kwargs sender, user, host and ctcp
		sender, user, host are the args that form the message prefix.
		Note that the constructor will automatically return the appropriate Message subclass
		if the command is recognised.
		"""
		# due to limitations of python2, we take generic kwargs and pull out our desired args manually
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

class Command(Message):
	"""Helper subclass that known commands inherit from"""
	__new__ = object.__new__

	def __init__(self, *args, **kwargs):
		"""We allow params to be set via command-specific args (see from_args)
		or directly with params kwarg (this is mainly useful when decoding)"""
		EXTRACT = {'sender', 'user', 'host', 'params'}
		extracted = {}
		for key in EXTRACT:
			if key in kwargs:
				extracted[key] = kwargs.pop(key)
		if 'params' in extracted:
			params = extracted.pop(params)
			if args or kwargs:
				raise TypeError("Recieved params as well as unexpected args and kwargs: {}, {}".format(args, kwargs))
		else:
			params = self.from_args(*args, **kwargs)
		super(Command, self).__init__(self.command, *params, **extracted)

	def from_args(self, self, *args, **kwargs):
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

class User(Command):
	def from_args(self, username, hostname, servername, realname):
		return username, hostname, servername, realname

class Quit(Command):
	def from_args(self, msg=None):
		return () if msg is None else (msg,)

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

		names, keys = zip(*keys.items())
		names += list(nokeys)
		names = map(normalize_channel, names)

		if not names:
			raise TypeError('No channels given')

		names = ','.join(names)
		keys = ','.join(keys)
		return (names, keys) if keys else (names,)

class Part(Command):
	def from_args(self, *channels):
		channels = map(normalize_channel, channels)
		return ','.join(channels),

class Mode(Command):
	def from_args(self, target, flags, arg=None, remove=False):
		"""Change mode flags for target (user or chan).
		flags should be a string or list of chars, eg. 'im' or 'o'
		The default is to add flags - change this with remove=True.
		arg is an optional extra arg required by some flags.
		eg. "#foo +o foo_guy" would be written as mode("#foo", "o", "foo_guy")
		while "foo_guy -o" would be written as mode("foo_guy", "o", remove=True)
		"""
		flags = ('-' if remove else '+') + flags
		return (target, flags) if arg is None else (target, flags, extra)

class Privmsg(Command):
	def from_args(self, target, msg):
		"""Target can be user, channel or list of users
		NOTE: Because we can't distinguish between a nick and a channel name,
			  this function will NOT automatically prepend a '#' to channels.
		msg can alternately be a tuple (ctcp_command, ctcp_arg)
		"""
		if not isinstance(target, basestring):
			target = ','.join(target)
		if not isinstance(msg, basestring):
			msg = '\x01{} {}\x01'.format(*msg)
		return target, msg

	@property
	def ctcp(self):
		"""Returns (ctcp_command, ctcp_arg) or None"""
		if not (self.msg.startswith('\x01') and self.msg.endswith('\x01')):
			return
		return self.msg.strip('\x01').split(' ', 1)

	@classmethod
	def me(cls, target, message):
		"""Helper constructor for /me messages"""
		return cls(target, ('ACTION', message))

class List(Command):
	def from_args(self, *channels):
		channels = map(normalize_channels, channels)
		if not channels: return
		return ','.join(channels),

class Kick(Command):
	def from_args(self, channel, nick, msg=None):
		channel = normalize_channel(channel)
		return (channel, nick) if msg is None else (channel, nick, msg)

class Whois(Command):
	def from_args(self, *nicks, **kwargs):
		"""Takes a server kwarg that I cannot expose explicitly due to python2 limitations"""
		nicks = ','.join(nicks)
		if not nicks:
			raise TypeError("No nicks given")
		server = kwargs.pop(server, None)
		if kwargs:
			raise TypeError("Unexpected kwargs: {}".format(kawrgs))
		return (nicks,) if server is None else (server, nicks)

class Ping(Command):
	def from_args(self, payload=None):
		if payload is None:
			payload = str(random.randrange(1, 2**31)) # range here is kinda arbitrary, this seems safe
		return payload,

class Pong(Command):
	def from_args(self, payload):
		return payload,

