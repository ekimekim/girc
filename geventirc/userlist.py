
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
		self.parse_prefix_str()
		# TODO

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
		self.mode_map = dict(zip(chars, modes))
		self._user_map = {mode: set() for mode in self.modes}

	def _resolve_name(self, name):
		"""Returns mode given mode, prefix or friendly name, or None"""
		if name in self.modes:
			return name
		if name in self.mode_map:
			return self.mode_map[name]
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
