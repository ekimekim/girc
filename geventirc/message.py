from geventirc.common import classproperty


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

def encode(sender, user, host, command, params):
	"""Note: does not try to validate valid chars for command, params, etc"""
	parts = [command]
	if sender or user or host:
		prefix = ':{}'.format(sender or '')
		if user:
			prefix += '!{}'.format(user)
		if host:
			prefix += '@{}'.format(host)
		parts = [prefix] + parts
	if params:
		last_param = params.pop()
		parts += params + [':{}'.format(last_param)]
	return ' '.join(map(str, parts))


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

	def from_args(self, *args, **kwargs):
		"""Subclasses should provide this method, which should take the args you want users
		to pass into the constructor, and return a list of params.
		"""
		raise NotImplementedError

	@classproperty
	def command(cls):
		return cls.__name__.upper()


def nick(nickname, **kwargs):
	return Message('NICK', nickname, **kwargs)

def user(username, hostname, servername, realname, **kwargs):
	return Message('USER', username, hostname, servername, realname, **kwargs)

def quit(msg=None, **kwargs):
	params = () if msg is None else (msg,)
	return Message('QUIT', *params, **kwargs)

def join(*channels, **kwargs):
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
	params = (names, keys) if keys else (names,)
	return Message('JOIN', *params, **kwargs)

def part(*channels, **kwargs):
	channels = map(normalize_channel, channels)
	return Message('PART', ','.join(channels), **kwargs)

def mode(target, flags, arg=None, remove=False, **kwargs):
	"""Change mode flags for target (user or chan).
	flags should be a string or list of chars, eg. 'im' or 'o'
	The default is to add flags - change this with remove=True.
	arg is an optional extra arg required by some flags.
	eg. "#foo +o foo_guy" would be written as mode("#foo", "o", "foo_guy")
	while "foo_guy -o" would be written as mode("foo_guy", "o", remove=True)
	"""
	extra = () if arg is None else (arg,)
	flags = ('-' if remove else '+') + flags
	return Message('MODE', target, flags, *extra, **kwargs)

def privmsg(target, msg, **kwargs):
	"""Target can be user, channel or list of users
	NOTE: Because we can't distinguish between a nick and a channel name,
	      this function will NOT automatically prepend a '#' to channels.
	"""
	if not isinstance(target, basestring):
		target = ','.join(target)
	return Message('PRIVMSG', target, msg, **kwargs)

def list(*channels, **kwargs):
	channels = map(normalize_channels, channels)
	params = ','.join(channels) if channels else ()
	return Message('LIST', *params, **kwargs)

def kick(channel, nick, msg=None, **kwargs):
	channel = normalize_channel(channel)
	extra = () if msg is None else (msg,)
	return Message('KICK', channel, nick, *extra, **kwargs)

def whois(*nicks, **kwargs):
	"""Takes a server kwarg that I cannot expose explicitly due to python2 limitations"""
	nicks = ','.join(nicks)
	if not nicks:
		raise TypeError("No nicks given")
	server = kwargs.pop(server, None)
	params = (server, nicks) if server is not None else (nicks,)
	return Message('WHOIS', *params, **kwargs)

def ping(payload, **kwargs):
	return Message('PING', server, **kwargs)

def pong(payload, **kwargs):
	return Message('PONG', payload, **kwargs)


X_DELIM = '\x01'
X_QUOTE = '\x86'
M_QUOTE = '\x10'

_low_level_quote_table = {
    NUL: M_QUOTE + '0',
    NL: M_QUOTE + 'n',
    CR: M_QUOTE + 'r',
    M_QUOTE: M_QUOTE * 2
}

_ctcp_quote_table = {
    X_DELIM: X_QUOTE + 'a',
    X_QUOTE: X_QUOTE * 2
}

_low_level_dequote_table = {v: k for k, v in _low_level_quote_table.items()}
_ctcp_dequote_table = {v: k for k, v in _ctcp_quote_table.items()}

# TODO clean _quote and _dequote
def _quote(string, table):
    cursor = 0
    buf = ''
    for pos, char in enumerate(string):
        if pos is 0:
            continue
        if char in table:
            buf += string[cursor:pos] + table[char]
            cursor = pos + 1
    buf += string[cursor:]
    return buf

def _dequote(string, table):
    cursor = 0
    buf = ''
    last_char = ''
    for pos, char in enumerate(string):
        if pos is 0:
            last_char = char
            continue
        if last_char + char in table:
            buf += string[cursor:pos] + table[last_char + char]
            cursor = pos + 1
        last_char = char

    buf += string[cursor:]
    return buf

def low_level_quote(string):
    return _quote(string, _low_level_quote_table)

def low_level_dequote(string):
    return _dequote(string, _low_level_dequote_table)

def ctcp_quote(string):
    return _quote(string, _ctcp_quote_table)

def ctcp_dequote(string):
    return _dequote(string, _ctcp_dequote_table)


class CTCPMessage(Message):

    def __init__(self, command, params, ctcp_params, prefix=None):
        super(CTCPMessage, self).__init__(command, params, prefix=prefix)
        self.ctcp_params = ctcp_params

    @classmethod
    def decode(cls, data):
        prefix, command, params = irc_split(data)
        extended_messages = []
        normal_messages = []
        if params:
            params = DELIM.join(params)
            decoded = low_level_dequote(params)
            messages = decoded.split(X_DELIM)
            messages.reverse()

            odd = False
            extended_messages = []
            normal_messages = []

            while messages:
                message = messages.pop()
                if odd:
                    if message:
                        ctcp_decoded = ctcp_dequote(message)
                        split = ctcp_decoded.split(DELIM, 1)
                        tag = split[0]
                        data = None
                        if len(split) > 1:
                            data = split[1]
                        extended_messages.append((tag, data))
                else:
                    if message:
                        normal_messages += filter(None, message.split(DELIM))
                odd = not odd

        return cls(command, normal_messages, extended_messages, prefix=prefix)

    def encode(self):
        ctcp_buf = ''
        for tag, data in self.ctcp_params:
            if data:
                if not isinstance(data, basestring):
                    data = DELIM.join(map(str, data))
                m = tag + DELIM + data
            else:
                m = str(tag)
            ctcp_buf += X_DELIM + ctcp_quote(m) + X_DELIM

        return irc_unsplit(
                self.prefix, self.command, self.params + 
                [low_level_quote(ctcp_buf)]) + "\r\n"


class Me(CTCPMessage):
    def __init__(self, to, action, prefix=None):
        super(Me, self).__init__('PRIVMSG', [to], [('ACTION', action)], prefix=prefix)
