"""Street point for each located parcel, version 2 (drop-in for streetview.py, same points() signature).

The point is the spot on the public road in front of the lot plus the compass heading from that spot toward the
lot. The dashboard turns it into a live Google Street View embed; no imagery is downloaded or stored.

What changed against streetview.py (measured in agent_reports/streetview_embed.md):
  * Local AND secondary roads (US / state highways) are always read together. v1 stopped at the first local road
    it found, so a lot addressed on a highway got a side street or an apartment driveway instead.
  * Road class matters. Ramps, walkways and bike paths are never used; private drives, parking-lot aisles, alleys
    and unnamed segments are a last resort (Google has almost no Street View on them); limited-access highways are
    used only when nothing else is within reach.
  * Street-name matching is tolerant: MT / MOUNT, RIDGECREEK / RIDGE CREEK, TRACE / TRCE, "HIGHWAY 85" / "State
    Rte 85", a city and ZIP typed after the street, unit and lot numbers, and one-letter spelling slips.
  * A road with the right name is looked for out to 450 m before a wrong-named nearer road is accepted, and big
    rural tracts are searched out to 1200 m instead of being left without a point.
  * A cached point is recomputed when the parcel's own coordinates change (v1 kept the old point for ever).
  * Optional, off by default: if the environment variable GOOGLE_MAPS_KEY is set, each point is checked against
    Google's official Street View metadata service (free of charge, needs the owner's own key) and parcels with no
    panorama nearby are left out, so the page never shows an empty Street View tile.

Roads come from the Census Bureau's TIGERweb service (public domain).

    points(slug, parcels, cache, limit=None) -> {parcel_id: {"lat":..,"lon":..,"heading":..,"road":..,"m":..,
                                                             "cls": road class, "match": name matched,
                                                             "alt": {same keys} second road, when there is one}}
cache key f"sv2|{slug}|{parcel_id}" -> the dict, or {"at": [lat, lon]} for 'checked, no road nearby'.
"""
import difflib
import json
import math
import os
import re
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

TIGER = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Transportation/MapServer/%d/query"
LOCAL, SECONDARY, PRIMARY = 8, 6, 2
UA = "Mozilla/5.0 (compatible; AtlantaTaxSaleDashboard/1.0)"
GSV_META = "https://maps.googleapis.com/maps/api/streetview/metadata"
RADIUS = (100, 300)  # metres, smallest and largest search circle; keep equal to SV_R in app.js
AIM_MAX = 30         # metres from the road toward the lot; keep equal to the value in app.js
RECHECK_DAYS = 25   # Google allows coordinates from its services to be kept for 30 days at most

# spelled-out word -> the form TIGER uses
NORM = {"STREET": "ST", "ROAD": "RD", "DRIVE": "DR", "AVENUE": "AVE", "AV": "AVE", "LANE": "LN", "COURT": "CT",
        "CIRCLE": "CIR", "BOULEVARD": "BLVD", "HIGHWAY": "HWY", "HIWAY": "HWY", "PARKWAY": "PKWY", "PKY": "PKWY",
        "PLACE": "PL", "TRAIL": "TRL", "TERRACE": "TER", "TERR": "TER", "TRACE": "TRCE", "TRC": "TRCE", "NORTH": "N",
        "SOUTH": "S", "EAST": "E", "WEST": "W", "NORTHEAST": "NE", "NORTHWEST": "NW", "SOUTHEAST": "SE",
        "SOUTHWEST": "SW", "KNOLL": "KNL", "POINT": "PT", "POINTE": "PT", "COVE": "CV", "CROSSING": "XING",
        "SQUARE": "SQ", "MT": "MOUNT", "FT": "FORT", "INDUSTRIAL": "IND", "ROUTE": "RTE", "MLK": "MARTINLUTHERKING"}
TYPES = {"ST", "RD", "DR", "AVE", "LN", "CT", "CIR", "BLVD", "HWY", "PKWY", "PL", "TRL", "TER", "WAY", "TRCE"}
DROP = TYPES | {"N", "S", "E", "W", "NE", "NW", "SE", "SW", "R", "REAR", "US", "U", "STATE", "RTE", "SR", "GA", "JR"}

# TIGER road classes (MTFCC). Penalty in metres added to the distance when roads are compared.
NEVER = {"S1630", "S1710", "S1720", "S1820", "S1830"}          # ramps, walkways, stairways, bike and bridle paths
PENALTY = {"S1400": 0, "S1200": 0, "S1640": 40, "S1100": 250,   # local, secondary, service drive, limited access
           "S1730": 200, "S1740": 200, "S1750": 200, "S1780": 200, "S1500": 200}  # alley, private, internal, parking, 4WD
UNNAMED = 120
MISMATCH = 300        # address has a house number: the named street wins unless it is 300 m farther
MISMATCH_LOOSE = 100  # no house number: the named street wins only when it is at most 100 m farther


def _tokens(text):
    out = []
    for x in re.findall(r"[A-Z0-9]+", str(text or "").upper()):
        out.append(NORM.get(x, x))
    return out


def _core(tokens):
    return [x for x in tokens if x not in DROP]


def _addr_tokens(address):
    """Street part of a parcel address as normalised tokens: house number, unit letter and city dropped."""
    a = str(address or "").upper().split(",")[0]
    a = re.sub(r"^\s*\d+(\s*-\s*\d+)?[A-Z]?\s+", "", a)
    t = _tokens(a)
    if len(t) > 2 and len(t[0]) == 1 and t[0].isalpha() and t[0] not in DROP:    # "302 D PLEASANT VALLEY RD"
        t = t[1:]
    if len(t) > 1 and t[0].isdigit() and t[1].isalpha() and t[1] not in ("TH", "ND", "HWY", "RTE") and t[1] not in TYPES:
        t = t[1:]                                                                   # "756 6 SHADOWRIDGE DR"
    return t


def _match(addr, road):
    """0 = different street, 1 = probably the same street, 2 = same street. addr / road are token lists."""
    A, R = _core(addr), _core(road)
    if not A or not R:
        return 0
    if A[:len(R)] == R:
        return 2
    jr, bounds, s = "".join(R), set(), ""
    for x in A:
        s += x
        bounds.add(len(s))
    if s.startswith(jr) and len(jr) in bounds:       # RIDGECREEK = RIDGE CREEK, with or without a city after it
        return 2
    if set(A) <= set(R):
        return 1
    if len(jr) >= 5 and not any(c.isdigit() for c in jr):
        s = ""
        for x in A[:len(R) + 1]:
            s += x
            if abs(len(s) - len(jr)) <= 2 and difflib.SequenceMatcher(None, jr, s).ratio() >= 0.88:
                return 1                             # GREENE / GREEN, GOLVIEW / GOLFVIEW
    return 0


def _get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def _roads(lat, lon, dist, layers):
    out = []
    for layer in layers:
        q = urllib.parse.urlencode({"geometry": f"{lon},{lat}", "geometryType": "esriGeometryPoint", "inSR": 4326, "outSR": 4326,
                                    "spatialRel": "esriSpatialRelIntersects", "distance": dist, "units": "esriSRUnit_Meter",
                                    "outFields": "NAME,MTFCC", "returnGeometry": "true", "f": "json"})
        d = _get(TIGER % layer + "?" + q)
        if "error" in d:
            raise RuntimeError(str(d["error"])[:100])
        for f in d.get("features", []):
            a = f.get("attributes") or {}
            for path in (f.get("geometry") or {}).get("paths", []):
                if len(path) > 1:
                    out.append((a.get("NAME") or "", a.get("MTFCC") or "", path))
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


def _rank(lat, lon, roads, addr, numbered):
    """Candidate roads for the lot, best first: (score, metres, plat, plon, name, mtfcc, match, clean)."""
    atypes, named, rows = set(addr) & TYPES, bool(_core(addr)), {}
    for name, cls, path in roads:
        if cls in NEVER:
            continue
        pr = _project(lat, lon, path)
        if pr and (name, cls) in rows and rows[(name, cls)][0] <= pr:
            continue
        if pr:
            rt = _tokens(name)
            rows[(name, cls)] = (pr, rt, _match(addr, rt) if name else 0)
    # "Oakland Falls Ct" must not lose to a nearer "Oakland Dr": only the longest full match counts as the street
    longest = max([len(_core(rt)) for _, rt, m in rows.values() if m == 2], default=0)
    out = []
    for (name, cls), (pr, rt, m) in rows.items():
        if m == 2 and len(_core(rt)) < longest:
            m = 0
        pen = PENALTY.get(cls, 200) + (0 if name else UNNAMED)
        score = pr[0] + pen
        if named:
            rtype = next((t for t in reversed(rt) if t in TYPES), None)
            if m and rtype and atypes and rtype not in atypes:
                m, score = 1, score + 110            # Monticello Ct when the address says Monticello Dr
            # a house number means the lot fronts that street; "0 MAIN ST" or "MAIN ST" is only a rough locator
            score += 0 if m == 2 else 40 if m == 1 else (MISMATCH if numbered else MISMATCH_LOOSE)
        out.append((score, pr[0], pr[1], pr[2], name, cls, m, pen == 0))
    return sorted(out)


def _point(lat, lon, c):
    _, d, plat, plon, name, cls, m, _ = c
    out = {"lat": round(plat, 6), "lon": round(plon, 6), "road": name, "m": round(d), "cls": cls, "match": bool(m)}
    if d > 3:
        out["heading"] = _heading(plat, plon, lat, lon)
    return out


def _one(p):
    lat, lon = p["lat"], p["lon"]
    addr = _addr_tokens(p.get("address"))
    named = bool(_core(addr))
    numbered = bool(re.match(r"\s*0*[1-9]\d*", str(p.get("address") or "")))
    cands = []
    for dist in (160, 450, 1200):
        cands = _rank(lat, lon, _roads(lat, lon, dist, (LOCAL, SECONDARY)), addr, numbered)
        if cands and cands[0][7] and (cands[0][6] == 2 or not named):
            break                                    # a public road with the right name: done
        if cands and dist >= 450:
            break
    if not cands or not cands[0][7]:
        cands = sorted(cands + _rank(lat, lon, _roads(lat, lon, 450, (PRIMARY,)), addr, numbered))
    if not cands:
        return {"at": [lat, lon]}
    best = cands[0]
    out = _point(lat, lon, best)
    out["at"] = [lat, lon]
    # the other reasonable answer: the nearest ordinary public road when the address street won, or the address
    # street when the nearest road won. Offered to the page as a second view; never replaces the first.
    clean = [c for c in cands if c[7] and c is not best]
    other = [c for c in clean if c[6] == 2] if not best[6] else sorted(clean, key=lambda c: c[1])
    for c in other[:1]:
        k = math.cos(math.radians(lat)) * 111320.0
        if c[1] <= 450 and math.hypot((c[3] - best[3]) * k, (c[2] - best[2]) * 110540.0) > 60 and (not best[6] or c[1] < best[1] - 40):
            out["alt"] = _point(lat, lon, c)
    return out


def aim(pt):
    """The coordinate the page hands to Google: up to AIM_MAX metres from the road point toward the lot. Google
    picks the panorama nearest to it and turns the camera to face it. Mirrors svAim() in app.js."""
    if pt.get("heading") is None:
        return pt["lat"], pt["lon"]
    d = min(AIM_MAX, max(8, pt.get("m") or AIM_MAX))
    r = math.radians(pt["heading"])
    return (round(pt["lat"] + d * math.cos(r) / 110540.0, 6),
            round(pt["lon"] + d * math.sin(r) / (111320.0 * math.cos(math.radians(pt["lat"]))), 6))


def radius(pt):
    """Search circle around the aim point: 100 m for an ordinary lot, wider for a big tract (its frontage is long
    and rural panoramas are far apart), never more than 300 m. Mirrors svRadius() in app.js."""
    return int(min(RADIUS[1], max(RADIUS[0], (pt.get("m") or 0) + 60)))


def embed_url(pt, r=None):
    """Key-free Street View embed for a point from points(). Same string svUrl() builds in app.js."""
    la, lo = aim(pt)
    return f"https://www.google.com/maps/embed?pb=!6m7!1m6!2m2!1d{la}!2d{lo}!5f1!6d{r or radius(pt)}!7e1"


def _verify(pt, key):
    """True / False from Google's Street View metadata service, None when it could not be asked."""
    la, lo = aim(pt)
    q = urllib.parse.urlencode({"location": f"{la},{lo}", "radius": radius(pt), "source": "outdoor", "key": key})
    try:
        d = _get(GSV_META + "?" + q, timeout=20)
    except Exception:
        return None
    s = d.get("status")
    return True if s == "OK" else False if s in ("ZERO_RESULTS", "NOT_FOUND") else None


def _same(a, b):
    return bool(a) and abs(a[0] - b[0]) < 2e-5 and abs(a[1] - b[1]) < 2e-5


def points(slug, parcels, cache, limit=None):
    key = os.environ.get("GOOGLE_MAPS_KEY", "").strip()
    today = int(time.time() // 86400)
    out, todo, check = {}, [], []
    for p in parcels:
        if not p.get("lat"):
            continue
        c = cache.get(f"sv2|{slug}|{p['parcel']}")
        if c is not None and _same(c.get("at"), (p["lat"], p["lon"])):
            if "lat" in c:
                out[p["parcel"]] = c
                if key and today - c.get("gsvDay", -999) >= RECHECK_DAYS:
                    check.append(c)
        else:
            todo.append(p)
    if limit is not None:
        todo = todo[:limit]

    def work(p):
        for _ in range(2):
            try:
                return p, _one(p)
            except Exception:
                time.sleep(1)
        return p, None

    if todo:
        with ThreadPoolExecutor(6) as ex:
            for p, r in ex.map(work, todo):
                if r is None:
                    continue
                cache[f"sv2|{slug}|{p['parcel']}"] = r
                if "lat" in r:
                    out[p["parcel"]] = r
                    if key:
                        check.append(r)
    if key and check:
        denied = 0
        for c in check:
            ok = _verify(c, key)
            if ok is None:
                denied += 1
                if denied >= 5:                      # bad key or quota: stop asking, keep every point
                    break
                continue
            c["gsv"], c["gsvDay"] = ok, today
            time.sleep(0.05)
    if key:
        out = {k: v for k, v in out.items() if v.get("gsv") is not False}
    return out


if __name__ == "__main__":
    import sys
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
        print(k, ps[k].get("address"), v, embed_url(v))
