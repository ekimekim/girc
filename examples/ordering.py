
import sys
import logging

import gevent

from girc.client import Client

host = sys.argv[1]
nick = sys.argv[2]

logging.basicConfig(level=logging.DEBUG)

match_args = dict(command='PRIVMSG', payload='ordering test')
client = Client(host, nick)

def reply(client, msg, s):
	client.msg(msg.reply_target, s)

@client.handler(**match_args)
def A(client, msg):
	reply(client, msg, "handler A")

@client.handler(after=A, sync=True, **match_args)
def B(client, msg):
	reply(client, msg, "handler B, after A, before next message is read")

@client.handler(before=B, **match_args)
def C(client, msg):
	reply(client, msg, "handler C, before B")

@client.handler(after=B, **match_args)
def D(client, msg):
	gevent.sleep(10)
	reply(client, msg, "handler D, after B, 10s sleep")

@client.handler(command='PRIVMSG', payload='hello test')
def hello(client, msg):
	reply(client, msg, "hi!")

client.start()
client.wait_for_stop()
