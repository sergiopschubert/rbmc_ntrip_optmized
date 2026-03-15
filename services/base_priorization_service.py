
import requests
import time
from geopy.distance import geodesic

class IBGEEndpointClient:
    def __init__(self, url: str, user_location: tuple):
        self.url = url
        self.user_location = user_location

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