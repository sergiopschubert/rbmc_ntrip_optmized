import threading
import socket
import base64
import time


class NtripClient(threading.Thread):
    def __init__(self, mount: str, host: str, port: int, user: str, pwd: str):
        super().__init__(daemon=True)
        self.mount = mount; self.host = host; self.port = port
        self.user = user; self.pwd = pwd
        self.socket = None
        self.running = False
        self.buffer = bytearray()

    def run(self):
        self.running = True
        while self.running:
            try:
                s = socket.create_connection((self.host, self.port), timeout=10)
                auth = base64.b64encode(f"{self.user}:{self.pwd}".encode()).decode()
                req = (f"GET /{self.mount} HTTP/1.1\r\n"
                       f"Host: {self.host}\r\n"
                       "Ntrip-Version: Ntrip/2.0\r\n"
                       "User-Agent: NTRIPRelay\r\n"
                       f"Authorization: Basic {auth}\r\n\r\n")
                s.send(req.encode())
                hdr=b''
                while b"\r\n\r\n" not in hdr:
                    hdr+=s.recv(1)
                while self.running:
                    chunk = s.recv(4096)
                    if not chunk: break
                    self.buffer.extend(chunk)
                s.close()
            except Exception as e:
                print(f"[NtripClient] erro: {e}, reconectando em 5s")
                time.sleep(5)

    def stop(self):
        self.running = False