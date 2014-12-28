
import sys
import logging

import gevent
import gtools

from girc.client import Client
from girc import message

host = sys.argv[1]
channel = sys.argv[2]
nick1, nick2, nick3 = sys.argv[3:5]

logging.basicConfig(level=logging.DEBUG)

gtools.backdoor()

client = Client(host, nick1)
client.start()
gevent.sleep(0.5)
message.Join(client, channel).send()

client.nick = nick2
gevent.sleep(39.9)

def spam(safe=False):
	for x in range(20):
		gevent.sleep(0.01)
		if safe:
			nick = client.nick
		else:
			nick = client._nick
		message = '{x}{safe} {client._new_nick} {nick}'.format(x=x, client=client, nick=nick, safe=('s' if safe else 'u'))
		client.msg(channel, message)

g1 = gevent.spawn(spam, False)
g2 = gevent.spawn(spam, True)

gevent.sleep(0.05)
client.msg(channel, 'Changing nick')
client.nick = nick3
client.msg(channel, 'Nick changed')
g1.get()
g2.get()
