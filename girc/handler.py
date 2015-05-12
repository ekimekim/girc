
from girc import message
from girc.common import iterable


class Handler(object):
	"""A handler object manages a handler callback.
	It wraps the callback with additional metadata governing matches.
	It is normally registered to a client automatically at init time, or you can manually register it later
	with handler.register(client).

	You can set before and after kwargs with other handlers (or lists of handlers) to control the order in which
	handlers are executed.
	You can also set sync=True to specify that the client should not process the next message until this handler
	has finished.
	(In truth, sync=True is just a shortcut for before='sync'.
	 after='sync' is also valid, though likely not useful.)

	To support its use as a decorator for methods of a class, it will bind to a class instance on __get__
	just like a function object. In addition, if you do instance.handler.register(client), then any callbacks
	from that client will be associated with that instance.

	So for example, this will work how you expect:
		class Foo(object):
			@Handler(**match_args)
			def callback(self, client, msg):
				...
			def __init__(self, client):
				self.callback.register(client)
	but so will this:
		@Handler(**match_args)
		def callback(client, msg):
			...
		def set_client(client):
			callback.register(client)
	"""

	def __init__(self, client=None, callback=None, before=[], after=[], sync=False, **match_args):
		"""Register a handler for client to call when a message matches.
		Match args are as per message.match()
		Callback should take args (client, message) and may return True to de-register itself.
		If callback object is already a Handler, the new init data is merged into the old handler.
		Callback may be omitted, in which case the first time Handler() is called it will act as a decorator,
		binding to the given argument.
		Client may be omitted, in which case the handler must later be bound to a client with handler.register().
		"""
		self.match_list = [] # list of match_args dicts to match on
		self.client_binds = {} # maps {client: set(instances to bind and call)}

		self.before = set(before) if iterable(before) else [before]
		self.after = set(after) if iterable(after) else [after]
		if sync:
			self.before.add('sync')

		self.add_match(**match_args)
		self.set_callback(callback)
		if client:
			self.register(client)

	def __repr__(self):
		return "<{cls.__name__}({self.callback})>".format(cls=type(self), self=self)

	def add_match(self, **match_args):
		"""Add a new set of match_args to the handler. Either this new set or the existing set matching will
		trigger the handler."""
		self.match_list.append(match_args)

	def register(self, client, instance=None):
		"""Register handler with a client. Optional arg instance is for internal use, and is used to implement
		the "call with callback bound to instance" functionality as described in the class docstring."""
		self.client_binds.setdefault(client, set()).add(instance)
		client.message_handlers.add(self)
		client.logger.info("Registering handler {} with match args {}".format(self, self.match_list))

	def unregister(self, client, instance=None):
		"""Remove association of handler with client."""
		# note: if instance given, we only want to disassociate that instance with that client,
		# unless it is the last bind for that client.
		if client not in self.client_binds:
			return
		self.client_binds[client].discard(instance)
		if not self.client_binds[client]:
			client.message_handlers.discard(self)
			del self.client_binds[client]

	@classmethod
	def find_handlers(cls, instance):
		"""Returns a set of BoundHandlers for Handlers that are methods for given instance."""
		result = set()
		for attr in dir(instance):
			value = getattr(instance, attr)
			if isinstance(value, BoundHandler):
				result.add(value)
		return result

	@classmethod
	def register_all(cls, client, instance):
		"""Find methods of the given instance that are Handlers, and register them to client."""
		for handler in cls.find_handlers(instance):
			handler.register(client)

	@classmethod
	def unregister_all(cls, client, instance):
		"""As register_all(), unregisters any handlers for instance if registered"""
		for handler in cls.find_handlers(instance):
			handler.unregister(client)

	def unregister_for_client(self, client):
		"""You probably don't want this. It removes all registrations with client, not just for one instance.
		It is mainly intended for a client to call when it is stopping."""
		self.client_binds.pop(client, None)

	def set_callback(self, callback):
		self.callback = callback

	def __call__(self, *args, **kwargs):
		"""If callback not set, set callback and return self (for decorator use). Else, call callback normally."""
		if self.callback:
			return self.callback(*args, **kwargs)
		self.set_callback(*args, **kwargs)
		return self

	def __get__(self, instance, cls):
		if instance is None:
			return self
		return BoundHandler(self, instance)

	def _handle(self, client, msg, instance=None):
		"""subclasses can hook here to customise how the callback is called without changing the behaviour
		of a naive __call__."""
		return self(instance, client, msg) if instance else self(client, msg)

	def handle(self, client, msg):
		try:
			if not any(message.match(msg, **match_args) for match_args in self.match_list):
				return
		except message.InvalidMessage:
			client.logger.warning("Problem with message {} while matching handler {}".format(msg, self),
			                      exc_info=True)
			return
		if not self.callback:
			return
		client.logger.debug("Handling message {} with handler {}".format(msg, self))
		for instance in self.client_binds.get(client, set()).copy():
			try:
				ret = self._handle(client, msg, instance=instance)
			except Exception:
				client.logger.exception("Handler {} failed{}".format(
				                        self, (' for instance {}'.format(instance) if instance else '')))
			else:
				if ret:
					self.unregister(client, instance)


class BoundHandler(object):
	"""A wrapper around a handler that applies the bound instance to certain operations."""
	def __init__(self, handler, instance):
		self.handler = handler
		self.instance = instance

	def register(self, client):
		return self.handler.register(client, self.instance)

	def unregister(self, client):
		return self.handler.unregister(client, self.instance)

	def __call__(self, *args, **kwargs):
		if not self.handler.callback:
			raise ValueError("Cannot set callback from BoundHandler")
		return self.handler(self.instance, *args, **kwargs)
