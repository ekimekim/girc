
import sys
import logging

import gtools

from girc.client import Client

host = sys.argv[1]
nick = sys.argv[2]

logging.basicConfig(level=logging.DEBUG)

gtools.backdoor()

client = Client(host, nick)
client.start()
client.wait_for_stop()
