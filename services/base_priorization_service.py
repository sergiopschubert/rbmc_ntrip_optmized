
import requests
import time
from geopy.distance import geodesic

# Bases conhecidas que podem não aparecer no endpoint do IBGE mas estão ativas no RBMC.
MANUAL_BASES = [
    {
        'id': 'SPAR0',
        'lat': -21.184919300575782,
        'lon': -50.43704095708879,
    },
]

class IBGEEndpointClient:
    def __init__(self, url: str, user_location: tuple):
        self.url = url
        self.user_location = user_location

    def _inject_manual_bases(self, bases: list) -> list:
        existing_ids = {b['id'] for b in bases}
        for manual in MANUAL_BASES:
            if manual['id'] not in existing_ids:
                bases.append(manual)
                print(f"[IBGE] Base manual injetada: {manual['id']} (não encontrada no endpoint)")
        return bases

    def fetch_active_bases(self) -> list:
        """Busca bases ativas do RBMC com retry e backoff (3 tentativas)."""
        last_error = None
        for attempt in range(3):
            try:
                resp = requests.get(self.url, timeout=10)
                resp.raise_for_status()
                bases = []
                for line in resp.text.splitlines():
                    if not line.startswith('STR;'): continue
                    parts = line.split(';')
                    try:
                        mount = parts[1]
                        lat = float(parts[9]); lon = float(parts[10])
                    except: continue
                    bases.append({'id': mount, 'lat': lat, 'lon': lon})
                bases = self._inject_manual_bases(bases)
                return bases
            except (requests.RequestException, requests.Timeout) as e:
                last_error = e
                print(f"[IBGE] Tentativa {attempt+1}/3 falhou: {e}")
                if attempt < 2:
                    time.sleep(2 ** attempt)
        raise ConnectionError(f"Não foi possível acessar RBMC após 3 tentativas: {last_error}")

    def prioritize(self, bases: list) -> list:
        for b in bases:
            b['distance_km'] = geodesic(self.user_location, (b['lat'], b['lon'])).km
        ordered = sorted(bases, key=lambda x: x['distance_km'])
        return ordered[:2]