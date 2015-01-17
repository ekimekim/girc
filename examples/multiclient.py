
import sys
import logging

import gtools

from girc.client import Client
from girc.handler import Handler

host = sys.argv[1]
nick1 = sys.argv[2]
nick2 = sys.argv[3]

logging.basicConfig(level=logging.DEBUG)

gtools.backdoor()

c1 = Client(host, nick1)
c2 = Client(host, nick2)

class Test(object):
	@Handler(command='PRIVMSG')
	def reply(self, client, msg):
		if client.matches_nick(msg.target):
			client.msg(msg.sender, "Hi from client {}".format(self.n))
	def __init__(self, client, n):
		self.n = n
		self.reply.register(client)

t1 = Test(c1, 1)
t2 = Test(c2, 2)


c1.start()
c2.start()
c1.wait_for_stop()
c2.wait_for_stop()
