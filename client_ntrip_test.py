import threading
import serial
import socket
import time
import argparse
import sys
from crccheck.crc import Crc24LteA
from dotenv import load_dotenv
import os
from datetime import datetime

load_dotenv()

# CONFIGURAÇÃO PADRÃO (pode ser sobrescrita por CLI)
SERIAL_PORT  = os.getenv('SERIAL_PORT_TEST')
BAUDRATE     = 115200
ORCH_HOST    = os.getenv('ORCH_HOST')
ORCH_PORT    = int(os.getenv('ORCH_PORT_TEST'))
GGA_INTERVAL = 60
LOG = 'ACTIVE'

# Constantes de reconexão
CONNECT_TIMEOUT = 10       # timeout para create_connection (s)
SOCKET_TIMEOUT  = 30       # timeout de leitura no socket (s)
RECONNECT_BASE  = 2        # delay inicial de reconexão (s)
RECONNECT_MAX   = 30       # delay máximo de reconexão (s)


def parse_args():
    parser = argparse.ArgumentParser(
        description='Cliente NTRIP — conecta ao Caster e envia RTCM ao receptor GNSS'
    )
    parser.add_argument('--serial', type=str, default=None,
                        help='Porta serial do receptor GNSS (ex: COM5, COM6). Padrão: .env SERIAL_PORT')
    parser.add_argument('--port', type=int, default=None,
                        help='Porta TCP do Caster (ex: 2102, 2103). Padrão: .env ORCH_PORT')
    parser.add_argument('--prefix', type=str, default='',
                        help='Prefixo do arquivo de log (ex: OPT, FIX). Padrão: vazio')
    parser.add_argument('--mount', type=str, default=None,
                        help='Mountpoint NTRIP para base fixa (ex: SPAR0). Se definido, envia GET /MOUNT ao caster')
    return parser.parse_args()


def make_log_name(prefix: str) -> str:
    ts = datetime.now().strftime("%d%m%y-%H%M%S")
    if prefix:
        return f"logs/{prefix}_LOG{ts}.txt"
    return f"logs/LOG{ts}.txt"


def log(line, log_name):
    if LOG == 'ACTIVE':
        with open(log_name, 'a', encoding='utf-8') as f:
            f.write(line + '\n')


def ts():
    """Timestamp formatado para logs de console."""
    return datetime.now().strftime("%H:%M:%S")


def serial_reader(ser, sock, stop_event, stop_sock, log_name, tag):
    """
    Lê NMEA GGA da serial, envia handshake e dispara reenvio periódico.
    """
    first_gga = True
    timer = 0
    t0 = time.time()
    while not stop_event.is_set():
        raw = ser.readline()
        if not raw:
            continue
        line = raw.decode('ascii', errors='ignore').strip()
        if line.startswith('$PUBX,00'):
            print(f"[{ts()}][GNSS PUBX]{tag} {line}")
            log(line, log_name)
        elif line.startswith('$GNGGA'):
            check_lat = line.split(',')[4]
            timer = time.time() - t0
            if (timer > GGA_INTERVAL or first_gga) and check_lat:
                gga_sentence = line
                if not stop_sock.is_set():
                    try:
                        sock.sendall((gga_sentence + '\r\n').encode('ascii'))
                        print(f"[{ts()}][Gateway]{tag} Reenviado GGA: {gga_sentence}")
                        first_gga = False
                    except (OSError, BrokenPipeError, ConnectionResetError) as e:
                        print(f"[{ts()}][serial_reader]{tag} Socket fechado ao enviar GGA: {e}")
                        stop_sock.set()
                        break
                else:
                    first_gga = True
                timer = 0
                t0 = time.time()

            print(f"[{ts()}][GNSS NMEA]{tag} {line}")
            log(line, log_name)
        elif line.startswith('$GN'):
            print(f"[{ts()}][GNSS NMEA]{tag} {line}")
            log(line, log_name)

    print(f"[{ts()}][serial_reader]{tag} encerrado")


def get_rtcm_msg_type(frame: bytes) -> int:
    """Extrai o tipo de mensagem RTCM3 (12 bits nos bytes 3-4 do frame)."""
    return (frame[3] << 4) | (frame[4] >> 4)


def extract_rtcm_frame(buffer: bytearray):
    """
    Extrai UM frame RTCM3 do buffer, validando CRC.
    Retorna (frame_bytes_ou_None, frame_len_consumido).
    Retorna (None, 0) se não há frame completo.
    """
    if len(buffer) < 6:
        return None, 0
    if buffer[0] != 0xD3:
        idx = buffer.find(0xD3)
        if idx < 0:
            buffer.clear()
            return None, 0
        del buffer[:idx]
        return None, 0
    length = ((buffer[1] & 0x03) << 8) | buffer[2]
    frame_len = 3 + length + 3
    if len(buffer) < frame_len:
        return None, 0
    frame = bytes(buffer[:frame_len])
    calc_crc = Crc24LteA.calc(frame[:-3]).to_bytes(3, 'big')
    recv_crc = frame[-3:]
    if calc_crc != recv_crc:
        return None, frame_len  # CRC inválido, descarta
    return frame, frame_len


def process_rtcm_buffer(buffer: bytearray, ser, tag):
    """
    Extrai TODOS os frames RTCM do buffer, deduplica por tipo de mensagem,
    e envia ao receptor apenas o frame mais recente de cada tipo.
    Isso evita que dados RTCM antigos (acumulados por lag) cheguem ao receptor.
    """
    # Fase 1: Extrair todos os frames válidos do buffer
    frames_by_type = {}   # msg_type → (frame, frame_len)
    total_extracted = 0
    total_frames = 0
    crc_errors = 0

    while True:
        frame, consumed = extract_rtcm_frame(buffer)
        if consumed == 0:
            break
        del buffer[:consumed]
        total_extracted += consumed
        if frame is not None:
            msg_type = get_rtcm_msg_type(frame)
            frames_by_type[msg_type] = frame  # sobrescreve → mantém o mais recente
            total_frames += 1
        else:
            crc_errors += 1

    if total_frames == 0:
        return

    # Fase 2: Enviar ao receptor apenas o frame mais recente de cada tipo
    sent = len(frames_by_type)
    discarded = total_frames - sent

    for msg_type, frame in frames_by_type.items():
        ser.write(frame)
        ser.flush()

    if discarded > 0:
        print(f"[{ts()}][RTCM → GNSS]{tag} Burst detectado: {total_frames} frames extraídos, "
              f"{discarded} stale descartados, {sent} enviados "
              f"(tipos: {sorted(frames_by_type.keys())})")
    else:
        types_str = ','.join(str(t) for t in sorted(frames_by_type.keys()))
        total_bytes = sum(len(f) for f in frames_by_type.values())
        print(f"[{ts()}][RTCM → GNSS]{tag} Sent {sent} frames ({total_bytes} bytes), tipos: [{types_str}]")

    if crc_errors > 0:
        print(f"[{ts()}][process_rtcm]{tag} {crc_errors} frames descartados por CRC inválido")


def rtcm_gateway(ser, sock, stop_event, tag, stats):
    """
    Parse de HTTP chunked e retransmissão de RTCM3.
    Utiliza timeout no socket para detectar perda de conexão.
    """
    try:
        sock.settimeout(SOCKET_TIMEOUT)
        f = sock.makefile('rb')
        hdr = b''
        while not stop_event.is_set() and b'\r\n\r\n' not in hdr:
            chunk = f.read(1)
            if not chunk:
                print(f"[{ts()}][rtcm_gateway]{tag} Conexão fechada durante leitura do header")
                stop_event.set()
                return
            hdr += chunk
        if stop_event.is_set():
            print(f"[{ts()}][rtcm_gateway]{tag} encerrado antes de iniciar parsing")
            return
        print(f"[{ts()}][Gateway]{tag} Header NTRIP recebido, iniciando parser chunked RTCM")
        buffer = bytearray()
        while not stop_event.is_set():
            size_line = f.readline()
            if not size_line:
                print(f"[{ts()}][Gateway]{tag} EOF ao ler tamanho do chunk")
                break
            try:
                size = int(size_line.strip(), 16)
            except ValueError:
                print(f"[{ts()}][Gateway]{tag} chunk size parse error: {size_line.strip()}")
                continue
            if size == 0:
                print(f"[{ts()}][Gateway]{tag} chunk size 0, fim do stream")
                break
            data = f.read(size)
            f.read(2)
            stats['bytes_received'] += len(data)
            buffer.extend(data)
            process_rtcm_buffer(buffer, ser, tag)
    except socket.timeout:
        print(f"[{ts()}][Gateway]{tag} TIMEOUT: sem dados RTCM há {SOCKET_TIMEOUT}s — reconectando")
    except (ConnectionResetError, ConnectionAbortedError, OSError) as e:
        print(f"[{ts()}][Gateway]{tag} Conexão perdida: {e}")
    finally:
        stop_event.set()
        print(f"[{ts()}][rtcm_gateway]{tag} encerrado, stop_event: {stop_event.is_set()}")


def run_gateway(serial_port, caster_port, log_prefix, mount=None):
    tag = f" [{log_prefix}]" if log_prefix else ""
    log_name = make_log_name(log_prefix)
    print(f"[{ts()}][+]{tag} Log: {log_name}")

    ser = serial.Serial(serial_port, BAUDRATE, timeout=1)
    print(f"[{ts()}][+]{tag} Serial aberta em {serial_port}@{BAUDRATE}")

    t1 = None
    stop_sock = threading.Event()
    stop_read_serial = threading.Event()
    sock = None
    reconnect_delay = RECONNECT_BASE
    reconnect_count = 0

    # Estatísticas de sessão
    stats = {
        'bytes_received': 0,
        'session_start': None,
    }

    while True:
        try:
            print(f"[{ts()}][Gateway]{tag} Conectando ao Caster {ORCH_HOST}:{caster_port} (timeout {CONNECT_TIMEOUT}s)...")
            sock = socket.create_connection((ORCH_HOST, caster_port), timeout=CONNECT_TIMEOUT)

            # TCP Keepalive — detecta conexões mortas rapidamente
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            if sys.platform == 'win32':
                # Windows: (habilitar, intervalo ms, timeout ms)
                sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 10000, 5000))

            print(f"[{ts()}][+]{tag} TCP conectado ao Caster em {ORCH_HOST}:{caster_port}")

            # Reset do backoff após conexão bem-sucedida
            reconnect_delay = RECONNECT_BASE

            # Contagem de reconexões
            reconnect_count += 1
            stats['session_start'] = time.time()
            stats['bytes_received'] = 0
            print(f"[{ts()}][Stats]{tag} Sessão #{reconnect_count} iniciada")

            # Se mount definido, envia requisição NTRIP com mountpoint
            if mount:
                ntrip_req = f"GET /{mount} HTTP/1.1\r\nHost: {ORCH_HOST}\r\n\r\n"
                sock.sendall(ntrip_req.encode('ascii'))
                print(f"[{ts()}][+]{tag} Enviado GET /{mount} ao caster (base fixa)")

            if t1:
                stop_read_serial.set()
                t1.join(timeout=1)
                t2 = None
                t1 = None
            stop_sock.clear()
            stop_read_serial.clear()
            t1 = threading.Thread(target=serial_reader, args=(ser, sock, stop_read_serial, stop_sock, log_name, tag), daemon=True)
            t1.start()

            t2 = threading.Thread(target=rtcm_gateway, args=(ser, sock, stop_sock, tag, stats), daemon=True)
            t2.start()
            while not stop_sock.is_set() and t1.is_alive() and t2.is_alive():
                time.sleep(0.2)

        except socket.timeout:
            print(f"[{ts()}][Gateway]{tag} Timeout de conexão ({CONNECT_TIMEOUT}s), reconectando em {reconnect_delay}s...")
            time.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, RECONNECT_MAX)
        except (ConnectionRefusedError, ConnectionResetError, OSError) as e:
            print(f"[{ts()}][Gateway]{tag} Erro de conexão: {e}, reconectando em {reconnect_delay}s...")
            time.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, RECONNECT_MAX)
        except Exception as e:
            print(f"[{ts()}][Gateway]{tag} Erro inesperado: {e}, reconectando em {reconnect_delay}s...")
            time.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, RECONNECT_MAX)
        finally:
            stop_sock.set()
            if t2:
                t2.join(timeout=2)
            # Estatísticas da sessão encerrada
            if stats['session_start']:
                elapsed = time.time() - stats['session_start']
                print(f"[{ts()}][Stats]{tag} Sessão #{reconnect_count} encerrada: "
                      f"{stats['bytes_received']} bytes em {elapsed:.1f}s")
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass
            print(f"[{ts()}][*]{tag} Threads encerradas, reiniciando gateway...")


def main():
    args = parse_args()

    serial_port = args.serial or SERIAL_PORT
    caster_port = args.port or ORCH_PORT
    log_prefix  = args.prefix
    mount       = args.mount

    print("=" * 60)
    print(f"  Cliente NTRIP")
    print(f"  Serial : {serial_port}")
    print(f"  Caster : {ORCH_HOST}:{caster_port}")
    print(f"  Mount  : {mount or '(auto)'}")
    print(f"  Prefixo: {log_prefix or '(nenhum)'}")
    print(f"  Timeouts: connect={CONNECT_TIMEOUT}s, read={SOCKET_TIMEOUT}s")
    print(f"  Backoff : {RECONNECT_BASE}s → {RECONNECT_MAX}s")
    print("=" * 60)

    run_gateway(serial_port, caster_port, log_prefix, mount)


if __name__ == '__main__':
    main()
