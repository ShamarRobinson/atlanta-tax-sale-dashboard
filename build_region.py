#!/usr/bin/env python3
"""Build region.json: every Georgia/Alabama county touching a circle around downtown Atlanta.

The radius is the distance from downtown Atlanta to the nearest edge of Athens-Clarke County (the farthest of the
named counties: Fayette, Bartow, Paulding, Douglas, Newton, Clarke and Hall), rounded up to 52 miles.
"""
import io
import json
import urllib.request
import zipfile

import shapefile
from pyproj import Transformer
from shapely.geometry import Point, mapping, shape
from shapely.ops import transform

URL = "https://www2.census.gov/geo/tiger/GENZ2023/shp/cb_2023_us_county_500k.zip"
CENTER = (33.749, -84.388)
RADIUS_MI = 52


def main():
    z = zipfile.ZipFile(io.BytesIO(urllib.request.urlopen(URL, timeout=300).read()))
    base = "cb_2023_us_county_500k"
    sf = shapefile.Reader(shp=io.BytesIO(z.read(base + ".shp")), dbf=io.BytesIO(z.read(base + ".dbf")), shx=io.BytesIO(z.read(base + ".shx")))
    fields = [f[0] for f in sf.fields[1:]]
    fwd = Transformer.from_crs(4326, 5070, always_xy=True).transform
    inv = Transformer.from_crs(5070, 4326, always_xy=True).transform
    mi = 1609.34
    atl = Point(*fwd(CENTER[1], CENTER[0]))
    circ = atl.buffer(RADIUS_MI * mi)

    def rnd(o):
        if isinstance(o, (list, tuple)):
            if o and isinstance(o[0], (int, float)):
                return [round(o[0], 4), round(o[1], 4)]
            return [rnd(x) for x in o]
        if isinstance(o, dict):
            return {k: rnd(v) for k, v in o.items()}
        return o

    out = []
    for sr in sf.iterShapeRecords():
        r = dict(zip(fields, sr.record))
        if r["STATEFP"] not in ("13", "01"):
            continue
        g = transform(fwd, shape(sr.shape.__geo_interface__))
        if g.intersects(circ):
            out.append(dict(name=r["NAME"], state=r["STUSPS"], fips=r["GEOID"], near=round(g.distance(atl) / mi, 1),
                            inside=round(g.intersection(circ).area / g.area * 100), geom=rnd(mapping(transform(inv, g.simplify(400))))))
    out.sort(key=lambda x: x["near"])
    json.dump(dict(center=list(CENTER), radius_mi=RADIUS_MI, counties=out, circle=rnd(mapping(transform(inv, circ.simplify(200))))),
              open("region.json", "w"), separators=(",", ":"))
    print(len(out), "counties")


if __name__ == "__main__":
    main()
