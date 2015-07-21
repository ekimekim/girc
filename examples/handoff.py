
import logging

from girc.client import Client

logging.basicConfig(level=logging.DEBUG)

def main(path, mode, *args):
	if mode == 'send':
		host, nick = args
		client = Client(host, nick)
		client.start()
		client.msg('ekimekim', 'sent from sender', block=True)
		client.handoff_to_sock(path)
	elif mode == 'recv':
		client = Client.from_sock_handoff(path)
		client.start()
		client.msg('ekimekim', 'sent from receiver')
		client.quit()
	else:
		raise ValueError("bad mode: must be send or recv")

if __name__ == '__main__':
	import sys
	main(*sys.argv[1:])
