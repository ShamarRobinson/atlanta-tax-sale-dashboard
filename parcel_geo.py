"""Exact parcel locations (centroid lat/lon, WGS84) from public county parcel GIS layers.

    locate(slug, parcel_ids, cache) -> {parcel_id: {"lat":.., "lon":.., "city":.., "address":..}}

`cache` is a dict the caller persists: f"{slug}|{parcel_id}" -> result dict, or None when the
layer answered but has no such parcel. Cached keys are never re-queried. Network failures are
not cached, so those parcels are retried on the next run. locate() never raises.

Every source is an open ArcGIS REST layer (no token). Each county has one or more sources, tried
in order; a source says which field holds the parcel number and how the stored ID is rewritten
into the layer's format. Matching happens in two passes:
  1. exact:  field IN (<variants of ~50 ids>)
  2. fuzzy:  field LIKE 'tok1%tok2%...'  for what is still missing
and every returned feature is verified by comparing normalised keys (letters and digits only),
so a loose LIKE can never attach the wrong parcel.

Standard library only.
"""
import json
import re
import urllib.parse

import time
import urllib.request

try:
    from ga_common import UA
except Exception:  # pragma: no cover - keeps the module usable on its own
    UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"

TIMEOUT = 20
TRIES = 2
BATCH = 50          # ids per IN query
LIKE_BATCH = 15     # ids per OR-of-LIKE query
GA = (30.3, 35.1, -85.7, -80.7)  # lat min, lat max, lon min, lon max

# Third-party statewide parcel layer (hosted on ArcGIS Online, not a county source); used only
# for counties that publish no open layer of their own. It has no county field, so each use is
# restricted to the county's bounding box.
POOL = "https://services8.arcgis.com/kePfB0a6AiElbP14/arcgis/rest/services/ga_parcel_pool_2_15_2025/FeatureServer/"


# ---------------------------------------------------------------- id helpers
def key(s):
    """Normalised comparison key: upper-case letters and digits only."""
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


def toks(s):
    return [t for t in re.split(r"[^A-Za-z0-9]+", str(s or "").upper()) if t]


def _clean(pid):
    """Stored ids sometimes carry spreadsheet debris ('1130023.0')."""
    s = str(pid or "").strip().upper()
    return re.sub(r"\.0$", "", s)


def v_basic(pid):
    s = _clean(pid)
    t = toks(s)
    return [s, " ".join(t), "".join(t), "-".join(t)]


def v_fulton(pid):
    # '11-0821-0303-169-1' -> '11 082103031691' ; '09F-1505-0078-050-5' -> '09F150500780505'
    t = toks(_clean(pid))
    if len(t) < 2:
        return v_basic(pid)
    rest = "".join(t[1:])
    return [t[0] + " " + rest, t[0] + rest, t[0] + "  " + rest]


def v_gwinnett(pid):
    s = _clean(pid)
    out = [s]
    if not s.startswith("R"):
        out.append("R" + s)
    m = re.match(r"^(R?\d{4}[A-Z]?)\s*(\d{3}[A-Z]?)$", s)
    if m:
        a, b = m.groups()
        if not a.startswith("R"):
            a = "R" + a
        out += [a + " " + b if len(a) == 5 else a + b, a + b, a + " " + b]
    return out


def v_henry(pid):
    # '044-01016000', 'M11-0802000', '006A07016000' : map(3) + '-' or letter, then 8 digits
    s = _clean(pid)
    k = key(s)
    out = [s, k]
    if len(k) == 11:
        out.append(k[:3] + "-" + k[3:])
    return out


def v_fayette(pid):
    # '0550008' -> '0550  008' (district+square, blank lot, parcel); 9-digit numbers match as stored
    k = key(_clean(pid))
    out = v_basic(pid)
    if len(k) == 7:
        out.append(k[:4] + "  " + k[4:])
    return out


def v_troup(pid):
    # layer value has no separators and a 4-digit map: '0602B 004 001' -> '0602B004001'
    k = key(_clean(pid))
    out = [k]
    t = toks(_clean(pid))
    if t and re.fullmatch(r"\d{3}[A-Z]?", t[0]):
        out.append("0" + k)
    return out


def parent_clayton(pid):
    # condo units ('05213 213001 V03') are not drawn; their parent parcel ('05213 213001') is
    t = _clean(pid).split()
    return " ".join(t[:2]) if len(t) == 3 else None


def _wingap_split(pid):
    """Split an undelimited WinGAP number into (map, parcel): '013A043' -> ('013A', '043')."""
    k = key(_clean(pid))
    for rx in (r"^([A-Z]?\d{2,4}[A-Z]?)(\d{3}[A-Z]?)$", r"^(\d{3})(\d{4}[A-Z]?)$"):
        m = re.match(rx, k)
        if m:
            return m.groups()
    return None


def like_tokens(pid):
    t = toks(_clean(pid))
    return ["%".join(t) + "%"] if t else []


def like_wingap(pid):
    out = like_tokens(pid)
    if len(toks(_clean(pid))) == 1:
        sp = _wingap_split(pid)
        if sp:
            out.append(sp[0] + "%" + sp[1] + "%")
    return out


def like_carroll(pid):
    # stored '1280189' / '030-0136' / 'T040060091' / '380033' (leading zero lost)
    # layer  '128    0189' / '128-   -0189' / 'T04-006-0091' / 'T04 0060091'
    k = key(_clean(pid))
    if re.fullmatch(r"\d{6}", k):
        k = "0" + k
    out = []
    if len(k) == 7:
        out.append(k[:3] + "%" + k[3:] + "%")
    elif len(k) == 10:
        out += [k[:3] + "%" + k[3:6] + "%" + k[6:] + "%"]
    else:
        out.append(k + "%")
    return out


def k_carroll(s):
    return key(s).lstrip("0")


def k_troup(s):
    return key(s).lstrip("0")


def like_polk(pid):
    # stored '008 072F' ; layer '008-072F' or '008-031-'
    t = toks(_clean(pid))
    return ["%".join(t) + "%"] if t else []


def k_gwinnett(s):
    k = key(s)
    return k[1:] if k.startswith("R") else k


def k_nozero(s):
    """Key that ignores zero padding inside tokens ('C0530-053-A00' == 'C0530-053-A')."""
    out = []
    for t in toks(s):
        t2 = t.lstrip("0") or "0"
        if re.fullmatch(r"[A-Z]0+", t):
            t2 = t[0]
        out.append(t2)
    while out and out[-1] in ("0", "000"):
        out.pop()
    return "|".join(out)


# ---------------------------------------------------------------- sources
def S(url, field, variants=v_basic, like=like_tokens, addr=None, city=None, keyf=key, bbox=None, name=None,
      parent=None):
    return {"url": url, "field": field, "variants": variants, "like": like, "addr": addr, "city": city,
            "keyf": keyf, "bbox": bbox, "name": name or url, "parent": parent}


def pool(bbox, like=like_tokens, keyf=key):
    return [S(POOL + str(i), "apn", like=like, addr="prop_address", city="city", keyf=keyf, bbox=bbox,
              name="statewide pool (third party) " + POOL + str(i)) for i in (2, 1, 0)]


SOURCES = {
    "fulton": [S("https://gismaps.fultoncountyga.gov/arcgispub2/rest/services/PropertyMapViewer/PropertyMapViewer/MapServer/11",
                 "ParcelID", v_fulton, addr="Address"),
               S("https://services1.arcgis.com/AQDHTHDrZzfsFsB5/arcgis/rest/services/Tax_Parcels/FeatureServer/0",
                 "ParcelID", v_fulton, addr="Address")],
    "dekalb": [S("https://dcgis.dekalbcountyga.gov/hosted/rest/services/PropertyAppraisal/Parcels_IASWorld/MapServer/0",
                 "PARCELID", addr="SITEADDRESS", city="CITY")],
    "cobb": [S("https://gis.cobbcounty.gov/gisserver/rest/services/tax/taxassessorsdaily/MapServer/0",
               "PIN", addr="SITUS_ADDR")],
    "clayton": [S("https://gis.claytoncountyga.gov/server/rest/services/TaxAssessor/Parcels/MapServer/0",
                  "PARCELID", addr="SITEADDRES", city="SITECITY", parent=parent_clayton)],
    "fayette": [S("https://gis.fayettecountyga.gov/arcgis/rest/services/Pictometry/parcelsRO/MapServer/0", "PARCEL_NO", v_fayette),
                S("https://services5.arcgis.com/Hg5aLg4LtSINzVWa/arcgis/rest/services/TaxParcels_public/FeatureServer/0",
                  "PARCELID", v_fayette, addr="SITEADDRESS")],
    "douglas": [S("https://maps.douglascountyga.gov/arcgis/rest/services/TylerTech/LandRecords/MapServer/0",
                  "PIN", addr="ADDRESS")],
    "troup": [S("https://services6.arcgis.com/WjqAE1SlQxuk7dsk/arcgis/rest/services/Troup_County_GA_Parcel_Feature_Layer/FeatureServer/0",
                "parcelnu_1", v_troup, addr="address", city="scity", keyf=k_troup)],
    "meriwether": [S("https://services9.arcgis.com/Xv8vRekQ4FVHSSIe/arcgis/rest/services/MeriwetherParcels/FeatureServer/0",
                     "Parcel_No", like=like_wingap, addr=["house_no", "stdirect", "street_nam", "sttype"])],
    "spalding": [S("https://services5.arcgis.com/IBG8fFojdkoiHAvQ/arcgis/rest/services/Parcels_Public_View/FeatureServer/1",
                   "PARCEL_ID", like=like_wingap)],
    "lumpkin": [S("https://services6.arcgis.com/BAJNi3EgCdtQ1BCG/arcgis/rest/services/Lumpkin_2025Parcels/FeatureServer/0",
                  "PARCEL_NO", like=like_wingap, addr=["HOUSE_NO", "STREET_NAM"])],
    "gwinnett": [S("https://services3.arcgis.com/RfpmnkSAQleRbndX/arcgis/rest/services/Property_and_Tax/FeatureServer/0",
                   "TAXPIN", v_gwinnett, keyf=k_gwinnett)],
    "henry": [S("https://services1.arcgis.com/W8nsWsU3ZKxVPuk7/arcgis/rest/services/HenryCountyParcels/FeatureServer/0",
                "PARCEL_ID", v_henry, addr="STREET_ADDRESS")],
    "rockdale": [S("https://services.arcgis.com/Tbke9ca9DhtF4VIx/ArcGIS/rest/services/Parcel_Polygons_working/FeatureServer/0",
                   "PARCEL_NO", addr=["Address", "Road_name", "St_Type"])],
    "coweta": [S("https://services1.arcgis.com/AaPyNbrJpNGryRqh/arcgis/rest/services/WeeklyUpdate_WinGapParcels/FeatureServer/0",
                 "ParcelNumber", like=like_wingap, addr=["HouseNumber", "StreetDirection", "StreetName", "StreetType"])],
    "forsyth": [S("https://geo.forsythco.com/gis/rest/services/Public/Tax_Parcel/FeatureServer/0",
                  "PARCELID", addr="SITEADDRESS")],
    "clarke": [S("https://services2.arcgis.com/xSEULKvB31odt3XQ/arcgis/rest/services/Parcel/FeatureServer/0",
                 "PARCEL_NO", addr="PAR_ADD")],
    "dawson": [S("https://services7.arcgis.com/Ptz860OPLeIY55cX/ArcGIS/rest/services/Energov_Layers_Update2021/FeatureServer/3",
                 "PARCELID"),
               S("https://services.arcgis.com/ISpzx3B5ZsVA6e1Z/ArcGIS/rest/services/Dawson_Webmap_WFL1/FeatureServer/1",
                 "Parcel_No", like=like_wingap)],
    "floyd": [S("https://services2.arcgis.com/nV67H1IJR8GS6SAA/ArcGIS/rest/services/CurrentParcels/FeatureServer/0",
                "Parcel_No", addr="LOCATION"),
              S("https://services2.arcgis.com/nV67H1IJR8GS6SAA/ArcGIS/rest/services/Current_Parcels/FeatureServer/5",
                "PARCEL", addr="PROP_ADDR")],
    "jackson": [S("https://services8.arcgis.com/bcbi4lYRFOsss0F5/ArcGIS/rest/services/jackson_baselayers/FeatureServer/9",
                  "PARCEL_NO", like=like_wingap, addr=["HOUSE_NO", "STREET_NAM"])],
    # no open county layer found: statewide third-party layer, limited to the county bounding box
    # Walton: the Social Circle layer covers only the south-east corner of the county
    "walton": [S("https://services8.arcgis.com/LtjAOW49t0cQdmjH/arcgis/rest/services/Walton_Parcels/FeatureServer/16",
                 "Parcel_No", addr=["house_no", "stdirect", "street_nam", "sttype"], keyf=k_nozero)]
              + pool((-83.95, 33.6, -83.45, 34.0), keyf=k_nozero),
    "carroll": pool((-85.36, 33.38, -84.78, 33.82), like=like_carroll, keyf=k_carroll),
    "heard": pool((-85.32, 33.12, -84.83, 33.47)),
    "pickens": pool((-84.68, 34.36, -84.23, 34.6)),
    "polk": pool((-85.44, 33.85, -84.95, 34.12), like=like_polk),
}


def endpoint(slug):
    """Primary endpoint used for a county (for reports)."""
    src = SOURCES.get(slug)
    return src[0]["name"] if src else None


# ---------------------------------------------------------------- geometry
def centroid(geom):
    """Area-weighted centroid of an Esri polygon (rings in lon/lat). Returns (lat, lon) or None."""
    try:
        if not geom:
            return None
        if "x" in geom and "y" in geom:
            return float(geom["y"]), float(geom["x"])
        rings = geom.get("rings") or []
        A = cx = cy = 0.0
        n = sx = sy = 0
        for ring in rings:
            for i in range(len(ring) - 1):
                x0, y0 = ring[i][0], ring[i][1]
                x1, y1 = ring[i + 1][0], ring[i + 1][1]
                c = x0 * y1 - x1 * y0
                A += c
                cx += (x0 + x1) * c
                cy += (y0 + y1) * c
            for p in ring:
                sx += p[0]
                sy += p[1]
                n += 1
        if abs(A) > 1e-14:
            return cy / (3.0 * A), cx / (3.0 * A)
        if n:
            return sy / n, sx / n
    except Exception:
        pass
    return None


def _in_ga(lat, lon):
    return GA[0] <= lat <= GA[1] and GA[2] <= lon <= GA[3]


# ---------------------------------------------------------------- querying
def _q(s):
    return "'" + str(s).replace("'", "''") + "'"


def _attr(attrs, spec):
    if not spec:
        return None
    if isinstance(spec, str):
        spec = [spec]
    parts = []
    for f in spec:
        v = attrs.get(f)
        if v is None:
            continue
        v = re.sub(r"\s+", " ", str(v)).strip()
        if v and v.upper() not in ("NULL", "NONE", "0"):
            parts.append(v)
    s = " ".join(parts).strip()
    return s or None


def _query(src, where):
    """Run one query. Returns list of features, or None on failure."""
    p = {"where": where, "outFields": "*", "returnGeometry": "true", "outSR": "4326",
         "geometryPrecision": "6", "f": "json"}
    fields = [src["field"]]
    for spec in (src["addr"], src["city"]):
        if spec:
            fields += [spec] if isinstance(spec, str) else list(spec)
    p["outFields"] = ",".join(dict.fromkeys(fields))
    if src["bbox"]:
        p.update({"geometry": "%s,%s,%s,%s" % src["bbox"], "geometryType": "esriGeometryEnvelope",
                  "inSR": "4326", "spatialRel": "esriSpatialRelIntersects"})
    body = urllib.parse.urlencode(p)
    url = src["url"] + "/query"
    for i in range(TRIES):
        try:
            # POST keeps long IN lists out of the URL (ArcGIS Online answers 404 to long GETs);
            # the second try falls back to GET when the request is short enough.
            if i and len(body) < 1800:
                req = urllib.request.Request(url + "?" + body, headers={"User-Agent": UA, "Accept": "*/*"})
            else:
                req = urllib.request.Request(url, data=body.encode(), headers={
                    "User-Agent": UA, "Accept": "*/*", "Content-Type": "application/x-www-form-urlencoded"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                j = json.loads(r.read().decode("utf-8", "replace"))
            if isinstance(j, dict) and "error" not in j:
                return j.get("features") or []
        except Exception:
            pass
        if i + 1 < TRIES:
            time.sleep(1)
    return None


def _batches(items, size, src, cost):
    """Chunk items: at most `size` entries and a where clause of bounded length."""
    lim = 12000
    cur, n = [], 0
    for it in items:
        c = cost(it)
        if cur and (len(cur) >= size or n + c > lim):
            yield cur
            cur, n = [], 0
        cur.append(it)
        n += c
    if cur:
        yield cur


def _take(src, feats, want, out):
    """Match returned features to wanted ids (by normalised key) and fill `out`."""
    for f in feats:
        try:
            a = f.get("attributes") or {}
            k = src["keyf"](a.get(src["field"]))
            pid = want.get(k)
            if pid is None or pid in out:
                continue
            c = centroid(f.get("geometry"))
            if not c or not _in_ga(c[0], c[1]):
                continue
            r = {"lat": round(c[0], 6), "lon": round(c[1], 6)}
            ad = _attr(a, src["addr"])
            ct = _attr(a, src["city"])
            if ad and re.search(r"[A-Za-z]", ad):
                r["address"] = ad
            if ct:
                r["city"] = ct.title()
            out[pid] = r
        except Exception:
            continue


def _run_source(src, pids):
    """Returns (found {pid: result}, answered set of pids whose queries all succeeded)."""
    found, failed = {}, set()
    fails = 0
    want = {}
    for p in pids:
        k = src["keyf"](_clean(p))
        if k:
            want.setdefault(k, p)
    todo = [p for p in pids if src["keyf"](_clean(p)) and want[src["keyf"](_clean(p))] == p]

    # pass 1: exact IN
    def vs(p):
        try:
            return [v for v in dict.fromkeys(src["variants"](p)) if v]
        except Exception:
            return [_clean(p)]
    for chunk in _batches(todo, BATCH, src, lambda p: sum(len(urllib.parse.quote(v)) + 9 for v in vs(p))):
        if fails >= 2:
            failed.update(chunk)
            continue
        vals = list(dict.fromkeys(v for p in chunk for v in vs(p)))
        feats = _query(src, "%s IN (%s)" % (src["field"], ",".join(_q(v) for v in vals)))
        if feats is None:
            fails += 1
            failed.update(chunk)
            continue
        fails = 0
        _take(src, feats, want, found)

    # pass 2: LIKE for what is still missing
    def ls(p):
        try:
            return [v for v in dict.fromkeys(src["like"](p)) if v and len(v.replace("%", "")) >= 4]
        except Exception:
            return []
    rest = [p for p in todo if p not in found and p not in failed and ls(p)]
    f = src["field"]
    for chunk in _batches(rest, LIKE_BATCH, src,
                          lambda p: sum(len(urllib.parse.quote(v)) + len(f) + 16 for v in ls(p))):
        if fails >= 2:
            failed.update(chunk)
            continue
        where = " OR ".join("%s LIKE %s" % (f, _q(v)) for p in chunk for v in ls(p))
        feats = _query(src, where)
        if feats is None:
            fails += 1
            failed.update(chunk)
            continue
        fails = 0
        _take(src, feats, want, found)
    # duplicates of the same normalised key share the result
    for p in pids:
        k = src["keyf"](_clean(p))
        first = want.get(k)
        if first is not None and first in found:
            found[p] = found[first]
        elif first in failed:
            failed.add(p)
    return found, failed


def locate(slug, parcel_ids, cache):
    """Look up parcel centroids. See module docstring."""
    out = {}
    try:
        if cache is None:
            cache = {}
        pids = []
        for p in dict.fromkeys(parcel_ids or []):
            if p is None or not str(p).strip():
                continue
            ck = "%s|%s" % (slug, p)
            if ck in cache:
                if cache[ck]:
                    out[p] = cache[ck]
                continue
            pids.append(p)
        sources = SOURCES.get(slug)
        if not sources or not pids:
            return out
        missing = list(pids)
        unanswered = set()      # pids for which some source failed (network) -> do not cache a miss
        for src in sources:
            if not missing:
                break
            try:
                found, failed = _run_source(src, missing)
            except Exception:
                found, failed = {}, set(missing)
            unanswered |= failed
            for p, r in found.items():
                out[p] = r
                cache["%s|%s" % (slug, p)] = r
            missing = [p for p in missing if p not in found]
        # condo units and similar: fall back to the parent parcel where the county defines one
        pf = sources[0].get("parent")
        if pf and missing:
            par = {}
            for p in missing:
                try:
                    q = pf(p)
                except Exception:
                    q = None
                if q:
                    par.setdefault(q, []).append(p)
            if par:
                try:
                    found, failed = _run_source(sources[0], list(par))
                except Exception:
                    found, failed = {}, set(par)
                for q, kids in par.items():
                    for p in kids:
                        if q in found:
                            r = dict(found[q])
                            r["note"] = "parent parcel " + q
                            out[p] = r
                            cache["%s|%s" % (slug, p)] = r
                        elif q in failed:
                            unanswered.add(p)
                missing = [p for p in missing if p not in out]
        for p in missing:
            if p not in unanswered:
                cache["%s|%s" % (slug, p)] = None
    except Exception:
        pass
    return out


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3:
        print(json.dumps(locate(sys.argv[1], sys.argv[2:], {}), indent=1))
    else:
        print("usage: parcel_geo.py <slug> <parcel id> [<parcel id> ...]")
