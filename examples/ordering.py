
"""
This example shows how you can refer to other handlers when delcaring a handler in order
to guarentee an ordering between them.

It sets up 5 handlers: A, B, C, D, and "hello".
It specifies that:
	B should be after A
	B should be before "sync", ie. before any future messages are processed.
		sync=True is mainly useful for messages which modify important state,
		such as maintaining user lists, which need to be finished before the next message is handled.
	C should be before B.
	D should be after B, and furthermore after 1 second has passed
It leaves "hello" to be called at any time.

This creates the following dependency graph:
	A,C -> B -> D
The ordering between A and C, and when hello will run, is unspecified.
So one possible output might be:
	handler C, before B
	handler A
	handler B, after A, before next message is read
	<next message is processed>
	hi!
	<1 second passes>
	handler D, after B, 1s sleep
"""

import sys
import logging

import gevent

from girc.client import Client

host, nick = sys.argv[1:3]

logging.basicConfig(level=logging.DEBUG)

# this is just shorthand for below so we don't need to write out the same match args each time
match_args = dict(command='PRIVMSG', payload='ordering test')

client = Client(host, nick)

def reply(client, msg, s):
	# this will reply back to sender if the message is a PM, else on the channel it was sent on.
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
	gevent.sleep(1)
	reply(client, msg, "handler D, after B, 1s sleep")

@client.handler(**match_args)
def hello(client, msg):
	reply(client, msg, "hi!")

client.start()
client.wait_for_stop()
