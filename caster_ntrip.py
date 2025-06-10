import socket
import time
from urllib.parse import urlparse
from services.base_priorization_service import IBGEEndpointClient
from services.get_rtcm import NtripClient
from dotenv import load_dotenv
import os

load_dotenv()

# --- Configurações de ambiente ---
raw_caster = os.getenv('RBMC_CASTER')
parsed = urlparse(raw_caster)
if parsed.scheme and parsed.hostname:
    RBMC_HOST = parsed.hostname
    RBMC_PORT = parsed.port or int(os.getenv('RBMC_PORT'))
else:
    RBMC_HOST = raw_caster
    RBMC_PORT = int(os.getenv('RBMC_PORT'))

RBMC_USER         = os.getenv('RBMC_USER')
RBMC_PASS         = os.getenv('RBMC_PASS')
IBGE_ENDPOINT_URL = os.getenv('IBGE_ENDPOINT_URL')
LOCAL_NTRIP_PORT  = int(os.getenv('LOCAL_NTRIP_PORT'))
GGA_CHECK_INTERVAL = 0.1  # intervalo de leitura de GGA


def parse_gngga(sentence: str):
    parts = sentence.strip().split(',')
    if parts[0].endswith('GGA'):
        lat = float(parts[2][:2]) + float(parts[2][2:]) / 60
        if parts[3] == 'S': lat = -lat
        lon = float(parts[4][:3]) + float(parts[4][3:]) / 60
        if parts[5] == 'W': lon = -lon
        return lat, lon
    return None, None


class Caster:
    def __init__(self, listen_port: int):
        self.sock = None
        self.ntrip_state = None
        self.state = 'INITIALIZE'
        self.current_base = None
        self.main_base = None
        self.helper_base = None

    

    def await_receptor(self,ntrip_port):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(('0.0.0.0', ntrip_port))
        self.sock.listen(1)
        print(f"[Caster] aguardando receptor GNSS na porta {ntrip_port}")
        conn, addr = self.sock.accept()
        self.ntrip_state = 'await'
        print(f"[Caster] receptor conectado de {addr}")
        conn.settimeout(GGA_CHECK_INTERVAL)
        return conn, addr

    def await_coordinates(self,conn):
        while True:
            try:
                data = conn.recv(1024).decode(errors='ignore')
            except socket.timeout:
                continue
            if not data:
                continue
            print(f"[Caster] Handshake recebido: {data.strip()}")
            lat, lon = parse_gngga(data)
            if lat is None:
                continue
            user_loc = (lat, lon)
            break
        return user_loc

    def select_bases(self,ibge):
        bases = ibge.fetch_active_bases()
        main_b, helper_b = ibge.prioritize(bases)
        return main_b,helper_b
        

    def start_rbmc(self):
        client = NtripClient(self.current_base['id'], RBMC_HOST, RBMC_PORT, RBMC_USER, RBMC_PASS)
        client.start()
        return client
    
        
    def ntrip_on(self,conn,ibge,client):
        if self.ntrip_state == 'await':
            conn.send(b"ICY 200 OK\r\nContent-Type: gnss/data\r\n\r\n")
            print("[Caster] Header NTRIP enviado ao receptor")
            self.ntrip_state = 'active'
        try:
            while self.state == 'SEND_RTCM':
                # 1) Verifica se chegou novo GGA do receptor
                try:
                    gga = conn.recv(1024).decode(errors='ignore')
                    print(f'[Caster] Handshake recebido:{gga}')
                except socket.timeout:
                    gga = ''
                if gga:
                    lat, lon = parse_gngga(gga)
                    if lat is not None:
                        ibge.user_location = (lat, lon)
                        main_b_new, helper_b_new = self.select_bases(ibge)
                        if main_b_new['id'] != self.main_base['id'] or helper_b_new['id'] != self.helper_base['id']:
                            print(f"[Caster] Mudança de base detectada: principal {main_b_new['id']}, auxiliar {helper_b_new['id']}")
                            # troca para nova principal
                            client.stop()
                            self.main_base = main_b_new
                            self.helper_base = helper_b_new
                            self.current_base = self.main_base
                            self.state = 'INITIALIZE'
                            print(f"[Caster] Novo estado: {self.state}")

                # 2) Envia RTCM do client ativo
                if client.buffer:
                    conn.send(client.buffer)
                    client.buffer.clear()
                else:
                    time.sleep(0.01)

        except Exception as e:
            print(f"[Caster] Reiniciando... Erro: {e}")
        finally:
            client.stop()
            conn.close()
            self.sock.close()
            if self.state != 'CONECT_RBMC':
                self.state = 'INITIALIZE'
                print(f"[Caster] Novo estado: {self.state}")


    def serve(self):
        while True:
            if self.state == 'INITIALIZE':
                conn, addr = self.await_receptor(LOCAL_NTRIP_PORT)
                
                self.state = 'GET_COORDINATES'
                print(f"[Caster] Novo estado: {self.state}")
                
            if self.state == 'GET_COORDINATES':
                user_loc = self.await_coordinates(conn)
                print(f"[Caster] Localização inicial: {user_loc}")
                ibge = IBGEEndpointClient(IBGE_ENDPOINT_URL, user_loc)
                self.state = 'DEFINE_BASE'
                print(f"[Caster] Novo estado: {self.state}")

            if self.state == 'DEFINE_BASE':
                main_base,helper_base = self.select_bases(ibge)
                self.current_base = main_base
                self.main_base = main_base
                self.helper_base = helper_base
                print(f"[Caster] Base inicial: {self.current_base['id']} ({self.current_base['distance_km']:.1f} km)")
                self.state = 'CONECT_RBMC'
                print(f"[Caster] Novo estado: {self.state}")
           
            if self.state == 'CONECT_RBMC':
                client = self.start_rbmc()
                self.state = 'SEND_RTCM'
                print(f"[Caster] Novo estado: {self.state}")

            if self.state == 'SEND_RTCM':
                self.ntrip_on(conn,ibge,client)


if __name__ == '__main__':
    orch = Caster(LOCAL_NTRIP_PORT)
    orch.serve()
