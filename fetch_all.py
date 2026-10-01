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

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
DATA.mkdir(exist_ok=True)
CACHE = DATA / "geocache.json"
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
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
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
                auctions = merge(auctions, fresh)
                print(f"  {len(fresh)} auctions, {sum(len(a['parcels']) for a in fresh)} parcel rows")
            except Exception as e:
                err = f"{type(e).__name__}: {e}"[:200]
                print("  FAILED", err)
                traceback.print_exc()
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
        today = datetime.now().strftime("%Y-%m-%d")
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
