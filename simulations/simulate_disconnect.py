"""
Simulador de desconexão de client NTRIP.

Conecta ao caster, envia GGA, e permite simular diferentes cenários:
  1. Congelamento (para de ler/enviar, conexão TCP aberta)
  2. Kill abrupto (encerra sem enviar FIN)
  3. Desconexão limpa (close TCP normal)

Uso:
  python simulate_disconnect.py [--port 2102] [--mount SPAR0]
"""

import socket
import time
import argparse
import os
import ctypes
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

ORCH_HOST = os.getenv('ORCH_HOST', '127.0.0.1')
GGA_SAMPLE = '$GNGGA,113000.00,2112.1080,S,05026.1600,W,1,12,0.8,400.0,M,-12.0,M,,*5A'


def ts():
    return datetime.now().strftime("%H:%M:%S")


def connect_and_handshake(host, port, mount=None):
    """Conecta ao caster e faz o handshake inicial."""
    print(f"[{ts()}] Conectando a {host}:{port}...")
    sock = socket.create_connection((host, port), timeout=10)
    print(f"[{ts()}] Conectado!")

    if mount:
        # Modo base fixa: envia requisição NTRIP
        req = f"GET /{mount} HTTP/1.1\r\nHost: {host}\r\n\r\n"
        sock.sendall(req.encode('ascii'))
        print(f"[{ts()}] Enviado GET /{mount}")
    else:
        # Modo otimizado: envia GGA como handshake
        sock.sendall((GGA_SAMPLE + '\r\n').encode('ascii'))
        print(f"[{ts()}] Enviado GGA handshake")

    # Lê resposta do caster (ICY 200 OK)
    sock.settimeout(10)
    response = b''
    while b'\r\n\r\n' not in response:
        chunk = sock.recv(1024)
        if not chunk:
            break
        response += chunk
    print(f"[{ts()}] Resposta do caster: {response.decode(errors='ignore').strip()}")

    return sock


def scenario_freeze(sock):
    """Cenário 1: Congela — para de ler dados, conexão fica aberta."""
    print(f"\n[{ts()}] === CENÁRIO: CONGELAMENTO ===")
    print(f"[{ts()}] A conexão TCP está aberta mas o client parou de ler.")
    print(f"[{ts()}] O buffer TCP do OS vai encher e o sendall do caster vai travar.")
    print(f"[{ts()}] Observe no caster quanto tempo leva para detectar (timeout de 10s).")
    print(f"[{ts()}] Pressione Ctrl+C para encerrar este script.\n")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print(f"\n[{ts()}] Script encerrado pelo usuário.")


def scenario_kill(sock):
    """Cenário 2: Mata o processo sem fechar o socket (simula crash)."""
    print(f"\n[{ts()}] === CENÁRIO: KILL ABRUPTO ===")
    print(f"[{ts()}] O processo será finalizado SEM fechar o socket.")
    print(f"[{ts()}] O OS pode ou não enviar RST ao caster.")
    print(f"[{ts()}] Encerrando em 3s...")
    time.sleep(3)
    # No Windows, TerminateProcess mata sem cleanup
    ctypes.windll.kernel32.TerminateProcess(
        ctypes.windll.kernel32.GetCurrentProcess(), 1
    )


def scenario_clean(sock):
    """Cenário 3: Desconexão limpa (close TCP normal)."""
    print(f"\n[{ts()}] === CENÁRIO: DESCONEXÃO LIMPA ===")
    print(f"[{ts()}] Fechando socket normalmente (envia FIN)...")
    sock.close()
    print(f"[{ts()}] Socket fechado. O caster deve detectar instantaneamente.")


def main():
    parser = argparse.ArgumentParser(description='Simulador de desconexão de client NTRIP')
    parser.add_argument('--port', type=int, default=int(os.getenv('ORCH_PORT_TEST', '2102')),
                        help='Porta do caster (default: ORCH_PORT_TEST do .env)')
    parser.add_argument('--mount', type=str, default=None,
                        help='Mountpoint para base fixa (ex: SPAR0). Se omitido, usa modo otimizado.')
    args = parser.parse_args()

    sock = connect_and_handshake(ORCH_HOST, args.port, args.mount)

    # Aguarda um pouco para receber alguns pacotes RTCM
    print(f"\n[{ts()}] Recebendo RTCM por 5 segundos para estabilizar...")
    sock.settimeout(1)
    t0 = time.time()
    bytes_received = 0
    while time.time() - t0 < 5:
        try:
            data = sock.recv(4096)
            if data:
                bytes_received += len(data)
        except socket.timeout:
            pass
    print(f"[{ts()}] Recebidos {bytes_received} bytes de RTCM em 5s.\n")

    # Menu de cenários
    print("=" * 50)
    print("  Escolha o cenário de desconexão:")
    print("=" * 50)
    print("  1. Congelamento (para de ler, conexão aberta)")
    print("  2. Kill abrupto (encerra sem fechar socket)")
    print("  3. Desconexão limpa (close TCP normal)")
    print("=" * 50)

    choice = input("\nOpção [1/2/3]: ").strip()

    if choice == '1':
        scenario_freeze(sock)
    elif choice == '2':
        scenario_kill(sock)
    elif choice == '3':
        scenario_clean(sock)
    else:
        print("Opção inválida. Encerrando.")
        sock.close()


if __name__ == '__main__':
    main()
