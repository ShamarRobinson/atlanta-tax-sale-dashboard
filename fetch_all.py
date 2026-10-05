#!/usr/bin/env python3
"""Fetch every Atlanta-area county's tax sale data and write data/<slug>.json plus data/index.json.

Counties overwrite their files, so each run merges with what was saved before: an auction seen once stays in the
archive even after the county takes the file down. A county that fails to load keeps its previous data.
"""
import csv
import io
import json
import re
import sys
import time
import traceback
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from registry import COUNTIES
import parcel_geo
import prices_metro as PM
import results_outer as RO
import photos_metro
import photos_outer
import streetview
import streetview2

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
DATA.mkdir(exist_ok=True)
CACHE = DATA / "geocache.json"
PCACHE = DATA / "pricecache.json"
GCACHE = DATA / "parcelgeo.json"
ACACHE = DATA / "areacache.json"
ZCACHE = DATA / "zipcache.json"
SVCACHE = DATA / "svcache.json"
PHOTOS = DATA / "photos"
PH_METRO, PH_OUTER = PHOTOS / "metro_cache.json", PHOTOS / "outer_cache.json"
try:
    AUCTION_INFO = json.loads((HERE / "auction_info.json").read_text())
except Exception:
    AUCTION_INFO = {}
if not (HERE / "region.json").exists():
    import build_region
    build_region.main()
REGION = json.loads((HERE / "region.json").read_text())
META = {c["name"]: c for c in REGION["counties"] if c["state"] == "GA"}


def slug(n):
    return re.sub(r"[^a-z0-9]+", "-", n.lower()).strip("-")


def merge(old, new):
    """Keep prior auctions; a fresh copy replaces the same (date, kind)."""
    by = {(a["date"], a["kind"]): a for a in old}
    for a in new:
        by[(a["date"], a["kind"])] = a
    return sorted(by.values(), key=lambda a: (a["date"], a["kind"]), reverse=True)


# Extra sources of sold prices per county: (functions returning auctions, kinds of previously saved auctions to drop first)
EXTRA = {
    "fayette": ([RO.fayette_results], {"excess"}),
    "carroll": ([RO.carroll_results, RO.carroll_excess_prices], {"excess"}),
    "meriwether": ([RO.meriwether_excess], set()),
    "walton": ([RO.walton_excess], set()),
    "troup": ([RO.troup_derived], {"excess"}),
    "cobb": ([PM.cobb_tax_deeds], set()),
}
PRICE_FIELDS = ("winningBid", "buyer", "excess", "priceSource")


def pkey(p):
    return re.sub(r"[^0-9A-Z]", "", str(p.get("parcel", "")).upper())


def fold(auctions):
    """One record per parcel per sale date: fold excess-list rows into the results/list row for the same date,
    carrying the sold price, buyer and excess across. Excess rows with no counterpart stay where they are."""
    by_date = {}
    for a in auctions:
        by_date.setdefault(a["date"], []).append(a)
    out = []
    for d, group in by_date.items():
        main = [a for a in group if a["kind"] != "excess"]
        idx = {}
        for a in sorted(main, key=lambda a: a["kind"] != "results"):
            for p in a["parcels"]:
                idx.setdefault(pkey(p), p)
        for a in group:
            if a["kind"] != "excess":
                continue
            keep = []
            for p in a["parcels"]:
                m = idx.get(pkey(p))
                if not m:
                    keep.append(p)
                    continue
                for k in PRICE_FIELDS:
                    if p.get(k) and not m.get(k):
                        m[k] = p[k]
                if not m.get("winningBid") and m.get("minBid") and m.get("excess"):
                    m["winningBid"] = round(m["minBid"] + m["excess"], 2)
                    m["priceSource"] = "Derived: amount owed + excess funds"
                for k in ("owner", "address", "minBid"):
                    if p.get(k) and not m.get(k):
                        m[k] = p[k]
                if m.get("status") in (None, "Listed"):
                    m["status"] = "Sold"
            a["parcels"] = keep
        out.extend(a for a in group if a["parcels"])
    return sorted(out, key=lambda a: (a["date"], a["kind"]), reverse=True)


def _place(lat, lon):
    u = ("https://geocoding.geo.census.gov/geocoder/geographies/coordinates?x=%s&y=%s&benchmark=Public_AR_Current&vintage=Current_Current"
         "&layers=Incorporated%%20Places,County%%20Subdivisions&format=json" % (lon, lat))
    for _ in range(2):
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
            g = json.loads(urllib.request.urlopen(req, timeout=30).read())["result"]["geographies"]
            pl = g.get("Incorporated Places") or []
            if pl:
                return re.sub(r"\s+(city|town|village|CDP|\(balance\)|consolidated government.*|unified government.*)$", "", pl[0]["NAME"]).strip()
            cs = g.get("County Subdivisions") or []
            if cs:
                return re.sub(r"\s+CCD$", "", cs[0]["NAME"]).strip() + " (unincorporated)"
            return ""
        except Exception:
            time.sleep(1)
    return None


def fill_areas(auctions, acache):
    """City for parcels that have coordinates but no area: the incorporated place the point falls in, else the census county division."""
    from concurrent.futures import ThreadPoolExecutor
    need = {}
    for a in auctions:
        for p in a["parcels"]:
            if p.get("lat") and not p.get("area"):
                need.setdefault(f"{p['lat']:.4f},{p['lon']:.4f}", (p["lat"], p["lon"]))
    todo = [(k, v) for k, v in need.items() if k not in acache]
    if todo:
        with ThreadPoolExecutor(6) as ex:
            for (k, _), r in zip(todo, ex.map(lambda kv: _place(*kv[1]), todo)):
                if r is not None:
                    acache[k] = r
    n = 0
    for a in auctions:
        for p in a["parcels"]:
            if p.get("lat") and not p.get("area"):
                r = acache.get(f"{p['lat']:.4f},{p['lon']:.4f}")
                if r:
                    p["area"] = FIX.get(r, r)
                    n += 1
    return n


UP = {"NE", "NW", "SE", "SW", "N", "S", "E", "W", "US", "GA", "II", "III", "IV", "PO", "LLC"}


def tc(s):
    """Title case for a street or city written in capitals (keeps NE/SW style directions, fixes Mc names)."""
    out = []
    for w in str(s or "").split():
        u = w.upper().strip(".,")
        if u in UP:
            out.append(w.upper())
        elif re.match(r"^\d+(ST|ND|RD|TH)$", u):
            out.append(w.lower())
        elif re.match(r"^MC[A-Z]{2,}", u):
            out.append("Mc" + w[2:].capitalize())
        else:
            out.append(w if (w != w.upper() and w != w.lower()) else w.capitalize())
    return " ".join(out)


NOT_STREET = re.compile(r"\b(LL|LLS|LAND LOTS?|DIST|DISTRICT|LOTS?|LT|LTS|AC|ACRES?|BLK|BLOCK|TRACT|PB|SEC|SECT|SECTION|UNIT PH|SUBD?|S/D|PHASE)\b\.?\s*[#\d&]"
                        r"|\b(DIST|LLS?|LTS?|BLK)\d|\d+(\.\d+)?\s*AC\b|@|/", re.I)
STYPES = ("RD|ROAD|ST|STREET|AVE|AVENUE|AV|DR|DRIVE|LN|LANE|CT|COURT|CIR|CIRCLE|WAY|TRL|TRAIL|PL|PLACE|BLVD|BOULEVARD|PKWY|PARKWAY|HWY|HIGHWAY|TER|TERR|TERRACE"
          "|PT|POINT|RUN|PASS|PATH|XING|CROSSING|LOOP|ROW|WALK|CV|COVE|SQ|SQUARE|RDG|RIDGE|BND|BEND|TRCE|TRACE|CHASE|CONNECTOR|EXT|LANDING|HOLLOW")
NUMBERED = re.compile(r"^[1-9]\d*[A-Z]?\s+\S+")
try:
    _CZ = json.loads((HERE / "county_zips.json").read_text())
except Exception:
    _CZ = {}
ZIPS, ZIPCITY = _CZ.get("zips", {}), _CZ.get("city", {})


def split_addr(raw):
    """(street, city, zip, legal) from the county's own address text. `legal` marks land lot / lot number / intersection
    wording that is not a plain street address."""
    s = re.sub(r"\s+", " ", re.sub(r"\*.*$", "", str(raw or ""))).strip(" ,")
    if not s:
        return None, None, None, False
    city = zp = None
    m = re.match(r"^(.*?)[\s,]+GA\.?[\s,]*(\d{5})(?:-\d{4})?$", s, re.I)
    if m:   # the county wrote "... CITY GA 30125" after the street
        s, zp = m.group(1).strip(" ,"), m.group(2)
    else:
        m = re.match(r"^(.*\S)\s*,\s*GA\.?$", s, re.I)
        if m:
            s = m.group(1).strip(" ,")
    parts = [x.strip() for x in s.split(",") if x.strip()]
    if len(parts) >= 2 and re.match(r"^[A-Za-z .'-]{2,30}$", parts[-1]) and parts[-1].upper() not in ("GA", "REAR", "OFF"):
        city, s = parts[-1], ", ".join(parts[:-1])
    elif zp:    # no comma before the city: split after the street type word
        m = re.match(r"^(\d.*\b(?:%s)\b\.?(?:\s+(?:N|S|E|W|NE|NW|SE|SW))?)\s+([A-Za-z][A-Za-z .'-]{2,29})$" % STYPES, s, re.I)
        if m:
            s, city = m.group(1), m.group(2)
    if city:
        city = HENRY.get(city.upper(), city)
    legal = bool(NOT_STREET.search(s)) or len(s) > 60 or not re.search(r"[A-Za-z]{2,}", s)
    return s, city, zp, legal


def city_zip(p, city=None, zp=None):
    """(city, zip, ", City, GA 30000") for a parcel: the city named with the address, else the incorporated place, else the
    postal city of the ZIP code, else the census county division."""
    zp = zp or p.get("zip")
    area = p.get("area") or ""
    if not city and area and "(unincorporated)" not in area:
        city = area
    if not city and zp and ZIPCITY.get(zp):
        city = ZIPCITY[zp]
    if not city and area:
        city = re.sub(r"\s*\(unincorporated\)", "", area)
    if city:
        city = FIX.get(tc(city), tc(city))
    return city, zp, (", " + city if city else "") + ", GA" + (" " + zp if zp else "")


def full_address(p, g):
    """Address with city, state and ZIP: "Street, City, GA 30000". Returns (text, numbered). Land lot wording and other
    text that is not a street address is kept as written and gets the city and ZIP added when they are known
    (numbered is False for those, so map links use the parcel's coordinates)."""
    st, city, zp, legal = split_addr(p.get("address"))
    if not st or re.search(r"\b(?!GA\b)[A-Z]{2}\.?,?\s+\d{5}(-\d{4})?$", st):
        return None, False      # nothing usable, or a mailing address in another state
    if g and g.get("matched"):
        parts = [x.strip() for x in g["matched"].split(",")]
        if len(parts) >= 4:
            city, zp = parts[1] or city, parts[3] or zp
    city, zp, tail = city_zip(p, city, zp)
    if legal:
        if not (city or zp):
            return None, False
        return (tc(st) if NUMBERED.match(st) and st == st.upper() else st) + tail, False
    street = re.sub(r"^0+\s+", "", st)
    if not re.search(r"[A-Za-z]{2,}", street):
        return None, False
    return tc(street) + tail, bool(NUMBERED.match(st))


def geocode_rows(rows):
    """Census batch geocoder with a city and / or ZIP per row. rows: [(id, street, city, zip)].
    Returns {id: (match type, matched address, lat, lon, county fips) or None}; ids of a failed batch are left out."""
    out = {}
    for i in range(0, len(rows), 1000):
        chunk = rows[i:i + 1000]
        buf = io.StringIO()
        w = csv.writer(buf)
        for rid, street, city, zp in chunk:
            w.writerow([rid, street, city or "", "GA", zp or ""])
        boundary = uuid.uuid4().hex
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"benchmark\"\r\n\r\nPublic_AR_Current\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"vintage\"\r\n\r\nCurrent_Current\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"addressFile\"; filename=\"a.csv\"\r\nContent-Type: text/csv\r\n\r\n").encode() + buf.getvalue().encode() + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request("https://geocoding.geo.census.gov/geocoder/geographies/addressbatch", data=body,
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "User-Agent": "Mozilla/5.0"})
        txt = None
        for _ in range(2):
            try:
                txt = urllib.request.urlopen(req, timeout=300).read().decode("utf-8", "replace")
                break
            except Exception as e:
                print("  geocode batch failed:", type(e).__name__)
                time.sleep(3)
        if txt is None:
            continue
        for r in csv.reader(io.StringIO(txt)):
            if len(r) >= 10 and r[2] == "Match":
                lon, lat = map(float, r[5].split(","))
                out[r[0]] = (r[3], r[4], round(lat, 6), round(lon, 6), (r[8] or "") + (r[9] or ""))
            elif r:
                out[r[0]] = None
        time.sleep(1)
    return out


def zkey(fips, street):
    return f"z|{fips}|{street.upper()}"


def locate_addresses(auctions, fips, cache):
    """Numbered street addresses the parcel map could not place: ask the Census geocoder with the city / ZIP the county
    wrote, else with every ZIP code of the county, and keep a match only when it is in this county, the street name is
    the same and there is one such place. Returns how many parcels were placed."""
    need = {}
    for a in auctions:
        for p in a["parcels"]:
            if p.get("lat"):
                continue
            st, city, zp, legal = split_addr(p.get("address"))
            if legal or not st or not NUMBERED.match(st):
                continue
            if not city and p.get("area") and "(unincorporated)" not in p["area"]:
                city = p["area"]
            need.setdefault(st.upper(), (st, city, zp))
    todo = [k for k in need if zkey(fips, k) not in cache]
    rows, ids = [], {}
    for n, k in enumerate(todo):
        st, city, zp = need[k]
        cands = ([(city, zp)] if (city or zp) else []) + [(None, z) for z in ZIPS.get(fips, []) if z != zp]
        ids[k] = [f"{n}#{i}" for i in range(len(cands))]
        rows += [(f"{n}#{i}", st, c, z) for i, (c, z) in enumerate(cands)]
    if rows:
        print(f"  geocoding {len(todo)} unplaced addresses against the county's ZIP codes ({len(rows)} tries)")
        res = geocode_rows(rows)
        want = _core
        for k in todo:
            if any(i not in res for i in ids[k]):
                continue        # a batch failed: try again next run
            ok = [res[i] for i in ids[k] if res[i] and res[i][4] == fips and want(res[i][1]) == want(k)]
            same = [m for m in ok if _full_words(m[1]) == _full_words(k)]
            ok = same or ok     # "45 HOLLY CT" beats "45 HOLLY RD" when the county wrote CT
            first = res[ids[k][0]] if (need[k][1] or need[k][2]) else None
            if first in ok:
                ok = [first]    # the place the county itself named
            spots = {(m[2], m[3]) for m in ok}
            hit = ok[0] if len(spots) == 1 else None
            cache[zkey(fips, k)] = {"lat": hit[2], "lon": hit[3], "matched": hit[1], "fips": fips} if hit else None
    n = 0
    for a in auctions:
        for p in a["parcels"]:
            if p.get("lat"):
                continue
            st, city, zp, legal = split_addr(p.get("address"))
            g = cache.get(zkey(fips, st)) if st and not legal else None
            if g:
                p["lat"], p["lon"], p["geo"] = g["lat"], g["lon"], "address"
                n += 1
    return n


def _core(address):
    """Street name words without house number, type and direction; route prefixes (US, State Rte, GA) are ignored too,
    so "371 W 78 HWY" and "371 US HWY 78" compare equal."""
    return [x for x in streetview._street_words(address) if x not in ("US", "U", "STATE", "RTE", "ROUTE", "GA", "SR")]


def _full_words(address):
    """House number, direction, name and street type of the first address line, in TIGER's abbreviations."""
    a = str(address or "").split(",")[0].upper()
    return [streetview.SUFFIX.get(x, {"TERR": "TER", "AV": "AVE", "PKY": "PKWY"}.get(x, x)) for x in re.findall(r"[A-Z0-9]+", a)]


def _inside(lon, lat, geom):
    """Point in a GeoJSON Polygon / MultiPolygon (outer rings only; good enough for a county)."""
    polys = geom["coordinates"] if geom.get("type") == "MultiPolygon" else [geom.get("coordinates", [])]
    for poly in polys:
        ring, hit = (poly[0] if poly else []), False
        for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
            if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
                hit = not hit
        if hit:
            return True
    return False


def _bbox(geom):
    polys = geom["coordinates"] if geom.get("type") == "MultiPolygon" else [geom.get("coordinates", [])]
    xs = [x for poly in polys for x, y in poly[0]]
    ys = [y for poly in polys for x, y in poly[0]]
    return min(xs), min(ys), max(xs), max(ys)


def _road_place(street, geom):
    """City and ZIP of a named road inside the county, when every stretch of it lies in one ZIP code (and one city).
    Returns {"zip":..., "city":...} (keys only when unambiguous), {} when the road is not found, None on a network error."""
    core = streetview._street_words("1 " + street)
    if not core:
        return {}
    word = max(core, key=len)
    if len(word) < 3:
        return {}
    full = _full_words("1 " + street)[1:]
    x1, y1, x2, y2 = _bbox(geom)
    pts = []
    try:
        for layer in streetview.LAYERS:
            q = urllib.parse.urlencode({"where": "UPPER(NAME) LIKE '%%%s%%'" % word.replace("'", "''"), "geometry": f"{x1},{y1},{x2},{y2}",
                                        "geometryType": "esriGeometryEnvelope", "inSR": 4326, "outSR": 4326, "spatialRel": "esriSpatialRelIntersects",
                                        "outFields": "NAME", "returnGeometry": "true", "f": "json"})
            req = urllib.request.Request(streetview.TIGER % layer + "?" + q, headers={"User-Agent": streetview.UA})
            d = json.loads(urllib.request.urlopen(req, timeout=40).read())
            if "error" in d:
                return None
            for f in d.get("features", []):
                name = (f.get("attributes") or {}).get("NAME") or ""
                if streetview._words(name) != core:
                    continue
                exact = _full_words("1 " + name)[1:] == full
                for path in (f.get("geometry") or {}).get("paths", []):
                    lon, lat = path[len(path) // 2]
                    if _inside(lon, lat, geom):
                        pts.append((exact, lat, lon))
    except Exception:
        return None
    if any(e for e, _, _ in pts):
        pts = [t for t in pts if t[0]]
    if not pts:
        return {}
    step = max(1, len(pts) // 6)
    zips, cities = set(), set()
    for _, lat, lon in pts[::step][:8]:
        z, c = _zcta(lat, lon), _place(lat, lon)
        if z is None or c is None:
            return None
        zips.add(z)
        cities.add(c)
    out = {}
    if len(zips) == 1 and "" not in zips:
        out["zip"] = zips.pop()
    if len(cities) == 1 and "" not in cities:
        out["city"] = cities.pop()
    return out


def road_places(auctions, name, fips, cache):
    """City and ZIP for parcels that still have no location but name a street: taken from the road itself when the whole
    road lies in one ZIP code. No map point is set (the spot along the road is unknown)."""
    geom = (META.get(name) or {}).get("geom")
    if not geom:
        return 0
    from concurrent.futures import ThreadPoolExecutor
    need = {}
    for a in auctions:
        for p in a["parcels"]:
            if p.get("lat") or (p.get("zip") and p.get("area")):
                continue
            st, city, zp, legal = split_addr(p.get("address"))
            if legal or not st:
                continue
            nm = re.sub(r"^\d+[A-Z]?\s+", "", st.upper()).strip()
            if nm:
                need.setdefault(nm, None)
    todo = [k for k in need if f"r|{fips}|{k}" not in cache]
    if todo:
        with ThreadPoolExecutor(4) as ex:
            for k, r in zip(todo, ex.map(lambda k: _road_place(k, geom), todo)):
                if r is not None:
                    cache[f"r|{fips}|{k}"] = r
    n = 0
    for a in auctions:
        for p in a["parcels"]:
            if p.get("lat") or (p.get("zip") and p.get("area")):
                continue
            st, city, zp, legal = split_addr(p.get("address"))
            if legal or not st:
                continue
            r = cache.get(f"r|{fips}|" + re.sub(r"^\d+[A-Z]?\s+", "", st.upper()).strip()) or {}
            if r.get("zip") and not p.get("zip"):
                p["zip"] = r["zip"]
                n += 1
            if r.get("city") and not p.get("area"):
                p["area"] = FIX.get(r["city"], r["city"])
    return n


def geo_lookup(cache, fips, p):
    """The geocoder's answer for a parcel's street address, from either pass."""
    st = street_of(p)
    g = cache.get(f"{fips}|{st.upper()}") if st else None
    if not g:
        s2 = split_addr(p.get("address"))[0]
        g = cache.get(zkey(fips, s2)) if s2 else None
    return g


def _zcta(lat, lon):
    u = ("https://geocoding.geo.census.gov/geocoder/geographies/coordinates?x=%s&y=%s&benchmark=Public_AR_Current&vintage=Current_Current"
         "&layers=2020%%20Census%%20ZIP%%20Code%%20Tabulation%%20Areas&format=json" % (lon, lat))
    for _ in range(2):
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
            g = json.loads(urllib.request.urlopen(req, timeout=30).read())["result"]["geographies"]
            z = next(iter(g.values()), [])
            return (z[0].get("ZCTA5") or z[0].get("NAME", "").replace("ZCTA5 ", "")) if z else ""
        except Exception:
            time.sleep(1)
    return None


def fill_zips(auctions, zcache, geo_of):
    """ZIP code for located parcels that did not get one from the address match."""
    from concurrent.futures import ThreadPoolExecutor
    need = {}
    for a in auctions:
        for p in a["parcels"]:
            g = geo_of(p)
            if g and g.get("matched") and g["matched"].count(",") >= 3:
                p["zip"] = g["matched"].split(",")[3].strip()
            elif p.get("lat"):
                need.setdefault(f"{p['lat']:.3f},{p['lon']:.3f}", (p["lat"], p["lon"]))
    todo = [(k, v) for k, v in need.items() if k not in zcache]
    if todo:
        with ThreadPoolExecutor(6) as ex:
            for (k, _), r in zip(todo, ex.map(lambda kv: _zcta(*kv[1]), todo)):
                if r is not None:
                    zcache[k] = r
    for a in auctions:
        for p in a["parcels"]:
            if not p.get("zip") and p.get("lat"):
                z = zcache.get(f"{p['lat']:.3f},{p['lon']:.3f}")
                if z:
                    p["zip"] = z


def extras(s, auctions, C):
    """Building details, street point for Street View, and photos, per distinct parcel."""
    uniq = {}
    for a in auctions:
        for p in a["parcels"]:
            uniq.setdefault(p["parcel"], p)
    plist = list(uniq.values())
    det, sv, ph = {}, {}, {}
    try:
        det = photos_metro.details(s, plist, C["metro"])
    except Exception as e:
        print("  building details failed:", type(e).__name__, e)
    try:
        sv = streetview2.points(s, plist, C["sv"])
    except Exception as e:
        print("  street points failed:", type(e).__name__, e)
    for name, fn, cache in (("assessor", photos_metro.find, C["metro"]), ("survey", photos_outer.find, C["outer"])):
        try:
            got = fn(s, plist, cache)
        except Exception as e:
            got = {}
            print(f"  {name} photos failed:", type(e).__name__, e)
        for pid, lst in got.items():
            for x in lst:
                if x.get("src") and (HERE / x["src"]).is_file() and all(x["src"] != y["src"] for y in ph.get(pid, [])):
                    ph.setdefault(pid, []).append({k: v for k, v in x.items() if v not in (None, "") and k in ("src", "kind", "credit", "date", "page")})
    nph = 0
    for a in auctions:
        for p in a["parcels"]:
            pid = p["parcel"]
            d = det.get(pid) or {}
            for k in ("yearBuilt", "sqft"):
                if d.get(k) and not p.get(k):
                    p[k] = d[k]
                    p["improved"] = True
            v = sv.get(pid)
            if v:
                p["sv"] = [v["lat"], v["lon"], v.get("heading"), v.get("m")]   # road point, heading toward the lot, metres to the lot
                if v.get("road"):
                    p["road"] = tc(v["road"]) if v["road"] == v["road"].upper() else v["road"]
            elif "sv" in p:
                p.pop("sv")
            if ph.get(pid):
                p["photos"] = ph[pid][:4]
                nph += 1
            elif "photos" in p:
                p.pop("photos")
    print(f"  street points {len(sv)}, parcels with photos {len(ph)}, building details {len(det)}")
    for k, f in (("metro", PH_METRO), ("outer", PH_OUTER)):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(C[k], separators=(",", ":")))
    SVCACHE.write_text(json.dumps(C["sv"], separators=(",", ":")))


def load(path):
    return json.loads(path.read_text()) if path.exists() else {}


def geocode(rows, fips):
    """Census batch geocoder (geographies, so we can reject matches in another county). rows: [(id, street)]."""
    out = {}
    for i in range(0, len(rows), 900):
        chunk = rows[i:i + 900]
        buf = io.StringIO()
        w = csv.writer(buf)
        for rid, street in chunk:
            w.writerow([rid, street, "", "GA", ""])
        boundary = uuid.uuid4().hex
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"benchmark\"\r\n\r\nPublic_AR_Current\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"vintage\"\r\n\r\nCurrent_Current\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"addressFile\"; filename=\"a.csv\"\r\nContent-Type: text/csv\r\n\r\n").encode() + buf.getvalue().encode() + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request("https://geocoding.geo.census.gov/geocoder/geographies/addressbatch", data=body,
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "User-Agent": "Mozilla/5.0"})
        try:
            txt = urllib.request.urlopen(req, timeout=300).read().decode("utf-8", "replace")
        except Exception as e:
            print("  geocode failed:", type(e).__name__)
            continue
        for r in csv.reader(io.StringIO(txt)):
            if len(r) >= 10 and r[2] == "Match":
                lon, lat = map(float, r[5].split(","))
                cty = (r[8] or "") + (r[9] or "")
                out[r[0]] = {"lat": round(lat, 6), "lon": round(lon, 6), "matched": r[4], "fips": cty}
            elif r:
                out[r[0]] = None
        time.sleep(1)
    return {k: v for k, v in out.items() if v is None or v["fips"] == fips}


def street_of(p):
    a = (p.get("address") or "").split(",")[0].strip()
    a = re.sub(r"\*.*$", "", a)
    return a if re.match(r"^\d+[A-Z]?\s+\S+", a) and not a.startswith("0 ") else None


HENRY = {"MCD": "McDonough", "LG": "Locust Grove", "STK": "Stockbridge", "HAM": "Hampton", "ELL": "Ellenwood", "REX": "Rex", "JNK": "Jenkinsburg", "FBN": "Fairburn"}


FIX = {"Mcdonough": "McDonough", "Lagrange": "LaGrange", "Mcdonough ": "McDonough"}


def area_of(p, g):
    """City name: from the geocoder's matched address, else from a city written after the street address."""
    if g and g.get("matched"):
        parts = [x.strip() for x in g["matched"].split(",")]
        if len(parts) >= 3 and parts[1]:
            return FIX.get(parts[1].title(), parts[1].title())
    parts = [x.strip() for x in (p.get("address") or "").split(",")]
    if len(parts) >= 2 and re.match(r"^[A-Za-z .'-]{2,30}$", parts[1]) and parts[1].upper() != "GA":
        return HENRY.get(parts[1].upper(), FIX.get(parts[1].title(), parts[1].title()))
    return None


def main(only=None):
    cache = load(CACHE)
    pcache, gcache, acache, zcache = load(PCACHE), load(GCACHE), load(ACACHE), load(ZCACHE)
    XC = {"metro": load(PH_METRO), "outer": load(PH_OUTER), "sv": load(SVCACHE)}
    index = []
    for c in COUNTIES:
        s = slug(c["name"])
        f = DATA / f"{s}.json"
        old = json.loads(f.read_text()) if f.exists() else {}
        auctions, err = old.get("auctions", []), None
        if c.get("fetch") and (not only or s in only):
            print("Fetching", c["name"], flush=True)
            try:
                fresh = c["fetch"]()
                funcs, drop = EXTRA.get(s, ([], set()))
                extra = []
                for fn in funcs:
                    try:
                        extra += fn()
                    except Exception as e:
                        print("  extra source failed:", fn.__name__, type(e).__name__, e)
                if extra:
                    keys = {(a["date"], a["kind"]) for a in extra}
                    fresh = [a for a in fresh if (a["date"], a["kind"]) not in keys and a["kind"] not in drop] + extra
                    auctions = [a for a in auctions if a["kind"] not in drop]
                auctions = merge(auctions, fresh)
                print(f"  {len(fresh)} auctions, {sum(len(a['parcels']) for a in fresh)} parcel rows")
            except Exception as e:
                err = f"{type(e).__name__}: {e}"[:200]
                print("  FAILED", err)
                traceback.print_exc()
        today = datetime.now().strftime("%Y-%m-%d")
        if auctions and (not only or s in only):
            try:
                if s == "cobb":
                    print("  cobb deed prices:", PM.enrich_cobb(auctions))
                elif s == "clayton":
                    print("  clayton payment prices:", PM.enrich_clayton(auctions, pcache))
                elif s == "dekalb":
                    print("  dekalb deed prices:", PM.enrich_dekalb(auctions, pcache))
            except Exception as e:
                print("  price lookup failed:", type(e).__name__, e)
            PCACHE.write_text(json.dumps(pcache))
        auctions = fold(auctions)
        m = META.get(c["name"], {})
        fips = m.get("fips", "")
        for a in auctions:
            for p in a["parcels"]:
                if p.get("mail") and p.get("address") == p["mail"]:
                    p.pop("address")        # decided again below, after the parcel map had its say
                p.pop("addrNote", None)
                ad = p.get("address") or ""
                ad2 = re.sub(r"^\d+(\.\d+)?\s*ACRES?\s*(?=\d)", "", ad)      # "29 ACRES5544 BRITTON DR"
                ym, yrs = re.match(r"^(20[012]\d)\s+(\D.*)$", ad2), re.findall(r"20\d\d", str(p.get("years") or ""))
                if s == "carroll" and ym and yrs and ym.group(1) == yrs[-1]:
                    ad2 = ym.group(2)     # the last tax year ran into the address column of the sale notice
                if ad2 != ad:
                    p["address"] = ad2
        # geocode street addresses once, cached by county + street
        todo = []
        for a in auctions:
            for p in a["parcels"]:
                st = street_of(p)
                if st:
                    key = f"{fips}|{st.upper()}"
                    if key not in cache:
                        todo.append((key, st))
        todo = list(dict(todo).items())
        if todo:
            print(f"  geocoding {len(todo)} addresses")
            res = geocode(todo, fips)
            for key, _ in todo:
                cache[key] = res.get(key)
            CACHE.write_text(json.dumps(cache))
        for a in auctions:
            for p in a["parcels"]:
                st = street_of(p)
                g = cache.get(f"{fips}|{st.upper()}") if st else None
                if g:
                    p["lat"], p["lon"] = g["lat"], g["lon"]
                ar = area_of(p, g)
                if ar:
                    p["area"] = ar
        # exact parcel location from the county's parcel GIS (overrides the street-address geocode)
        if auctions and (not only or s in only):
            ids = sorted({p["parcel"] for a in auctions for p in a["parcels"] if p.get("parcel")})
            try:
                loc = parcel_geo.locate(s, ids, gcache)
            except Exception as e:
                loc = {}
                print("  parcel GIS failed:", type(e).__name__, e)
            GCACHE.write_text(json.dumps(gcache))
            n = 0
            for a in auctions:
                for p in a["parcels"]:
                    h = loc.get(p.get("parcel"))
                    if h and h.get("lat"):
                        p["lat"], p["lon"], p["geo"] = round(h["lat"], 6), round(h["lon"], 6), "parcel"
                        n += 1
                        if h.get("city") and not p.get("area"):
                            p["area"] = FIX.get(h["city"].title(), h["city"].title())
                        if h.get("address") and not p.get("address"):
                            p["address"] = h["address"]
                        for k in ("ptype", "ptypeDetail", "improved", "acres", "acresSrc", "sqft", "yearBuilt"):
                            if h.get(k) is not None:
                                p[k] = h[k]
            for a in auctions:
                for p in a["parcels"]:
                    if not p.get("ptype"):
                        blob = " ".join(str(p.get(k) or "") for k in ("parcel", "type", "desc", "address")).upper()
                        if re.search(r"MH$|MOBILE|MANUFACTURED HOME", str(p.get("parcel", "")).upper() + " " + blob):
                            p["ptype"], p["ptypeDetail"] = "Mobile Home", "From the sale listing"
                        elif re.search(r"\bBOAT\b|PERSONAL PROP|\bEQUIPMENT\b", blob):
                            p["ptype"], p["ptypeDetail"] = "Other", "Personal property (from the sale listing)"
                        elif re.search(r"VAC(ANT)? ?LOT|\(LOT\)|VACANT", blob):
                            p["ptype"], p["ptypeDetail"] = "Vacant Land", "From the sale listing"
                        elif re.search(r"\bHOUSE\b|DWELLING|RESIDEN", blob):
                            p["ptype"], p["ptypeDetail"] = "Residential", "From the sale listing"
            print(f"  parcel GIS placed {n} records")
            local = set(ZIPS.get(fips, []))
            for a in auctions:
                for p in a["parcels"]:
                    # an owner's mailing address stands in for the property address only inside the county, and says so
                    if p.get("mail") and not p.get("address"):
                        zp = split_addr(p["mail"])[2]
                        if zp and zp in local and not re.search(r"\bP\.? ?O\.? BOX\b", p["mail"], re.I):
                            p["address"] = p["mail"]
                            p["addrNote"] = "Owner's mailing address on the county's list. It may not be the property itself."
            try:
                print("  placed by street address:", locate_addresses(auctions, fips, cache))
            except Exception as e:
                print("  address placing failed:", type(e).__name__, e)
            CACHE.write_text(json.dumps(cache))
            geo_of = lambda p: geo_lookup(cache, fips, p)
            for a in auctions:
                for p in a["parcels"]:
                    st, city, zp, legal = split_addr(p.get("address"))
                    if zp and local and zp not in local:
                        continue            # a mailing address outside the county says nothing about the parcel
                    if zp and not p.get("zip"):
                        p["zip"] = zp       # the ZIP code the county wrote
                    if not p.get("area"):
                        ar = area_of(p, geo_of(p)) or (FIX.get(tc(city), tc(city)) if city else None)
                        if ar:
                            p["area"] = ar
            print("  areas from location:", fill_areas(auctions, acache))
            ACACHE.write_text(json.dumps(acache))
            fill_zips(auctions, zcache, geo_of)
            ZCACHE.write_text(json.dumps(zcache))
            try:
                print("  city / ZIP from the road name:", road_places(auctions, c["name"], fips, cache))
            except Exception as e:
                print("  road name lookup failed:", type(e).__name__, e)
            CACHE.write_text(json.dumps(cache))
            for a in auctions:
                for p in a["parcels"]:
                    fa, numbered = full_address(p, geo_of(p))
                    if fa:
                        p["fullAddress"], p["addrNum"] = fa, numbered
                    else:
                        p.pop("fullAddress", None)
                        p.pop("addrNum", None)
            extras(s, auctions, XC)
            for a in auctions:
                for p in a["parcels"]:
                    if not p.get("fullAddress") and not p.get("address") and p.get("lat"):
                        city, zp, tail = city_zip(p)
                        if city or zp:      # no address published: the nearest road, city and ZIP of the parcel's map location
                            p["fullAddress"], p["addrNum"] = ("Near " + p["road"] if p.get("road") else "No street address") + tail, False
                            p["addrNote"] = "The county publishes no street address for this parcel. The line shows the nearest road, city and ZIP code of its map location."
        rec = {k: v for k, v in c.items() if k != "fetch"}
        rec["auction"] = AUCTION_INFO.get(c["name"])
        rec.update(slug=s, label=c.get("label", c["name"]), fips=fips, milesFromAtlanta=m.get("near"), pctInRadius=m.get("inside"),
                   automated=bool(c.get("fetch")), auctions=auctions, lastChecked=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        if err:
            rec["lastError"] = err
        elif "lastError" in rec:
            rec.pop("lastError")
        if old.get("auctions") != auctions or not old:
            rec["lastChanged"] = rec["lastChecked"]
        else:
            rec["lastChanged"] = old.get("lastChanged", rec["lastChecked"])
        f.write_text(json.dumps(rec, separators=(",", ":")))
        up = sorted([a["date"] for a in auctions if a["date"] >= today])
        last = sorted([a["date"] for a in auctions if a["date"] < today], reverse=True)
        index.append(dict(slug=s, name=c["name"], label=rec["label"], fips=fips, run=c["run"], automated=rec["automated"], milesFromAtlanta=rec["milesFromAtlanta"],
                          pctInRadius=rec["pctInRadius"], auctions=len(auctions), parcels=sum(len(a["parcels"]) for a in auctions),
                          sold=sum(1 for a in auctions for p in a["parcels"] if p.get("status") == "Sold"),
                          withPrice=sum(1 for a in auctions for p in a["parcels"] if p.get("winningBid")),
                          nextSale=up[0] if up else None, lastSale=last[0] if last else None, page=c["page"], note=c["note"], lastError=rec.get("lastError")))
    out = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "center": REGION["center"], "radiusMiles": REGION["radius_mi"],
           "counties": index}
    (DATA / "index.json").write_text(json.dumps(out, separators=(",", ":")))
    geo = {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {"name": cc["name"], "slug": slug(cc["name"]), "state": cc["state"], "pct": cc["inside"]},
                                                       "geometry": cc["geom"]} for cc in REGION["counties"]] + [{"type": "Feature", "properties": {"name": "radius"}, "geometry": REGION["circle"]}]}
    (DATA / "region.geojson").write_text(json.dumps(geo, separators=(",", ":")))
    print("done:", len(index), "counties")


if __name__ == "__main__":
    main(set(sys.argv[1:]) or None)
