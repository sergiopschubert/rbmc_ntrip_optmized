
import requests
from geopy.distance import geodesic

class IBGEEndpointClient:
    def __init__(self, url: str, user_location: tuple):
        self.url = url
        self.user_location = user_location

    def fetch_active_bases(self) -> list:
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

    def prioritize(self, bases: list) -> list:
        for b in bases:
            b['distance_km'] = geodesic(self.user_location, (b['lat'], b['lon'])).km
        ordered = sorted(bases, key=lambda x: x['distance_km'])
        return ordered[:2]