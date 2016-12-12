
"""
This test script is meant to highlight the race conditions that can occur around a nick change operation.
After we send a NICK command to change our nick, there is a period of time where any messages we recieve
may have been sent before or after the nick change.

WARNING: This example intentionally spams the server at high speed in order to expose race conditions.
Running this against a public server is likely to get you banned, or at the very least make people angry.

IT IS NOT REQUIRED to understand all the following for basic usage!
For most cases, you should simply always use client.nick to get your current nick.

In this scenario, we presume we have a server of the type that will give you a grace period
when attempting to change to a protected nick before forcing you back to your old or a
generated nick.
That grace period should approximately equal FORCEBACK_TIME below. Experiment with that value to try
to have the force back occur during the spam period.

During the critical period, we attempt to send two messages every 10ms.
They detail:
	- the nick we are changing to
	- the nick we think the server thinks we are

One of them is the "safe" version, which uses client.nick. This is a property which blocks until
its value is known unambiguously.
The other remains "unsafe" and so gives us a full picture of the state changes as they occur.

Should the server's "force back" message occur during our own attempted nick change, four things may happen:
	1. The force-back will occur before we send our own nick change.
	   Both safe and unsafe loops will report the nick change immediately
	   and then our own nick change will proceed normally.

	2. The force-back will occur after send our own nick change but before the server has processed it.
	   The safe loop will be blocking on the nick change finishing and won't see this happen.
	   The unsafe loop will show that <what we think the server thinks we're called> will be updated immediately.
	   Our own requested change will then complete, leaving us with our intended new nick.
	   The safe loop will show us changing from the old nick to the new nick, with no temporary change

	3. The force-back will occur after the server has processed our nick change but before we've confirmed this.
	   (internally, the library "confirms" a nick change by issuing a PING and waiting for the response PONG,
	   since we assume the server is processing our messages in order)
	   The safe loop will be blocking on the nick change being confirmed and won't see this happen.
	   The unsafe loop will show that <the nick we're trying to change to> will be updated immediately.
	   Once the ping returns, we will arrive at our final state of the nick the server forced us to.
	   The safe loop will show us changing from the old nick to the forced nick, with no temporary change
	   to the new nick in the middle.

	4. The force-back occurs after the nick change has been confirmed.
	   Both safe and unsafe loops will report the nick change immediately.

Below we show a heavily abridged version of what this might look like in the above four scenarios:
	Scenario 1 (force before change):
		<nick2> 01u None nick2
		<nick2> 01s None nick2
		nick2 has changed name to forcednick
		<forcednick> 02u None nick2
		<forcednick> 02s None nick2
		<forcednick> 02u None forcednick
		<forcednick> 02s None forcednick
		<forcednick> Changing nick
		<forcednick> 03u nick3 forcednick
		forcednick has changed name to nick3
		<nick3> 04u nick3 forcednick
		<nick3> Nick changed
		<nick3> 03s None nick3
		<nick3> 05u None nick3
	Scenario 2 (force before server receives change):
		<nick2> 01u None nick2
		<nick2> 01s None nick2
		<nick2> Changing nick
		<nick2> 02u nick3 nick2
		nick2 has changed name to forcednick
		<nick2> 03u nick3 nick2
		<nick2> 04u nick3 forcednick
		forcednick has changed name to nick3
		<nick3> 05u nick3 forcednick
		<nick3> Nick changed
		<nick3> 02s None nick3
		<nick3> 06u None nick3
	Scenario 3 (force before change is confirmed)
		<nick2> 01u None nick2
		<nick2> 01s None nick2
		<nick2> Changing nick
		<nick2> 02u nick3 nick2
		nick2 has changed name to nick3
		<nick3> 03u nick3 nick2
		nick3 has changed name to forcednick
		<forcednick> 04u nick3 nick2
		<forcednick> 05u forcednick nick2
		<forcednick> Nick changed
		<forcednick> 02s None forcednick
		<forcednick> 06u None forcednick
	Scenario 4 (force after change is confirmed)
		<nick2> 01u None nick2
		<nick2> 01s None nick2
		<nick2> Changing nick
		<nick2> 02u nick3 nick2
		nick2 has changed name to nick3
		<nick3> 03u nick3 nick2
		<nick3> Nick changed
		<nick3> 02s None nick3
		<nick3> 03u None nick3
		nick3 has changed name to forcednick
		<forcednick> 03s None nick3
		<forcednick> 04u None nick3
		<forcednick> 04s None forcednick
		<forcednick> 05u None forcednick
"""

import sys
import logging

import gevent

from girc import Client

FORCEBACK_TIME = 40.0

host = sys.argv[1]
channel = sys.argv[2]
host, channel, nick1, nick2, nick3 = sys.argv[1:6]

logging.basicConfig(level=logging.DEBUG)

client = Client(host, nick1)
channel = client.channel(channel)
channel.join()
client.start()

client.nick = nick2
gevent.sleep(FORCEBACK_TIME - 0.1)

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
