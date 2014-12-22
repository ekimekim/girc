


class UserDispatchMeta(type):
	"""Metaclass so that multiple Users with the same client and nick (and are active)
	instead return the same instance."""
	def __call__(self, client, nick):
		if nick not in client._users or not client._users[nick].active:
			return super(UserDispatchMeta, self).__call_(client, nick)
		return client._users[nick]


class User(object):
	__metaclass__ = UserDispatchMeta

	ident = None
	realname = None
	host = None

	server = None
	secure = False
	operator = False

	account = None
	metadata = {}

	active = True # whether this User object is up to date, ie. we're confident the nick is correct.

	def __init__(self, client, nick):
		self.client = client
		self.nick = nick
		self.client._users[nick] = self

	@property
	def channels(self):
		"""A map {channel: highest status mode} for channels the user is in"""
		results = {}
		for channel in self.client._channels.values():
			try:
				mode = channel.users.get_level(self.nick)
			except KeyError:
				pass
			else:
				results[channel] = mode
		return results


def register_handlers(client):
	"""Register handlers for given client to capture user info"""

	def command(name):
		"""Shortcut for client.add_handler(command=name)"""
		return client.add_handler(command=name)

	def with_user(fn):
		"""Helper decorator for handlers, takes first arg of msg and gets User() for that nick.
		Calls wrapped function with args fn(user, *params[1:])"""
		@functools.wraps(fn)
		def _with_user(client, msg):
			user = User(client, msg.params[0])
			return fn(user, *params[1:])
		return _with_user

	@command(replies.WHOISUSER)
	@with_user
	def whois_user(user, ident, host, realname):
		user.ident = ident
		user.host = host
		user.realname = realname

	@command(replies.WHOISSERVER)
	@with_user
	def whois_server(user, server, server_info):
		user.server = server

	@command(replies.WHOISOPERATOR)
	@with_user
	def whois_oper(user, *junk):
		user.operator = True

	@command(replies.WHOISCHANNELS)
	@with_user
	def whois_channels(user, *channels):
		# channels is normally one space-seperated arg, but might be seperate args - normalize
		channels = (' '.join(channels)).split(' ')
		all_prefixes = [prefix for mode, prefix in client.server_properties.prefixes]
		for name in channels:
			prefixes = ''
			while any(name.startswith(prefix) for prefix in all_prefixes if prefix):
				prefixes += name[0]
				name = name[1:]
			client.channel(name).users.recv_user_prefix(prefixes + user.nick)

	@command(replies.WHOISSECURE)
	@with_user
	def whois_secure(user, *junk):
		user.secure = True

	@command(replies.WHOISACCOUNT)
	@with_user
	def whois_account(user, account, *junk):
		user.account = account

	@command(replies.WHOISKEYVALUE)
	@with_user
	def whois_metadata(user, key, value):
		category, key = key.split('.', 1)
		user.setdefault('category', {})[key] = value

"""
info on whois responses
not all servers send all responses

311 WHOISUSER nick user host "*" realname
312 WHOISSERVER nick server server_description
313 WHOISOPERATOR nick ??
317 WHOISIDLE nick ... # DO NOT USE: ambiguous between server impls
318 ENDOFWHOIS nick # no more whois replies
319 WHOISCHANNELS nick (all one arg: space-seperated channels with optional prefix indicating user's rank in channel)
330 WHOISACCOUNT nick account _
671 WHOISSECURE nick (security type) [_]
760 WHOISKEYVALUE nick metadata_category.key value
"""
