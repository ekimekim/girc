
"""
This example shows having two clients connected to the same irc network.
In addition, it shows the class-oriented usage of a Handler,
where we decorate a method at definition-time, then later (at init-time) register it
with the passed-in client.

The two connected bots will respond to private messages with either "Hi from client 1"
or "Hi from client 2".

Note also the use of client.matches_nick(msg.target) instead of client.nick == msg.target.
The distinction between these is subtle and comes down to behaviour during the ambiguity caused
by a nick change operation: client.matches_nick will match on the old or new nicks, whereas
client.nick will block until we can be sure the server has processed our nick change.

See girc/client.py for a full discussion on nick changes.
"""

import sys
import logging

from girc import Client, Handler

host, nick1, nick2 = sys.argv[1:4]

logging.basicConfig(level=logging.DEBUG)

class Test(object):
	@Handler(command='PRIVMSG')
	def reply(self, client, msg):
		if client.matches_nick(msg.target):
			client.msg(msg.sender, "Hi from client {}".format(self.n))
	def __init__(self, client, n):
		self.n = n
		self.reply.register(client)

c1 = Client(host, nick1)
c2 = Client(host, nick2)

t1 = Test(c1, 1)
t2 = Test(c2, 2)

c1.start()
c2.start()
c1.wait_for_stop()
c2.wait_for_stop()
