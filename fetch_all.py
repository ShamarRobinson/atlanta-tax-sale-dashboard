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
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from registry import COUNTIES
import parcel_geo
import prices_metro as PM
import results_outer as RO

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
DATA.mkdir(exist_ok=True)
CACHE = DATA / "geocache.json"
PCACHE = DATA / "pricecache.json"
GCACHE = DATA / "parcelgeo.json"
ACACHE = DATA / "areacache.json"
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
    pcache, gcache, acache = load(PCACHE), load(GCACHE), load(ACACHE)
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
            print(f"  parcel GIS placed {n} records")
            print("  areas from location:", fill_areas(auctions, acache))
            ACACHE.write_text(json.dumps(acache))
        rec = {k: v for k, v in c.items() if k != "fetch"}
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
