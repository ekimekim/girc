
import logging
import os
import socket

from girc.client import Client

logging.basicConfig(level=logging.DEBUG)

def main(path, mode, *args):
	if mode == 'send':
		host, nick = args
		client = Client(host, nick)
		client.start()
		client.msg('ekimekim', 'sent from sender', block=True)

		try:
			listener = socket.socket(socket.AF_UNIX)
			listener.bind(path)
			listener.listen(128)
			sock, _ = listener.accept()
		finally:
			os.remove(path)

		client.handoff_to_sock(sock)

	elif mode == 'recv':
		sock = socket.socket(socket.AF_UNIX)
		sock.connect(path)
		client = Client.from_sock_handoff(sock)
		client.start()
		client.msg('ekimekim', 'sent from receiver')
		client.quit()

	else:
		raise ValueError("bad mode: must be send or recv")

if __name__ == '__main__':
	import sys
	main(*sys.argv[1:])
