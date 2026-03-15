import socket
import time
import threading
import sys
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
LOCAL_NTRIP_PORT_FIXED = int(os.getenv('LOCAL_NTRIP_PORT_FIXED', '2103'))
GGA_CHECK_INTERVAL = 0.1  # intervalo de leitura de GGA

# --- Timeouts do servidor ---
CONN_SEND_TIMEOUT  = 10   # timeout máximo para sendall (s)
CONN_RECV_TIMEOUT  = 30   # timeout de recv quando não chega dados (s)


def parse_gngga(sentence: str):
    parts = sentence.strip().split(',')
    if parts[0].endswith('GGA'):
        lat = float(parts[2][:2]) + float(parts[2][2:]) / 60
        if parts[3] == 'S': lat = -lat
        lon = float(parts[4][:3]) + float(parts[4][3:]) / 60
        if parts[5] == 'W': lon = -lon
        return lat, lon
    return None, None


def _configure_conn(conn, tag):
    """Configura TCP keepalive e timeout de envio na conexão aceita."""
    conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    if sys.platform == 'win32':
        # Windows: (habilitar, intervalo_ms, timeout_ms)
        conn.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 10000, 5000))
    print(f"{tag} TCP keepalive configurado na conexão")


# =============================================================================
# Caster OTIMIZADO — troca automática de base (comportamento original)
# =============================================================================
class Caster:
    def __init__(self, listen_port: int):
        self.listen_port = listen_port
        self.sock = None
        self.ntrip_state = None
        self.state = 'INITIALIZE'
        self.current_base = None
        self.main_base = None
        self.helper_base = None
        self.tag = '[Caster-OPT]'

    def _ensure_listen_socket(self):
        """Cria o socket de escuta apenas uma vez (persistente)."""
        if self.sock is None:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.bind(('0.0.0.0', self.listen_port))
            self.sock.listen(1)
            print(f"{self.tag} Socket de escuta criado na porta {self.listen_port}")

    def await_receptor(self):
        self._ensure_listen_socket()
        print(f"{self.tag} aguardando receptor GNSS na porta {self.listen_port}")
        conn, addr = self.sock.accept()
        self.ntrip_state = 'await'
        print(f"{self.tag} receptor conectado de {addr}")
        _configure_conn(conn, self.tag)
        conn.settimeout(GGA_CHECK_INTERVAL)
        return conn, addr

    def await_coordinates(self, conn):
        while True:
            try:
                data = conn.recv(1024).decode(errors='ignore')
            except socket.timeout:
                continue
            if not data:
                continue
            print(f"{self.tag} Handshake recebido: {data.strip()}")
            lat, lon = parse_gngga(data)
            if lat is None:
                continue
            user_loc = (lat, lon)
            break
        return user_loc

    def select_bases(self, ibge):
        bases = ibge.fetch_active_bases()
        main_b, helper_b = ibge.prioritize(bases)
        return main_b, helper_b

    def start_rbmc(self):
        client = NtripClient(self.current_base['id'], RBMC_HOST, RBMC_PORT, RBMC_USER, RBMC_PASS)
        client.start()
        return client

    def ntrip_on(self, conn, ibge, client):
        if self.ntrip_state == 'await':
            conn.send(b"ICY 200 OK\r\nContent-Type: gnss/data\r\n\r\n")
            print(f"{self.tag} Header NTRIP enviado ao receptor")
            self.ntrip_state = 'active'
        try:
            while self.state == 'SEND_RTCM':
                # 1) Verifica se chegou novo GGA do receptor
                try:
                    gga = conn.recv(1024).decode(errors='ignore')
                    print(f'{self.tag} Handshake recebido:{gga}')
                except socket.timeout:
                    gga = ''
                if gga:
                    lat, lon = parse_gngga(gga)
                    if lat is not None:
                        ibge.user_location = (lat, lon)
                        main_b_new, helper_b_new = self.select_bases(ibge)
                        if main_b_new['id'] != self.main_base['id'] or helper_b_new['id'] != self.helper_base['id']:
                            print(f"{self.tag} Mudança de base detectada: principal {main_b_new['id']}, auxiliar {helper_b_new['id']}")
                            # troca para nova principal
                            client.stop()
                            self.main_base = main_b_new
                            self.helper_base = helper_b_new
                            self.current_base = self.main_base
                            self.state = 'INITIALIZE'
                            print(f"{self.tag} Novo estado: {self.state}")

                # 2) Envia RTCM do client ativo (thread-safe)
                data = client.get_data()
                if data:
                    try:
                        # Timeout de envio para detectar client morto
                        conn.settimeout(CONN_SEND_TIMEOUT)
                        conn.sendall(data)
                        conn.settimeout(GGA_CHECK_INTERVAL)
                        print(f"{self.tag} Enviado {len(data)} bytes de RTCM")
                    except socket.timeout:
                        print(f"{self.tag} TIMEOUT no envio ({CONN_SEND_TIMEOUT}s) — client morto, desconectando")
                        self.state = 'INITIALIZE'
                        break
                    except (BrokenPipeError, ConnectionResetError, OSError) as e:
                        print(f"{self.tag} Receptor desconectou: {e}")
                        self.state = 'INITIALIZE'
                        break
                else:
                    time.sleep(0.01)

        except Exception as e:
            print(f"{self.tag} Reiniciando... Erro: {e}")
        finally:
            client.stop()
            conn.close()
            # NÃO fechar self.sock — socket de escuta é persistente
            if self.state != 'CONECT_RBMC':
                self.state = 'INITIALIZE'
                print(f"{self.tag} Novo estado: {self.state}")

    def serve(self):
        while True:
            if self.state == 'INITIALIZE':
                conn, addr = self.await_receptor()
                self.state = 'GET_COORDINATES'
                print(f"{self.tag} Novo estado: {self.state}")

            if self.state == 'GET_COORDINATES':
                user_loc = self.await_coordinates(conn)
                print(f"{self.tag} Localização inicial: {user_loc}")
                ibge = IBGEEndpointClient(IBGE_ENDPOINT_URL, user_loc)
                self.state = 'DEFINE_BASE'
                print(f"{self.tag} Novo estado: {self.state}")

            if self.state == 'DEFINE_BASE':
                main_base, helper_base = self.select_bases(ibge)
                self.current_base = main_base
                self.main_base = main_base
                self.helper_base = helper_base
                print(f"{self.tag} Base inicial: {self.current_base['id']} ({self.current_base['distance_km']:.1f} km)")
                self.state = 'CONECT_RBMC'
                print(f"{self.tag} Novo estado: {self.state}")

            if self.state == 'CONECT_RBMC':
                client = self.start_rbmc()
                self.state = 'SEND_RTCM'
                print(f"{self.tag} Novo estado: {self.state}")

            if self.state == 'SEND_RTCM':
                self.ntrip_on(conn, ibge, client)


# =============================================================================
# Caster de BASE FIXA — mountpoint via rota NTRIP ou nearest-fixo
# =============================================================================
#   Porta 2103 — aceita conexão NTRIP padrão.
#   • Se o client enviar  GET /SPAR0  → usa SPAR0 como base fixa.
#   • Se o client enviar apenas GGA (sem HTTP) → pega a base mais próxima,
#     mas NÃO troca durante a sessão ("nearest-fixo").
# =============================================================================
class FixedBaseCaster:
    def __init__(self, listen_port: int):
        self.listen_port = listen_port
        self.sock = None
        self.tag = '[Caster-FIX]'

    def _parse_ntrip_request(self, data: str):
        """Tenta extrair mountpoint de um GET /MOUNT HTTP/1.x.
        Retorna o mountpoint ou None se não for HTTP."""
        for line in data.splitlines():
            line = line.strip()
            if line.upper().startswith('GET '):
                parts = line.split()
                if len(parts) >= 2:
                    path = parts[1].lstrip('/')
                    if path:
                        return path
        return None

    def _resolve_mountpoint(self, conn):
        """Lê dados do cliente e determina o mountpoint.
        Retorna (mountpoint, user_location_or_None)."""
        buffer = ''
        while True:
            try:
                data = conn.recv(1024).decode(errors='ignore')
            except socket.timeout:
                continue
            if not data:
                continue
            buffer += data
            print(f"{self.tag} Dados recebidos: {buffer.strip()}")

            # Tenta parsear como requisição NTRIP (GET /MOUNT)
            mount = self._parse_ntrip_request(buffer)
            if mount:
                print(f"{self.tag} Mountpoint via rota: {mount}")
                return mount, None

            # Tenta parsear como GGA direto (cliente legado)
            lat, lon = parse_gngga(buffer)
            if lat is not None:
                print(f"{self.tag} GGA recebido sem mountpoint, usando nearest-fixo")
                return None, (lat, lon)

    def _get_nearest_mount(self, user_loc):
        """Consulta RBMC e retorna o mountpoint mais próximo."""
        ibge = IBGEEndpointClient(IBGE_ENDPOINT_URL, user_loc)
        bases = ibge.fetch_active_bases()
        ordered = ibge.prioritize(bases)
        nearest = ordered[0]
        print(f"{self.tag} Base mais próxima: {nearest['id']} ({nearest['distance_km']:.1f} km) — fixa para a sessão")
        return nearest['id']

    def _ensure_listen_socket(self):
        """Cria o socket de escuta apenas uma vez (persistente)."""
        if self.sock is None:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.bind(('0.0.0.0', self.listen_port))
            self.sock.listen(1)
            print(f"{self.tag} Socket de escuta criado na porta {self.listen_port}")

    def serve(self):
        while True:
            conn = None
            client = None
            try:
                # 1) Aguarda conexão do receptor (socket persistente)
                self._ensure_listen_socket()
                print(f"{self.tag} aguardando receptor GNSS na porta {self.listen_port}")
                conn, addr = self.sock.accept()
                print(f"{self.tag} receptor conectado de {addr}")
                _configure_conn(conn, self.tag)
                conn.settimeout(GGA_CHECK_INTERVAL)

                # 2) Determina mountpoint (via rota ou nearest-fixo)
                mountpoint, user_loc = self._resolve_mountpoint(conn)

                if mountpoint is None and user_loc is not None:
                    # Modo nearest-fixo: pega base mais próxima mas não troca
                    mountpoint = self._get_nearest_mount(user_loc)

                if mountpoint is None:
                    print(f"{self.tag} Erro: não foi possível determinar mountpoint")
                    continue

                # 3) Conecta ao RBMC com base fixa
                print(f"{self.tag} Base fixa para esta sessão: {mountpoint}")
                client = NtripClient(mountpoint, RBMC_HOST, RBMC_PORT, RBMC_USER, RBMC_PASS)
                client.start()

                # 4) Envia header NTRIP e começa relay
                conn.sendall(b"ICY 200 OK\r\nContent-Type: gnss/data\r\n\r\n")
                print(f"{self.tag} Header NTRIP enviado ao receptor")

                while True:
                    # Lê (e descarta) novos GGAs — base nunca muda
                    try:
                        gga = conn.recv(1024).decode(errors='ignore')
                        if gga:
                            print(f"{self.tag} GGA recebido (base mantida: {mountpoint})")
                    except socket.timeout:
                        pass

                    # Envia RTCM da base fixa (thread-safe)
                    data = client.get_data()
                    if data:
                        try:
                            # Timeout de envio para detectar client morto
                            conn.settimeout(CONN_SEND_TIMEOUT)
                            conn.sendall(data)
                            conn.settimeout(GGA_CHECK_INTERVAL)
                            print(f"{self.tag} Enviado {len(data)} bytes RTCM (base: {mountpoint})")
                        except socket.timeout:
                            print(f"{self.tag} TIMEOUT no envio ({CONN_SEND_TIMEOUT}s) — client morto, encerrando sessão")
                            break
                        except (BrokenPipeError, ConnectionResetError, OSError) as e:
                            print(f"{self.tag} Client desconectou: {e}, encerrando sessão")
                            break
                    else:
                        time.sleep(0.01)

            except Exception as e:
                print(f"{self.tag} Erro: {e}, reiniciando...")
            finally:
                if client:
                    client.stop()
                if conn:
                    conn.close()
                # NÃO fechar self.sock — socket de escuta é persistente
                print(f"{self.tag} Sessão encerrada, aguardando nova conexão...")
                time.sleep(0.5)


# =============================================================================
# Execução: ambos os casters em threads separadas
# =============================================================================
def run_optimized_caster():
    print("[Main] Iniciando Caster OTIMIZADO na porta", LOCAL_NTRIP_PORT)
    caster = Caster(LOCAL_NTRIP_PORT)
    caster.serve()  # socket de escuta persistente, aceita reconexões rápidas


def run_fixed_caster():
    print(f"[Main] Iniciando Caster BASE FIXA na porta {LOCAL_NTRIP_PORT_FIXED} (mountpoint via rota)")
    caster = FixedBaseCaster(LOCAL_NTRIP_PORT_FIXED)
    caster.serve()


if __name__ == '__main__':
    t_opt = threading.Thread(target=run_optimized_caster, daemon=True, name='caster-opt')
    t_fix = threading.Thread(target=run_fixed_caster, daemon=True, name='caster-fix')

    t_opt.start()
    t_fix.start()

    # Mantém o processo principal vivo
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[Main] Encerrando casters...")
