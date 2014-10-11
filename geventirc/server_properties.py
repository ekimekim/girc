import re

from common import dotdict

class ServerProperties(dotdict):
	"""A dict containing server properties, but with some defaults and pre-processing."""

	defaults = dict(
		
	)

	def __getitem__(self, item):
		if item in self:
			return super(ServerProperties, self).__getitem__(item)
		return defaults[item]

	@property
	def prefixes(self):
		"""Returns a list of (mode, prefix) in order of most to least power, or [] if unknown."""
		if 'PREFIX' not in self:
		match = re.match(r'^\((.*)\)(.*)$', self.PREFIX)
		if not match:
			# TODO log warning
			return []
		modes, prefs = match.groups()
		if len(modes) != len(prefs):
			# TODO log warning
			return []
		return zip(modes, prefs)
