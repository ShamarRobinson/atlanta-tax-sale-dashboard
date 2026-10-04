"""Street point for each located parcel: the spot on the nearest public road in front of the lot, plus the
compass heading from that spot toward the lot. The dashboard uses it to open a live Google Street View that
faces the property (no imagery is downloaded or stored).

Roads come from the Census Bureau's TIGERweb service (public domain). For a parcel placed from the county
parcel map, the centroid is projected onto the nearest road, preferring a road whose name matches the
parcel's street address.

    points(slug, parcels, cache, limit=None) -> {parcel_id: {"lat":..,"lon":..,"heading":..,"road":..}}
cache key f"sv|{slug}|{parcel_id}" -> the dict, or {} for 'checked, no road nearby'.
"""
import json
import math
import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

TIGER = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Transportation/MapServer/%d/query"
LAYERS = (8, 6, 2)   # local roads, secondary roads, primary roads
UA = "Mozilla/5.0 (compatible; AtlantaTaxSaleDashboard/1.0)"
SUFFIX = {"STREET": "ST", "ROAD": "RD", "DRIVE": "DR", "AVENUE": "AVE", "LANE": "LN", "COURT": "CT", "CIRCLE": "CIR",
          "BOULEVARD": "BLVD", "HIGHWAY": "HWY", "PARKWAY": "PKWY", "PLACE": "PL", "TRAIL": "TRL", "TERRACE": "TER",
          "WAY": "WAY", "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W", "NORTHEAST": "NE", "NORTHWEST": "NW",
          "SOUTHEAST": "SE", "SOUTHWEST": "SW"}
DROP = {"ST", "RD", "DR", "AVE", "LN", "CT", "CIR", "BLVD", "HWY", "PKWY", "PL", "TRL", "TER", "WAY", "N", "S", "E", "W",
        "NE", "NW", "SE", "SW", "R", "REAR"}


def _words(name):
    w = [SUFFIX.get(x, x) for x in re.findall(r"[A-Z0-9]+", str(name or "").upper())]
    return [x for x in w if x not in DROP]


def _street_words(address):
    a = str(address or "").split(",")[0]
    a = re.sub(r"^\s*\d+[A-Z]?\s+", "", a.upper())
    return _words(a)


def _roads(lat, lon, dist):
    out = []
    for layer in LAYERS:
        q = urllib.parse.urlencode({"geometry": f"{lon},{lat}", "geometryType": "esriGeometryPoint", "inSR": 4326, "outSR": 4326,
                                    "spatialRel": "esriSpatialRelIntersects", "distance": dist, "units": "esriSRUnit_Meter",
                                    "outFields": "NAME", "returnGeometry": "true", "f": "json"})
        req = urllib.request.Request(TIGER % layer + "?" + q, headers={"User-Agent": UA})
        d = json.loads(urllib.request.urlopen(req, timeout=30).read())
        if "error" in d:
            raise RuntimeError(str(d["error"])[:100])
        for f in d.get("features", []):
            for path in (f.get("geometry") or {}).get("paths", []):
                out.append(((f.get("attributes") or {}).get("NAME") or "", path))
        if out and layer == LAYERS[0]:
            break
    return out


def _project(lat, lon, path):
    """Nearest point on a polyline to (lat, lon); returns (metres, plat, plon)."""
    k = math.cos(math.radians(lat)) * 111320.0
    best = None
    for (x1, y1), (x2, y2) in zip(path, path[1:]):
        ax, ay, bx, by = (x1 - lon) * k, (y1 - lat) * 110540.0, (x2 - lon) * k, (y2 - lat) * 110540.0
        dx, dy = bx - ax, by - ay
        L = dx * dx + dy * dy
        t = 0 if L == 0 else max(0, min(1, -(ax * dx + ay * dy) / L))
        px, py = ax + t * dx, ay + t * dy
        d = math.hypot(px, py)
        if best is None or d < best[0]:
            best = (d, lat + py / 110540.0, lon + px / k)
    return best


def _heading(lat1, lon1, lat2, lon2):
    y = math.sin(math.radians(lon2 - lon1)) * math.cos(math.radians(lat2))
    x = math.cos(math.radians(lat1)) * math.sin(math.radians(lat2)) - math.sin(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.cos(math.radians(lon2 - lon1))
    return round((math.degrees(math.atan2(y, x)) + 360) % 360)


def _one(p):
    lat, lon = p["lat"], p["lon"]
    want = _street_words(p.get("address"))
    for dist in (120, 400):
        roads = _roads(lat, lon, dist)
        cands = []
        for name, path in roads:
            if len(path) < 2:
                continue
            pr = _project(lat, lon, path)
            if pr:
                match = bool(want) and set(want) <= set(_words(name))
                cands.append((not match, pr[0], pr[1], pr[2], name))
        if cands:
            cands.sort()
            _, d, plat, plon, name = cands[0]
            out = {"lat": round(plat, 6), "lon": round(plon, 6), "road": name, "m": round(d)}
            if d > 6:
                out["heading"] = _heading(plat, plon, lat, lon)
            return out
    return {}


def points(slug, parcels, cache, limit=None):
    out, todo = {}, []
    for p in parcels:
        if not p.get("lat"):
            continue
        k = f"sv|{slug}|{p['parcel']}"
        if k in cache:
            if cache[k]:
                out[p["parcel"]] = cache[k]
        else:
            todo.append(p)
    if limit is not None:
        todo = todo[:limit]

    def work(p):
        for _ in range(2):
            try:
                return p, _one(p)
            except Exception:
                pass
        return p, None

    if todo:
        with ThreadPoolExecutor(6) as ex:
            for p, r in ex.map(work, todo):
                if r is None:
                    continue
                cache[f"sv|{slug}|{p['parcel']}"] = r
                if r:
                    out[p["parcel"]] = r
    return out


if __name__ == "__main__":
    import sys
    import time
    from pathlib import Path
    d = json.loads((Path(__file__).parent / "data" / (sys.argv[1] + ".json")).read_text())
    ps = {}
    for a in d["auctions"]:
        for p in a["parcels"]:
            ps.setdefault(p["parcel"], p)
    c = {}
    t = time.time()
    r = points(sys.argv[1], list(ps.values()), c, int(sys.argv[2]) if len(sys.argv) > 2 else None)
    print(len(ps), len(r), round(time.time() - t), "s")
    for k, v in list(r.items())[:8]:
        print(k, ps[k].get("address"), v)
