
KNOWN_NAMES = dict(
	owners = 'q',
	admins = 'a',
	ops = 'o',
	halfops = 'h',
	voiced = 'v',
	users = '',
)

class UserList(object):
	"""Tracks users and their privilige levels.

	Since privilige levels differ between servers, we canonically refer to them only by
	the mode letter (eg. 'v') given in server_properties.
	However, we also allow lookup by prefix char (eg. '+') or a friendly name if available
	(eg. "voiced").
	The base unpriviliged level is referred to as simply "users",
	and its mode letter and prefix char are the empty string.
	Note that (voiced, 'v', '+') and ('op', 'o', '@') are mandated by RFC.

	A set of usernames for a given level can be looked up by getitem (eg. userlist['o'] or userlist['@']).
	You can also look up a level by friendly name as an attribute, eg. userlist.ops
	This usage will return all users with this prefix OR ABOVE.
	For example, userlist['o'] will ALWAYS be a subset of userlist['v'],
	ie. all ops are also considered voiced.

	If you wish to be more specific, you can do operations like userlist.voiced - userlist.ops
	to get all voiced non-ops.
	However, several helper methods exist for your convenience, such as only() and below()
	(see individual docstrings).
	"""

	def __init__(self, client, channel):
		"""Takes a client object and string channel name. Begins watching for relevant messages immediately."""
		self.client = client
		self.channel = channel
		self.parse_prefix_str()
		self.client.add_handler(self.recv_user_list, command=replycodes.replies.NAMREPLY,
		                        params=lambda params: len(params) > 1 and params[1] == channel)
		self.client.add_handler(self.user_join, command='JOIN',
		                        channels=lambda channels: channel in channels)
		self.client.add_handler(self.user_leave, command='PART',
		                        channels=lambda channels: channel in channels)
		self.client.add_handler(self.user_leave, command='KICK', channel=channel)
		self.client.add_handler(self.user_leave, command='QUIT')
		self.client.add_handler(self.user_mode_change, command='MODE', target=channel)
		self.client.add_handler(self.user_nick_change, command='NICK')

	def parse_prefix_str(self):
		prefix_str = self.client.server_properties['PREFIX']
		match = re.match(r'^\(([a-z]+)\)(.*)$')
		if not match:
			raise Exception("Invalid PREFIX: {!r}".format(prefix_str))
		modes, chars = match.groups()
		if len(modes) != len(chars):
			raise Exception("PREFIX contained mismatched parts: {} modes, {} chars".format(
			                len(modes), len(chars)))

		# special "users" (nothing) mode
		modes.append('')
		chars.append('')

		self.modes = modes
		self.prefix_map = dict(zip(chars, modes)) # prefix_map maps prefix chars to modes
		self._user_map = {mode: set() for mode in self.modes} # user_map maps modes to users

	def _resolve_name(self, name):
		"""Returns mode given mode, prefix or friendly name, or None"""
		if name in self.modes:
			return name
		if name in self.prefix_map:
			return self.prefix_map[name]
		if name in KNOWN_NAMES:
			mode = KNOWN_NAMES[name]
			if mode in self.modes:
				return mode

	def __getattr__(self, attr):
		mode = self._resolve_name(attr)
		if mode is None:
			raise AttributeError(attr)
		return self[attr]

	def __getitem__(self, item):
		_item = self._resolve_name(item)
		if _item is None:
			raise KeyError(item)
		item = _item
		result = set()
		for mode in modes:
			result.update(self._user_map[mode])
			if mode == item: break
		return result

	def only(self, mode):
		"""Return only users that match this mode exactly (friendly names allowed)"""
		_mode = self._resolve_name(mode)
		if _mode is None:
			raise ValueError("Unknown mode: {}".format(mode))
		return self._user_map[_mode]

	def below(self, mode):
		"""Return all users that are less priviliged than this mode (friendly names allowed)"""
		return self.users - self[mode]

	def above(self, mode):
		"""Return all users that are more priviliged than this mode (friendly names allowed)"""
		return self[mode] - self.only(mode)

	def get_level(self, user):
		"""Return the mode of given user, or raise KeyError"""
		for mode, user_set in self._user_map.items():
			if user in user_set:
				return mode
		raise KeyError(user)

	def unregister(self):
		"""Stop watching for relevant messages, removing the handlers from the client."""
		# TODO

	# handlers

	def recv_user_list(self, msg):
		users = msg.params[2:]
		for raw_user in users:
			prefix, user = raw_user[0], raw_user[1:]
			if prefix not in self.mode_map:
				# no prefix, normal user
				prefix = ''
				user = raw_user
			mode = self.mode_map[prefix]
			self._user_map[mode].add(user)

	def user_join(self, msg):
		user = msg.sender
		# we might or might not already have this user under a certain mode
		# if not, we put them in users until they get MODEed
		if user not in self.users:
			self._user_map[''].add(user)

	def user_leave(self, msg):
		if isinstance(msg, Kick):
			user = msg.nick
		else:
			user = msg.sender
		for user_set in self._user_map.values():
			if user in user_set:
				user_set.remove(user)

	def user_mode_change(self, msg):
		user = msg.arg

		# get highest mode in self.modes
		for mode in self.modes:
			# don't count '' as a mode here
			if mode and mode in msg.flags:
				break
		else:
			return # no recognized modes

		user_set = self._user_map[mode]
		if msg.remove:
			if user in user_set:
				user_set.remove(user)
			# need to add back into basic users
			self._user_map[''].add(user)
			# XXX this could mean we've lost knowledge of a lesser mode
		else:
			user_set.add(user)
			# need to remove from any lesser modes
			for lesser_mode in self.modes[self.modes.index(mode):]:
				lesser_set = self._user_set[lesser_mode]
				if user in lesser_set:
					lesser_set.remove(user)

	def user_nick_change(self, msg):
		old_nick = msg.sender
		new_nick = msg.nickname
		for user_set in self._user_map.values():
			if old_nick in user_set:
				user_set.remove(old_nick)
				user_set.add(new_nick)
