
"""
In this example, we demonstrate one way to do graceful restart (without disconnecting)
by handing off the connection state and fd to another process.

Usage:
	Open two shells. In one, run:
		python handoff.py send PATH HOST NICK TARGET
	PATH will be used to create a unix socket, so should be writable. For example, "/tmp/handoff.sock".
	This will connect to HOST as NICK and send "sent from sender" to TARGET channel or user.
	It will then stay connected, idle.
	In the other shell, run:
		python handoff.py recv PATH TARGET
	PATH must be the same as above.
	This will cause the 'send' process to hand off the connection to the 'recv' process, and exit.
	The recv process will then send "sent from receiver" to TARGET channel or user from the same connection.

This example uses a UNIX socket to transfer the connection, but any means of communicating an open
file descriptor is possible to use similarly, for example executing a child and handing off to it,
or re-exec()ing your own process.
"""

import logging
import os
import socket

from girc.client import Client

logging.basicConfig(level=logging.DEBUG)

def main(mode, path, *args):
	if mode == 'send':
		host, nick, target = args
		client = Client(host, nick)
		client.start()
		client.msg(target, 'sent from sender', block=True)

		try:
			listener = socket.socket(socket.AF_UNIX)
			listener.bind(path)
			listener.listen(128)
			sock, _ = listener.accept()
		finally:
			os.remove(path)

		client.handoff_to_sock(sock)

	elif mode == 'recv':
		target, = args
		sock = socket.socket(socket.AF_UNIX)
		sock.connect(path)
		client = Client.from_sock_handoff(sock)
		client.start()
		client.msg(target, 'sent from receiver')
		client.quit()

	else:
		raise ValueError("bad mode: must be send or recv")

if __name__ == '__main__':
	import sys
	main(*sys.argv[1:])
