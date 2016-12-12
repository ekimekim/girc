
"""
In this example, we have a basic 'hello world' IRC bot.
Upon joining the server and a channel, it will annouce 'Hello world!'.
Thereafter it will watch for any messages containing its nick (case-insensitive)
and respond to the sender with a "Hello, {sender}!"

Press ^C (ie. send SIGINT) to exit.
"""

import logging
import sys

from geventirc import Client

logging.basicConfig(level=logging.DEBUG)

host, nick, channel = sys.argv[1:4]

client = Client(host, nick=nick)
channel = client.channel(channel)

channel.join()
channel.msg("Hello world!")

@client.handler(command='PRIVMSG', payload=lambda value: nick.lower() in value.lower())
def mentioned(client, msg):
	channel.msg("Hello, {}!".format(msg.sender))

client.start()
client.join()
