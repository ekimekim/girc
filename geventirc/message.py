from common import classproperty, subclasses


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

	@property
	def code(self):
		"""If command is a numeric code, returns the string representation, otherwise None"""
		return replycodes.codes.get(self.command, None)

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
	@property
	def nickname(self):
		return self.params[0]

class User(Command):
	def from_args(self, username, hostname, servername, realname):
		return username, hostname, servername, realname
	@property
	def username(self):
		return self.params[0]
	@property
	def hostname(self):
		return self.params[1]
	@property
	def servername(self):
		return self.params[2]
	@property
	def realname(self):
		return self.params[3]

class Quit(Command):
	def from_args(self, msg=None):
		return () if msg is None else (msg,)
	@property
	def message(self):
		return params[0] if params else None

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
		channels = map(normalize_channel, channels)
		return ','.join(channels),
	@property
	def channels(self):
		return self.params[0].split(',')

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
		return (target, flags) if arg is None else (target, flags, arg)
	@property
	def target(self):
		return self.params[0]
	@property
	def flags(self):
		return self.params[1].lstrip('+-')
	@property
	def arg(self):
		return self.params[2] if len(self.params) > 2 else None
	@property
	def remove(self):
		return self.params[1][0] == '-'

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
	def target(self):
		return self.params[0].split(',')

	@property
	def message(self):
		return self.params[1]

	@property
	def ctcp(self):
		"""Returns (ctcp_command, ctcp_arg) or None"""
		if not (self.message.startswith('\x01') and self.message.endswith('\x01')):
			return
		return self.message.strip('\x01').split(' ', 1)

	@classmethod
	def me(cls, target, message):
		"""Helper constructor for /me messages"""
		return cls(target, ('ACTION', message))

class List(Command):
	def from_args(self, *channels):
		channels = map(normalize_channels, channels)
		if not channels: return
		return ','.join(channels),
	@property
	def channels(self):
		return self.params[0].split(',') if self.params else None

class Kick(Command):
	def from_args(self, channel, nick, msg=None):
		channel = normalize_channel(channel)
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
		server = kwargs.pop(server, None)
		if kwargs:
			raise TypeError("Unexpected kwargs: {}".format(kawrgs))
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
		return params[0]

class Pong(Command):
	def from_args(self, payload):
		return payload,
	@property
	def payload(self):
		return params[0]


def match(message, command=None, params=None, **attr_args):
	"""Return True if message is considered a match according to args:
		command: A command or list of commands the message must match, or None for any command.
		         Commands can be a string, int or Command subclass.
		sender, user, host: A sender, user or host value that message must match.
		params: A list of values, which must match each param of the message respectively.
		<other kwarg>: Any further kwargs are interpreted as attrs to lookup in the specific message
		               object (this only makes sense when command is also used), whose value must match.
	All args must match for a message to match.
	If not otherwise specified, the default for an arg is to match all.

	Value matches:
		Several times above, it is mentioned that a "value must match". The meaning of this
		depends on the type of the passed in match arg:
			string: Regex to match the whole message value
			callable: Function that takes a single arg (the message value) and returns True or False
			iterable: A list of the above, of which at least one must match.

	Examples:
		Match any message:
			match(message)
		Match a Privmsg that begins with "foobar":
			match(message, command=Privmsg, message="foobar.*")
		Match a Nick or Mode message from users "alice" or "bob":
			match(message, command=[Nick, Mode], sender=["alice", "bob"])
		Match a Mode message which gives a person Op status:
			match(message, command='mode', flags=lambda flags: 'o' in flags, remove=False)
	"""
	def match_value(match_spec, value):
		if isinstance(match_spec, basestring) or not iterable(match_spec):
			match_spec = [match_spec]
		for match_part in match_spec:
			if isinstance(match_part, basestring):
				match_part = re.compile("^({})$".format(match_part))
			if match_part(value):
				return True
		return False

	if command is not None:
		if isinstance(command, basestring) or not iterable(command):
			commands = [command]
		else:
			commands = command
		for command in commands:
			if issubclass(command, Command):
				command = command.command
			command = str(command).upper()
			if command != message.command:
				return False

	if params is not None:
		if len(params) != len(message.params):
			return False
		for match_spec, value in zip(params, message.params):
			if not match_value(match_spec, value):
				return False

	for attr, match_spec in attr_args.items():
		if not match_value(match_spec, getattr(message, attr)):
			return False

	return True
