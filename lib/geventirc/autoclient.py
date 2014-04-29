
from geventirc import client, message, replycode, handlers

USER_MODES = 'USER', 'VOICED', 'OP', 'ADMIN' # all admins are ops, all ops are voiced, etc.
USER, VOICED, OP, ADMIN = USER_MODES
USER_LIST_CHARS = {
	USER: '',
	VOICED: '+',
	OP: '~',
	ADMIN: '&',
}
USER_MODE_CHARS = {
	VOICED: 'v',
	OP: 'o',
	ADMIN: 'a'
}
new_lists_dict = lambda: {k: [] for k in USER_MODES}


class AutoClient(client.Client):
	"""A standard client with some preset handlers to automatically do common tasks:
		* Keeps better track of its nick
		* Auto-joins given channels
		* Maintains lists of users in channel
		* Automatically responds to PINGs

	Nick is available as self.nick
	self.user_lists is a dict {channel: {user_type: [users]}}
		where user_types are {USER, VOICED, OP, ADMIN}
	"""

	def __init__(self, *args, **kwargs):
		channels = kwargs.pop('channels', [])
		super(AutoClient, self).__init__(*args, **kwargs)

		# ping handler
		self.add_handler(handlers.ping_handler, 'PING')

		# auto-join channels
		for channel in channels:
			self.add_handler(handlers.JoinHandler(channel))

		# nick management
		@self.handler('001')
		def do_auth(self, msg):
			self.send_message(message.Nick(self.nick))
			self._authenticate()
		@self.handler(replycode.ERR_NICKNAMEINUSE, replycode.ERR_NICKCOLLISION)
		def nick_in_use(self, msg):
			nick = msg.params[1]
			if nick != self.nick: return # stale message? ignore.
			self.set_nick(nick + '_')
		@self.handler('NICK')
		def forced_nick_change(self, msg):
			target, nick = msg.params
			if target != self.nick: return # someone else changed nick, we don't care
			self.nick = nick

		# user list management
		self.user_lists = {}
		@self.handler('353')
		def recv_user_list(self, msg):
			channel = msg.params[1]
			users = msg.params[2:]
			lists = new_lists_dict()
			self.user_lists[channel] = lists
			for user in users:
				top_mode = [mode for mode in USER_MODES
				            if user.startswith(USER_LIST_CHARS[mode])][0]
				top_mode_index = USER_MODES.index(top_mode)
				user = user.lstrip(''.join(USER_LIST_CHARS.values()))
				for mode in USER_MODES[:top_mode_index+1]:
					lists[mode].append(user)
		@self.handler('JOIN')
		def user_joined(self, msg):
			user = msg.sender
			channel, = msg.params
			lists = self.user_lists.setdefault(channel, new_lists_dict())
			lists[USER].append(user)
		@self.handler('PART', 'QUIT')
		def user_left(self, msg):
			user = msg.sender
			channel, = msg.params
			lists = self.user_lists.setdefault(channel, new_lists_dict())
			for user_list in lists:
				if user in user_list:
					user_list.remove(user)
		@self.handler('MODE')
		def user_changed_mode(self, msg):
			channel, = msg.params
			flags, user = msg.params[1:3]
			if not flags.startswith('+'):
				raise NotImplementedError
			flags = flags.lstrip('+')
			lists = self.user_lists.setdefault(channel, new_lists_dict())
			for mode in USER_MODES:
				if USER_MODE_CHARS[mode] in flags:
					lists[mode].append(user)
		@self.handler('NICK')
		def user_changed_name(self, msg):
			old_nick = msg.sender
			new_nick = msg.params[0]
			lists = self.user_lists.setdefault(channel, new_lists_dict())
			for user_list in lists:
				if old_nick in user_list:
					user_list.remove(old_nick)
					user_list.append(new_nick)

	def set_nick(self, nick):
		self.send_message(message.Nick(nick))
		self.nick = nick

	def _authenticate(self):
		"""Override this if the irc server's auth mechanism differs"""
		from getpass import getpass
		password = getpass()
		self.msg('nickserv', 'identify %s' % password)
