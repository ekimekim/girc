

class classproperty(object):
	"""Acts like @property when used as a decorator, but wrapped function is also a classmethod
	For simplicity's sake, we only implement read-only property.
	"""
	def __init__(self, fn):
		self.fn = fn
	def __get__(self, instance, cls):
		return fn(cls)


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
