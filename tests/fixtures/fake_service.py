"""Servico de mentira: abre uma porta TCP e imprime quais variaveis 'secretas' recebeu."""
import os
import socket
import sys
import time

print("SECRET_ENV=" + ",".join(sorted(k for k in os.environ if k.endswith("_API_KEY") or k.startswith("HERA_"))), flush=True)
srv = socket.socket()
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", int(sys.argv[1])))
srv.listen(5)
while True:
    time.sleep(0.2)
