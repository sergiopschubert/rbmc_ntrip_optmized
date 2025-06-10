import threading
import serial
import socket
import time
from crccheck.crc import Crc24LteA
from dotenv import load_dotenv
import os
from datetime import datetime

load_dotenv()

# CONFIGURAÇÃO
SERIAL_PORT  = os.getenv('SERIAL_PORT')   # Serial conectada ao GNSS (GGA TX / RTCM3 RX)
BAUDRATE     = 115200                    # Baudrate configurado no GNSS para RTCM3
ORCH_HOST    = os.getenv('ORCH_HOST')     # IP do Orchestrator/relay NTRIP
ORCH_PORT    = int(os.getenv('ORCH_PORT'))# Porta do Orchestrator
GGA_INTERVAL = 60                   # segundos para reenviar GGA
LOG = 'ACTIVE'
LOG_NAME = f"logs/LOG{datetime.now().strftime("%d%m%y-%H%M%S")}.txt"

def log(line):
    if LOG == 'ACTIVE':
        file_name = LOG_NAME
        f = open(file_name, 'a', encoding='utf-8')
        f.write(line+'\n')
        f.close()



def periodic_gga_sender(sock, gga_sentence, stop_event):
    """
    Reenvia a sentença GGA a cada GGA_INTERVAL segundos enquanto não for sinalizado stop.
    """
    while not stop_event.is_set():
        time.sleep(GGA_INTERVAL)
        try:
            sock.sendall((gga_sentence + '\r\n').encode('ascii'))
            print(f"[Gateway] Reenviado GGA: {gga_sentence}")
        except Exception as e:
            print(f"[Gateway] Erro ao reenviar GGA: {e}")
            break


def serial_reader(ser, sock, stop_event,stop_sock):
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
            print(f"[GNSS PUBX] {line}")
            log(line)
        elif line.startswith('$GNGGA'):
            check_lat = line.split(',')[4]
            timer = time.time() - t0 
            if (timer > GGA_INTERVAL or first_gga) and check_lat: 
                gga_sentence = line
                if not stop_sock.is_set():
                    # if first_gga == False:
                    #     gga_sentence = '$GNGGA,131804.00,2290.63642,S,05125.61568,W,5,12,2.18,368.8,M,-5.5,M,,0000*5D'
                    sock.sendall((gga_sentence + '\r\n').encode('ascii'))
                    print(f"[Gateway] Reenviado GGA: {gga_sentence}")
                    first_gga = False
                else:
                    first_gga = True
                timer = 0
                t0 = time.time()
                
            print(f"[GNSS NMEA] {line}")
            log(line)
        elif line.startswith('$GN'):
            print(f"[GNSS NMEA] {line}")
            log(line)    

    print("[serial_reader] encerrado")


def process_rtcm(buffer: bytearray, ser):
    """
    Extrai e envia pacotes RTCM3 completos, validando CRC.
    """
    if len(buffer) < 6:
        return 0
    if buffer[0] != 0xD3:
        idx = buffer.find(0xD3)
        if idx < 0:
            buffer.clear()
            return 0
        del buffer[:idx]
        return 0
    length = ((buffer[1] & 0x03) << 8) | buffer[2]
    frame_len = 3 + length + 3
    if len(buffer) < frame_len:
        return 0
    frame = bytes(buffer[:frame_len])
    calc_crc = Crc24LteA.calc(frame[:-3]).to_bytes(3, 'big')
    recv_crc = frame[-3:]
    if calc_crc == recv_crc:
        ser.write(frame)
        ser.flush()
        print(f"[RTCM → GNSS] Sent frame {frame_len} bytes, header {frame[:6].hex()}…")
    else:
        print(f"[process_rtcm] CRC mismatch, discarding {frame_len} bytes")
    return frame_len


def rtcm_gateway(ser, sock, stop_event):
    """
    Parse de HTTP chunked e retransmissão de RTCM3.
    """
    f = sock.makefile('rb')
    hdr = b''
    while not stop_event.is_set() and b'\r\n\r\n' not in hdr:
        hdr += f.read(1)
    if stop_event.is_set():
        print("[rtcm_gateway] encerrado antes de iniciar parsing")
        return
    print("[Gateway] Header NTRIP recebido, iniciando parser chunked RTCM")
    buffer = bytearray()
    while not stop_event.is_set():
        size_line = f.readline()
        if not size_line:
            print("[Gateway] EOF ao ler tamanho do chunk")
            break
        try:
            size = int(size_line.strip(), 16)
        except ValueError:
            print(f"[Gateway] chunk size parse error: {size_line.strip()}")
            continue
        if size == 0:
            print("[Gateway] chunk size 0, fim do stream")
            break
        data = f.read(size)
        f.read(2)
        buffer.extend(data)
        while True:
            consumed = process_rtcm(buffer, ser)
            if consumed:
                del buffer[:consumed]
            else:
                break
    print(f"[rtcm_gateway] encerrado, stop_event: {stop_event.is_set()}")


def run_gateway():
    ser = serial.Serial(SERIAL_PORT, BAUDRATE, timeout=1)
    print(f"[+] Serial aberta em {SERIAL_PORT}@{BAUDRATE}")
    t1 = None
    stop_sock = threading.Event()
    stop_read_serial = threading.Event()
    sock = None
    while True:
        try:
            
            sock = socket.create_connection((ORCH_HOST, ORCH_PORT), timeout=60)
            print(f"[+] TCP conectado ao Caster em {ORCH_HOST}:{ORCH_PORT}")
            
            if t1:
                stop_read_serial.set()
                t1.join(timeout=1)
                t2 = None
                t1 = None
            stop_sock.clear()
            stop_read_serial.clear()
            t1 = threading.Thread(target=serial_reader, args=(ser, sock, stop_read_serial,stop_sock), daemon=True)
            t1.start()
            
            t2 = threading.Thread(target=rtcm_gateway,  args=(ser, sock, stop_sock), daemon=True)
            t2.start()
            while not stop_sock.is_set() and t1.is_alive() and t2.is_alive():
                time.sleep(0.2)
        except Exception as e:
            print(f"[Gateway] erro: {e}, reconectando em 5s...")
            time.sleep(5)
        finally:
            stop_sock.set()
            if t2:
                t2.join(timeout=1)
            print("[*] Threads encerradas, reiniciando gateway...")
            

def main():
    run_gateway()

if __name__ == '__main__':
    main()
