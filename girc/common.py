
import multiprocessing.reduction
from collections import defaultdict

from gevent.select import select
from gevent.event import AsyncResult


class classproperty(object):
	"""Acts like @property when used as a decorator, but wrapped function is also a classmethod
	For simplicity's sake, we only implement read-only property.
	"""
	def __init__(self, fn):
		self.fn = fn
	def __get__(self, instance, cls):
		return self.fn(cls)


def async_property():
	"""Returns a pair of property objects.
	Used to define a property where any get operation blocks
	until the first set operation has been made.
	This is the first property of the pair.

	The second property returns the AsyncResult object associated with the instance,
	so special actions can be performed, such as calling result.ready() or result.set_exception().
	"""
	results = defaultdict(AsyncResult)
	def get_result(instance):
		return results[instance]
	def get(instance):
		return get_result(instance).get()
	def set(instance, value):
		get_result(instance).set(value)
	return property(get, set), property(get_result)


def subclasses(cls):
	"""Return all subclasses of cls, including subclasses of subclasses of cls, etc."""
	subs = set()
	for subcls in cls.__subclasses__():
		subs.add(subcls)
		subs |= subclasses(subcls)
	return subs


def iterable(obj):
	"""Return boolean of whether obj is iterable"""
	try:
		iter(obj)
	except TypeError:
		return False
	return True


class dotdict(dict):
	def __getattr__(self, attr):
		try:
			return self[attr]
		except KeyError:
			raise AttributeError(attr)
	def __setattr__(self, attr, value):
		self[attr] = value
	def __hasattr__(self, attr):
		return attr in self


def int_equals(a, b):
	"""Small helper function, takes two objects and returns True if they are equal when cast to int.
	This is mainly intended to facilitate checking two strings that may or may not be ints.
	Most importantly, it will return False if either cannot be cast to int."""
	try:
		return int(a) == int(b)
	except ValueError:
		return False


def send_fd(sock, fd):
	"""Send an fd over a unix socket"""
	if hasattr(fd, 'fileno'):
		fd = fd.fileno()
	while True:
		r, w, x = select([], [sock], [])
		if not w:
			continue
		multiprocessing.reduction.send_handle(sock, fd, None)
		break


def recv_fd(sock):
	"""Receive an fd from a unix socket.
	Note this function returns a raw integer fd. You probably want to os.fdopen() or socket.fromfd() it.
	"""
	while True:
		r, w, x = select([sock], [], [])
		if not r:
			continue
		return multiprocessing.reduction.recv_handle(sock)
