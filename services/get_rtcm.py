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
        self.lock = threading.Lock()

    def run(self):
        self.running = True
        reconnect_delay = 2
        MAX_DELAY = 30
        while self.running:
            s = None
            try:
                s = socket.create_connection((self.host, self.port), timeout=10)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                auth = base64.b64encode(f"{self.user}:{self.pwd}".encode()).decode()
                req = (f"GET /{self.mount} HTTP/1.1\r\n"
                       f"Host: {self.host}\r\n"
                       "Ntrip-Version: Ntrip/2.0\r\n"
                       "User-Agent: NTRIPRelay\r\n"
                       f"Authorization: Basic {auth}\r\n\r\n")
                s.send(req.encode())

                # Leitura de header com timeout de 15s
                s.settimeout(15)
                hdr = b''
                while b"\r\n\r\n" not in hdr:
                    chunk = s.recv(1)
                    if not chunk:
                        raise ConnectionError("Conexão fechada durante leitura do header RBMC")
                    hdr += chunk

                # Timeout de leitura de dados RTCM
                s.settimeout(30)
                reconnect_delay = 2  # reset após conexão bem-sucedida

                while self.running:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    with self.lock:
                        self.buffer.extend(chunk)

            except Exception as e:
                print(f"[NtripClient] erro: {e}, reconectando em {reconnect_delay}s")
                time.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, MAX_DELAY)
            finally:
                if s:
                    try:
                        s.close()
                    except Exception:
                        pass

    def get_data(self):
        """Retorna dados pendentes de forma thread-safe e limpa o buffer."""
        with self.lock:
            if not self.buffer:
                return None
            data = bytes(self.buffer)
            self.buffer.clear()
            return data

    def stop(self):
        self.running = False