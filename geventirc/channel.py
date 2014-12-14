
import gevent

from geventirc.message import Join, Part, Privmsg
from geventirc.replycodes import replies
from geventirc.userlist import UserList


class Channel(object):
	"""Object representing an IRC channel.
	This is the reccomended way to do operations like joins, or tracking user lists.

	A channel may be join()ed and part()ed multiple times.
	The user list will be the most recent info available, or None before first join.
	In particular, the user list can be considered up to date iff users_ready is set.

	Can be used in a with statement to join then part.
	"""

	USERS_READY_TIMEOUT = 10

	joined = False
	users_ready = gevent.event.Event()
	userlist = None

	def __init__(self, client, name):
		self.client = client
		self.name = name
		self.client.add_handler(self._recv_part, command=Part, channels=lambda value: self.name in value)
		self.client.add_handler(self._recv_end_of_names, command=replies.ENDOFNAMES, params=[None, self.name, None])

	def join(self, block=True):
		"""Join the channel if not already joined. If block=True, do not return until name list is received."""
		if self.joined: return
		self.joined = True
		self.users_ready.clear()
		self.userlist = UserList(self.client, self.name)
		self.client.send(Join(self.name))
		if not block: return
		self.users_ready.wait(self.USERS_READY_TIMEOUT)

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

	def _recv_end_of_names(self, client, msg):
		self.users_ready.set()

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
