

from message import Join, Part, Privmsg
from replycodes import replies


class Channel(object):
	"""Object representing an IRC channel.
	This is the reccomended way to do operations like joins, or tracking user lists.

	A channel may be join()ed and part()ed multiple times.
	The user list will be the most recent info available, or None before first join.

	Can be used in a with statement to join then part.
	"""
	joined = False
	userlist = None

	def __init__(self, client, name):
		self.client = client
		self.name = name
		self.client.add_handler(self._recv_part, command=Part, channels=lambda value: self.name in value)

	def join(self, block=True):
		"""Join the channel if not already joined. If block=True, do not return until name list is received."""
		if self.joined: return
		self.joined = True
		self.userlist = UserList(self.client, self.name)
		self.client.send(Join(self.name))
		if not block: return
		self.client.wait_for(command=replies.ENDOFNAMES, params=[None, self.name, None])

	def part(self, block=True):
		"""Part from the channel if joined. If block=True, do not return until fully parted."""
		if not self.joined: return
		self.joined = False
		@gevent.spawn
		def _part():
			# we delay unregistering until the part is sent.
			self.client.send(Part(self.name), block=True)
			self.userlist.unregister()
		if block: _part.get()

	def msg(self, content, block=False):
		self.client.msg(self.name, content, block=block)

	def action(self, content, block=False):
		self.client.send(Privmsg.action(self.name, content), block=block)

	def _recv_part(self, client, msg):
		# we receive a forced PART from the server
		self.joined = False
		self.userlist.unregister()

	def __enter__(self):
		self.join()
	def __exit__(self, *exc_info):
		# if we're cleaning up after an exception, ignore errors in part()
		# as they are most likely a carry-on error or same root cause.
		try:
			self.part()
		except Exception:
			if exc_info == (None, None, None):
				raise
