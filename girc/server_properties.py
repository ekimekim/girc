import re

from girc.common import dotdict

class ServerProperties(dotdict):
	"""A dict containing server properties, but with some defaults and pre-processing."""

	defaults = {
		'CHANTYPES': '#',
		'PREFIX': '(ov)@+',
		'CHANMODES': 'biklmnst',
		'CHANMODES': 'b,k,l,imnst',
	}

	def __getitem__(self, item):
		if item in self:
			return super(ServerProperties, self).__getitem__(item)
		return self.defaults[item]

	@property
	def prefixes(self):
		"""Returns a list of (mode, prefix) in order of most to least power."""
		match = re.match(r'^\((.*)\)(.*)$', self.PREFIX)
		if not match:
			raise ValueError("Invalid format for PREFIX: {!r}".format(self.PREFIX))
		modes, prefs = match.groups()
		if len(modes) != len(prefs):
			raise ValueError("PREFIX modes don't match prefixes: {!r}".format(self.PREFIX))
		return list(zip(modes, prefs))

	@property
	def channel_modes(self):
		"""Returns a dict {mode: mode type}. See mode_type() for details."""

		mode_lists = self.CHANMODES.split(',')
		if len(mode_lists) != 4:
			raise ValueError("Invalid format for CHANMODES: {!r}".format(self.CHANMODES))
		result = {}
		for mode_list, mode_type in zip(mode_lists, ['list', 'param-unset', 'param', 'noparam']):
			for mode in mode_list:
				result[mode] = mode_type

		# prefix modes are list modes
		for mode, prefix in self.prefixes:
			result[mode] = 'list'

		return result

	def mode_type(self, mode, user_modes=False):
		"""Returns the "type" of the given mode letter. If user_modes=True, mode is looked up as a user mode,
		otherwise it's a channel mode. The types are as follows:
			"list": Takes a parameter when adding or removing. Adds/removes don't set the value directly,
			        but edit a list of values. eg. +foo, +bar, +baz, -foo resolves to ["bar", "baz"]
			"param": Takes a parameter on set, but not unset. Parameters always replace the old value.
			"param-unset": As param, but still takes a parameter on unset, even though it is meaningless.
			"noparam": Does not take a param. Represents a boolean flag.
			None: Flag unknown. In this case you should generally default to noparam.
		"""
		if user_modes:
			# there's no CHANMODES equivilent for user modes
			return
		return self.channel_modes.get(mode, None)
