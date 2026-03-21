import threading
import serial
import socket
import time
import argparse
import sys
import math
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

_debug_log_name: str | None = None


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


def make_debug_log_name(prefix: str) -> str:
    ts_str = datetime.now().strftime("%d%m%y-%H%M%S")
    if prefix:
        return f"logs/{prefix}_DEBUG{ts_str}.txt"
    return f"logs/DEBUG{ts_str}.txt"


def log(line, log_name):
    if LOG == 'ACTIVE':
        with open(log_name, 'a', encoding='utf-8') as f:
            f.write(line + '\n')


def ts():
    """Timestamp formatado para logs de console."""
    return datetime.now().strftime("%H:%M:%S")


def dprint(*args, **kwargs):
    """Print no console e salva no arquivo de debug com timestamp."""
    print(*args, **kwargs)
    if _debug_log_name:
        msg = ' '.join(str(a) for a in args)
        ts_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        with open(_debug_log_name, 'a', encoding='utf-8') as f:
            f.write(f"[{ts_now}] {msg}\n")


def calcula_precisao_gst(lat_erro, lon_erro):
    if lat_erro is None or lon_erro is None:
        return None
    try:
        precisao = math.sqrt(float(lat_erro)**2 + float(lon_erro)**2)
        return "%.3f" % precisao
    except ValueError:
        return None


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
            dprint(f"[{ts()}][GNSS PUBX]{tag} {line}")
            log(line, log_name)
        elif line.startswith('$GNGGA'):
            check_lat = line.split(',')[4]
            timer = time.time() - t0
            if (timer > GGA_INTERVAL or first_gga) and check_lat:
                gga_sentence = line
                if not stop_sock.is_set():
                    try:
                        sock.sendall((gga_sentence + '\r\n').encode('ascii'))
                        dprint(f"[{ts()}][Gateway]{tag} Reenviado GGA: {gga_sentence}")
                        first_gga = False
                    except (OSError, BrokenPipeError, ConnectionResetError) as e:
                        dprint(f"[{ts()}][serial_reader]{tag} Socket fechado ao enviar GGA: {e}")
                        stop_sock.set()
                        break
                else:
                    first_gga = True
                timer = 0
                t0 = time.time()

            parts = line.split(',')
            fix_quality = parts[6] if len(parts) > 6 else '?'
            num_sats    = parts[7] if len(parts) > 7 else '?'
            dprint(f"[{ts()}][GNSS GGA]{tag} fix={fix_quality} sats={num_sats} | {line}")
            log(line, log_name)
        elif '$GNGST' in line:
            parts = line.split(',')
            if len(parts) >= 8:
                try:
                    lat_err = parts[6] or None
                    lon_raw = parts[7].split('*')[0] if len(parts) > 7 else None
                    lon_err = lon_raw or None
                    precisao = calcula_precisao_gst(lat_err, lon_err)
                    if precisao:
                        dprint(f"[{ts()}][PRECISÃO GST]{tag} {precisao} m  (lat_err={lat_err}, lon_err={lon_err})")
                except Exception:
                    pass
            log(line, log_name)
        elif line.startswith('$GN'):
            dprint(f"[{ts()}][GNSS NMEA]{tag} {line}")
            log(line, log_name)

    dprint(f"[{ts()}][serial_reader]{tag} encerrado")


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
    Extrai TODOS os frames RTCM do buffer e envia ao receptor em ordem de chegada.
    """
    frames = []
    crc_errors = 0

    while True:
        frame, consumed = extract_rtcm_frame(buffer)
        if consumed == 0:
            break
        del buffer[:consumed]
        if frame is not None:
            frames.append(frame)
        else:
            crc_errors += 1

    if not frames:
        return

    for frame in frames:
        ser.write(frame)
        ser.flush()

    types_str = ','.join(str(get_rtcm_msg_type(f)) for f in frames)
    total_bytes = sum(len(f) for f in frames)
    dprint(f"[{ts()}][RTCM → GNSS]{tag} Sent {len(frames)} frames ({total_bytes} bytes), tipos: [{types_str}]")

    if crc_errors > 0:
        dprint(f"[{ts()}][process_rtcm]{tag} {crc_errors} frames descartados por CRC inválido")


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
                dprint(f"[{ts()}][rtcm_gateway]{tag} Conexão fechada durante leitura do header")
                stop_event.set()
                return
            hdr += chunk
        if stop_event.is_set():
            dprint(f"[{ts()}][rtcm_gateway]{tag} encerrado antes de iniciar parsing")
            return
        dprint(f"[{ts()}][Gateway]{tag} Header NTRIP recebido, iniciando parser chunked RTCM")
        buffer = bytearray()
        while not stop_event.is_set():
            size_line = f.readline()
            if not size_line:
                dprint(f"[{ts()}][Gateway]{tag} EOF ao ler tamanho do chunk")
                break
            try:
                size = int(size_line.strip(), 16)
            except ValueError:
                dprint(f"[{ts()}][Gateway]{tag} chunk size parse error: {size_line.strip()}")
                continue
            if size == 0:
                dprint(f"[{ts()}][Gateway]{tag} chunk size 0, fim do stream")
                break
            data = f.read(size)
            f.read(2)
            stats['bytes_received'] += len(data)
            buffer.extend(data)
            process_rtcm_buffer(buffer, ser, tag)
    except socket.timeout:
        dprint(f"[{ts()}][Gateway]{tag} TIMEOUT: sem dados RTCM há {SOCKET_TIMEOUT}s — reconectando")
    except (ConnectionResetError, ConnectionAbortedError, OSError) as e:
        dprint(f"[{ts()}][Gateway]{tag} Conexão perdida: {e}")
    finally:
        stop_event.set()
        dprint(f"[{ts()}][rtcm_gateway]{tag} encerrado, stop_event: {stop_event.is_set()}")


def run_gateway(serial_port, caster_port, log_prefix, mount=None):
    global _debug_log_name
    _debug_log_name = make_debug_log_name(log_prefix)

    tag = f" [{log_prefix}]" if log_prefix else ""
    log_name = make_log_name(log_prefix)
    dprint(f"[{ts()}][+]{tag} Debug log: {_debug_log_name}")
    dprint(f"[{ts()}][+]{tag} Log: {log_name}")

    ser = serial.Serial(serial_port, BAUDRATE, timeout=1)
    dprint(f"[{ts()}][+]{tag} Serial aberta em {serial_port}@{BAUDRATE}")

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
            dprint(f"[{ts()}][Gateway]{tag} Conectando ao Caster {ORCH_HOST}:{caster_port} (timeout {CONNECT_TIMEOUT}s)...")
            sock = socket.create_connection((ORCH_HOST, caster_port), timeout=CONNECT_TIMEOUT)

            # TCP Keepalive — detecta conexões mortas rapidamente
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            if sys.platform == 'win32':
                # Windows: (habilitar, intervalo ms, timeout ms)
                sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 10000, 5000))

            dprint(f"[{ts()}][+]{tag} TCP conectado ao Caster em {ORCH_HOST}:{caster_port}")

            # Reset do backoff após conexão bem-sucedida
            reconnect_delay = RECONNECT_BASE

            # Contagem de reconexões
            reconnect_count += 1
            stats['session_start'] = time.time()
            stats['bytes_received'] = 0
            dprint(f"[{ts()}][Stats]{tag} Sessão #{reconnect_count} iniciada")

            # Se mount definido, envia requisição NTRIP com mountpoint
            if mount:
                ntrip_req = f"GET /{mount} HTTP/1.1\r\nHost: {ORCH_HOST}\r\n\r\n"
                sock.sendall(ntrip_req.encode('ascii'))
                dprint(f"[{ts()}][+]{tag} Enviado GET /{mount} ao caster (base fixa)")

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
            dprint(f"[{ts()}][Gateway]{tag} Timeout de conexão ({CONNECT_TIMEOUT}s), reconectando em {reconnect_delay}s...")
            time.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, RECONNECT_MAX)
        except (ConnectionRefusedError, ConnectionResetError, OSError) as e:
            dprint(f"[{ts()}][Gateway]{tag} Erro de conexão: {e}, reconectando em {reconnect_delay}s...")
            time.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, RECONNECT_MAX)
        except Exception as e:
            dprint(f"[{ts()}][Gateway]{tag} Erro inesperado: {e}, reconectando em {reconnect_delay}s...")
            time.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, RECONNECT_MAX)
        finally:
            stop_sock.set()
            if t2:
                t2.join(timeout=2)
            # Estatísticas da sessão encerrada
            if stats['session_start']:
                elapsed = time.time() - stats['session_start']
                dprint(f"[{ts()}][Stats]{tag} Sessão #{reconnect_count} encerrada: "
                      f"{stats['bytes_received']} bytes em {elapsed:.1f}s")
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass
            dprint(f"[{ts()}][*]{tag} Threads encerradas, reiniciando gateway...")


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
