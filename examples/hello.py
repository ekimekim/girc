import logging
logging.basicConfig(level=logging.DEBUG)

import sys
from geventirc import Client

host = sys.argv[1]
nick = sys.argv[2]
channel = sys.argv[3]

client = Client('irc.desertbus.org', nick=nick)
channel = client.channel('#desertbus')

channel.join()
channel.msg("Hello world!")

@client.handler(command='PRIVMSG', payload=lambda value: nick in value.lower())
def mentioned(client, msg):
	channel.msg("Hello, {}!".format(msg.sender))

client.start()
client.join()
