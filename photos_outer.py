"""Building / lot photos for the outer counties whose assessors publish only through qPublic.

    find(slug, parcels, cache, limit=None) -> {parcel_id: [photo, ...]}      (photo dict: see photo_store.py)

Counties: carroll, clarke, coweta, dawson, floyd, heard, jackson, lumpkin, meriwether, pickens, polk, spalding,
troup, walton. qPublic / Beacon (Schneider) and its photo hosts are never contacted, nor are listing sites, web
archives or anything behind a captcha or browser challenge. Street-level and aerial imagery live in
photos_generic.py.

Photo sources (tested 2026-10-03 on every distinct parcel: 31 parcels with a photo, 81 photos, 9.5 MB):
  (1) GNAHRGIS, all 14 counties: the state's historic resources survey database (Georgia DCA Historic
      Preservation Division with the University of Georgia, www.gnahrgis.org). Field surveyors photograph every
      building of about 50 years and older in the surveyed towns; each record has a street address, a point and
      the exterior photographs. The site is entered through its own "Public Access - No Login Required" button,
      which asks the visitor to tick "I have read and understand the disclaimer" (an accuracy disclaimer, no
      terms of use) - the same kind of click-through as the DeKalb disclaimer in photos_metro.py. Set
      ACCEPT_DISCLAIMER["gnahrgis"] = False to switch the source off. The county's records are read in pages of
      1000, the photo list is asked only for records whose address matches a parcel.
      Photos exist for the surveys of 2002 and later (FindIt county surveys, City of Rome 2020/2024, Monroe
      2021-2025, Hogansville 2016, LaGrange 2019, Callaway Mills / Manchester 2024, Grantville, Bowdon,
      Carrollton, Dawsonville, Winterville ...); the 1988-2005 statewide survey has records but no pictures.
      Result: floyd 18 parcels, walton 5, meriwether 4, troup 2, jackson 1, pickens 1; the other eight
      counties have survey records but none at the address of a tax sale parcel (or none with pictures).
  (2) floyd: "North Rome Historic Resources Survey", a public Survey123 feature layer of the Rome-Floyd County
      GIS office (809 surveyed buildings, 16 of them tax sale parcels; so far only 52 records carry photo
      attachments and none of those is a tax sale parcel, so this source finds nothing yet).
  (3) floyd: Rome-Floyd County Land Bank Authority inventory (ePropertyPlus public site), matched by parcel
      number. Mostly vacant lots taken in through tax foreclosure; none of the 17 lots on offer is in the tax
      sale data today. Most of its pictures are screenshots of Google Street View or of a map: those are
      rejected (file name, and OCR of the Google watermark / "Image capture" line with tesseract; without
      tesseract every picture of this source is rejected), so only the authority's own photographs are kept.
  Checked and found without parcel pictures: every county / city / regional commission ArcGIS organisation and
  server (photo fields are yes/no flags; attachments are hydrants, signs, zoning files), the tax commissioner
  sites with their Government Window buckets and WordPress media libraries (Meriwether's June 2024 sale list
  embeds qPublic aerial map screenshots, not photos), the other land banks, Bid4Assets (courtesy notice only),
  National Register lists and OpenStreetMap image tags (no tax sale parcel among them).

Matching. A survey record is tied to a parcel only when house number and street name are the same and
  - street type and direction prefix agree too, and the two points (when both are known) are not far apart, or
  - the survey point lies within 60 m of the parcel (covers "1705 GORDON ST" vs "1705 Gordon Ave NE").
  A parcel without coordinates is matched on the full address alone and only when all matching records lie in
  one town (and in the parcel's town, when the parcel names one). Land bank lots are matched by parcel number.

Cache keys: "<slug>|<parcel>" -> list of photo dicts ([] = checked, none); "_hashes" -> {hash: {cache key:
stored file}} and "_placeholders" -> hashes, so that a picture served for three or more parcels is recognised
as a placeholder and dropped everywhere (same layout as photos_metro.py). Nothing is cached for a parcel when a
request for it failed on the network. find() never raises.

Command line (test / bulk run):  python photos_outer.py [--limit N] [slug ...]
"""
import http.cookiejar
import io
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

import photo_store as ps

PAUSE = 0.3
BIG_PAUSE = 1.0         # between downloads of the 5-13 MB survey originals
TIMEOUT = 40
MAX_PHOTOS = 3
MAX_RECORDS = 4         # survey records looked at per parcel
PLACEHOLDER_AT = 3      # the same picture on this many parcels is a "no photo" card
NEAR_M = 60             # address differs in street type / direction: the survey point must be this close
FAR_M = 400             # same address but further apart than this (plus the lot size): a different place

SLUGS = ("carroll", "clarke", "coweta", "dawson", "floyd", "heard", "jackson", "lumpkin", "meriwether",
         "pickens", "polk", "spalding", "troup", "walton", "fulton", "dekalb", "cobb", "clayton", "gwinnett", "henry", "douglas", "rockdale", "fayette", "forsyth")

# Plain accuracy disclaimers that are acknowledged on the visitor's behalf (see the module text).
ACCEPT_DISCLAIMER = {"gnahrgis": True}

GNAHRGIS = "https://www.gnahrgis.org"
GNAHRGIS_CREDIT = "Georgia Historic Resources Survey (GNAHRGIS)"
FIPS = {"carroll": 13045, "clarke": 13059, "coweta": 13077, "dawson": 13085, "floyd": 13115, "heard": 13149,
        "jackson": 13157, "lumpkin": 13187, "meriwether": 13199, "pickens": 13227, "polk": 13233,
        "spalding": 13255, "troup": 13285, "walton": 13297,
        "fulton": 13121, "dekalb": 13089, "cobb": 13067, "clayton": 13063, "gwinnett": 13135, "henry": 13151,
        "douglas": 13097, "rockdale": 13247, "fayette": 13113, "forsyth": 13117}
METRO = ("fulton", "dekalb", "cobb", "clayton", "gwinnett", "henry", "douglas", "rockdale", "fayette", "forsyth")

# Public ArcGIS feature layers of building surveys with photo attachments.
ARC_SURVEYS = {
    "floyd": [{
        "layer": "https://services2.arcgis.com/nV67H1IJR8GS6SAA/arcgis/rest/services/"
                 "survey123_be6bc6e52189470f9be6c77b8904f6ea/FeatureServer/0",
        "oid": "objectid", "addr": "_2b_address", "date": "CreationDate", "kind": "assessor",
        "credit": "Rome-Floyd County GIS, North Rome Historic Resources Survey",
        "page": "https://www.arcgis.com/home/item.html?id=df458532cba9423ea3b547c2a68310c1",
    }],
}

# Land bank inventories on ePropertyPlus (public site, parcel number on every property).
LAND_BANKS = {
    "floyd": [{"host": "https://public-rflba.epropertyplus.com", "kind": "listing",
               "credit": "Rome-Floyd County Land Bank Authority", "screenshots": True}],
}

PLACEHOLDERS = set()
STATS = {}              # per county counters from the last find() calls, for reporting
_last = [0.0]


class _Down(Exception):
    """A request failed on the network (or the service answered with an error)."""


# ---------------------------------------------------------------- small helpers

def _stat(slug, name, n=1):
    s = STATS.setdefault(slug, {})
    s[name] = s.get(name, 0) + n


def _pid(p):
    try:
        return str(p.get("parcel") or "").strip()
    except Exception:
        return ""


def _wait(pause=PAUSE):
    wait = pause - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)


def _get(url, timeout=TIMEOUT, headers=None, pause=PAUSE):
    """Bytes or None. Requests are spaced `pause` seconds apart; one retry after a dropped connection."""
    for attempt in (0, 1):
        _wait(pause if not attempt else 3.0)
        data, _ = ps.fetch(url, headers=headers, timeout=timeout)
        _last[0] = time.time()
        if data is not None:
            return data
    return None


def _load_json(data, what=""):
    if data is None:
        raise _Down(what)
    try:
        j = json.loads(data.decode("utf-8", "replace"))
    except Exception:
        raise _Down(what)
    if isinstance(j, dict) and (j.get("error") or j.get("success") is False or j.get("Errors")):
        raise _Down(what)
    return j


def _get_json(url):
    return _load_json(_get(url, headers={"Accept": "application/json"}), url)


def _day(ms):
    try:
        return time.strftime("%Y-%m-%d", time.gmtime(int(ms) / 1000)) if ms else None
    except Exception:
        return None


def _num(v):
    try:
        v = float(v)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _dist(a, b):
    """Metres between two (lat, lon) pairs, None when either is missing."""
    if not a or not b or None in a or None in b:
        return None
    r, p = 6371000.0, math.radians
    x = math.sin(p(b[0] - a[0]) / 2) ** 2 + math.cos(p(a[0])) * math.cos(p(b[0])) * math.sin(p(b[1] - a[1]) / 2) ** 2
    return 2 * r * math.asin(math.sqrt(x))


def _usable(data):
    """A real picture: decodes, at least 200 px on the short side, not a flat colour card."""
    return bool(data) and ps.is_image(data, min_bytes=4000, min_side=200) and not ps.is_blank(data)


def _is_html(data):
    return data[:300].lstrip().lower().startswith((b"<!doctype", b"<html"))


_GSV = re.compile(r"goog[il1!|]e|image\s*capture|street\s*view|see more dates", re.I)


def _streetview_screenshot(data):
    """True when OCR finds Google Street View furniture in the picture (the watermark, the "Image capture" line,
    the address card of the Maps window); None when it cannot be checked (no tesseract)."""
    if not shutil.which("tesseract"):
        return None
    try:
        from PIL import Image, ImageOps
        im = Image.open(io.BytesIO(data)).convert("L")
        w, h = im.size
        boxes = [(0, int(h * 0.88), w, h), (int(w * 0.25), int(h * 0.80), int(w * 0.75), h),
                 (int(w * 0.55), int(h * 0.93), w, h), (0, 0, int(w * 0.5), int(h * 0.25))]
        for b in boxes:
            c = im.crop(b)
            sc = max(1.0, min(4.0, 2200 / c.width))
            c = c.resize((int(c.width * sc), int(c.height * sc)), Image.LANCZOS)
            auto = ImageOps.autocontrast(c)
            for v in (c.point(lambda x: 0 if x >= 225 else 255), auto, ImageOps.invert(auto)):
                with tempfile.NamedTemporaryFile(suffix=".png") as f:
                    v.save(f.name)
                    txt = subprocess.run(["tesseract", f.name, "-", "--psm", "11"], capture_output=True, text=True,
                                         timeout=40).stdout
                if _GSV.search(txt):
                    return True
        return False
    except Exception:
        return None


def _exists(photo):
    try:
        return bool(photo.get("url")) or (ps.HERE / photo["src"]).is_file()
    except Exception:
        return False


def _remove(src):
    try:
        (ps.HERE / src).unlink()
    except Exception:
        pass


# ---------------------------------------------------------------- addresses

_DIRS = {"N": "N", "S": "S", "E": "E", "W": "W", "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
         "NE": "NE", "NW": "NW", "SE": "SE", "SW": "SW"}
_TYPES = {"ST": "ST", "STREET": "ST", "AVE": "AVE", "AV": "AVE", "AVENUE": "AVE", "RD": "RD", "ROAD": "RD",
          "DR": "DR", "DRIVE": "DR", "LN": "LN", "LANE": "LN", "CT": "CT", "COURT": "CT", "CIR": "CIR",
          "CIRCLE": "CIR", "PL": "PL", "PLACE": "PL", "BLVD": "BLVD", "BOULEVARD": "BLVD", "HWY": "HWY",
          "HIGHWAY": "HWY", "TER": "TER", "TERRACE": "TER", "TRL": "TRL", "TRAIL": "TRL", "WAY": "WAY",
          "PKWY": "PKWY", "PARKWAY": "PKWY", "ALY": "ALY", "ALLEY": "ALY", "ALLY": "ALY", "SQ": "SQ",
          "SQUARE": "SQ", "LOOP": "LOOP", "PT": "PT", "POINT": "PT", "RUN": "RUN", "XING": "XING",
          "CROSSING": "XING", "EXT": "EXT", "EXTENSION": "EXT"}
_NUM = re.compile(r"^\s*(\d+[A-Z]?)\b\s*(1/2)?\s*([A-D](?:\s*(?:&|AND)\s*[A-D])+(?=\s))?\s+(.+)$")


def addr_parts(s):
    """'205 W Callahan St NE Rome, GA 30161' -> ('205', 'CALLAHAN', 'ST', 'W'); None without a house number.
    The tuple is (number, street name, street type or '', direction prefix or ''). Unit letters after the number
    ('405 A&B STONEWALL ST') and everything after the street type (postal quadrant, city) are dropped."""
    try:
        t = re.split(r"[,\n\r;]", str(s or "").upper())[0]
        m = _NUM.match(t)
        if not m or int(re.sub(r"\D", "", m.group(1))) == 0:
            return None
        num = m.group(1) + (" 1/2" if m.group(2) else "")
        toks = [x for x in re.sub(r"[^0-9A-Z ]", " ", m.group(4).replace("'", "")).split() if x]
        if not toks:
            return None
        last = max((i for i, x in enumerate(toks) if x in _TYPES), default=None)
        typ = ""
        if last is not None and last > 0:
            typ, toks = _TYPES[toks[last]], toks[:last]
        else:
            while len(toks) > 1 and toks[-1] in _DIRS:
                toks = toks[:-1]
        pre = ""
        if len(toks) > 1 and toks[0] in _DIRS:
            pre, toks = _DIRS[toks[0]], toks[1:]
        return num, " ".join(toks), typ, pre
    except Exception:
        return None


def same_place(a, b, pa=None, pb=None, far=FAR_M):
    """True only when two addresses certainly are the same property: same house number and street name, and
    either the street type and direction prefix agree as well (and the two points, when both are known, are
    not far apart) or the two points are within NEAR_M of each other."""
    if not a or not b or a[0] != b[0] or a[1] != b[1]:
        return False
    d = _dist(pa, pb)
    if a[2] == b[2] and a[3] == b[3]:
        return d is None or d <= far
    return d is not None and d <= NEAR_M


def _parcel_key(s):
    return re.sub(r"[^0-9A-Z]", "", str(s or "").upper())


def _point(p):
    try:
        lat, lon = _num(p.get("lat")), _num(p.get("lon"))
        return (lat, lon) if lat is not None and lon is not None else None
    except Exception:
        return None


def _town(s):
    """'Maysville (unincorporated)' -> '' (not a town the parcel is in); 'Athens-Clarke County' -> 'ATHENS'."""
    s = str(s or "").upper()
    if not s or "UNINCORPORATED" in s:
        return ""
    return re.sub(r"[^A-Z]", "", re.split(r"[-,(]", s)[0])


def _matches(parcel, rows):
    """Survey rows ({"parts", "pt", "town", ...}) that certainly describe the parcel, nearest first."""
    parts, pt = addr_parts(parcel.get("address")), _point(parcel)
    if not parts:
        return []
    far = FAR_M + math.sqrt(max(0.0, _num(parcel.get("acres")) or 0.0) * 4047)
    hits = [r for r in rows if same_place(parts, r["parts"], pt, r["pt"], far)]
    if hits and pt is None:                 # no coordinates to confirm with: the town must be unambiguous
        towns, mine = {r.get("town") or "" for r in hits}, _town(parcel.get("area"))
        if len(towns) > 1 or (mine and towns != {""} and mine not in towns):
            return []
    hits.sort(key=lambda r: (_dist(pt, r["pt"]) or 0, -(r.get("rank") or 0)))
    return hits


# ---------------------------------------------------------------- source 1: GNAHRGIS (statewide)

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class _GnSession:
    """Public (no login) session of www.gnahrgis.org; one for the whole run."""

    def __init__(self):
        self.cookie = None

    def enter(self):
        """Open the public entrance: fetch the form token, acknowledge the disclaimer, press "Enter Site"."""
        if not ACCEPT_DISCLAIMER.get("gnahrgis"):
            raise _Down("gnahrgis disclaimer not accepted")
        jar = http.cookiejar.CookieJar()
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), _NoRedirect())
        op.addheaders = [("User-Agent", ps.UA)]
        try:
            _wait()
            with op.open(GNAHRGIS + "/PublicHome/Index", timeout=TIMEOUT) as r:
                page = r.read().decode("utf-8", "replace")
            tok = re.search(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', page)
            if not tok or "Public Access" not in page:
                raise _Down("gnahrgis entrance changed")
            body = urllib.parse.urlencode({"__RequestVerificationToken": tok.group(1), "UserName": "public",
                                           "Password": "password", "LogInQuestion": "N"}).encode()
            _wait()
            try:
                op.open(GNAHRGIS + "/PublicHome/Login", data=body, timeout=TIMEOUT).close()
            except urllib.error.HTTPError as e:     # the 302 to the (1.3 MB) start page is not followed
                if e.code not in (301, 302, 303):
                    raise
        except _Down:
            raise
        except Exception:
            raise _Down("gnahrgis entrance")
        finally:
            _last[0] = time.time()
        if not any(c.name == ".ASPXAUTH" for c in jar):
            raise _Down("gnahrgis entrance refused")
        self.cookie = "; ".join("%s=%s" % (c.name, c.value) for c in jar)

    def post(self, path, fields):
        """JSON answer of one of the site's grid requests; enters (again) when the session is missing or stale."""
        for attempt in (0, 1, 2):
            if not self.cookie:
                self.enter()
            data = None
            try:
                _wait(PAUSE if not attempt else 3.0)
                req = urllib.request.Request(GNAHRGIS + path, data=urllib.parse.urlencode(fields).encode(), headers={
                    "User-Agent": ps.UA, "Cookie": self.cookie, "X-Requested-With": "XMLHttpRequest",
                    "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=120) as r:
                    data = r.read()
            except Exception:
                data = None
            finally:
                _last[0] = time.time()
            if data is not None and not _is_html(data):
                return _load_json(data, path)
            if data is not None:
                self.cookie = None          # the login page came back: session expired
        raise _Down(path)


_GN = _GnSession()


class _Gnahrgis:
    def __init__(self, slug):
        self.slug, self.rows, self.index = slug, None, {}

    def load(self):
        if self.rows is not None:
            return
        rows, page = [], 1
        while True:
            j = _GN.post("/HistoricResource/GetList", {
                "sort": "", "page": page, "pageSize": 1000, "group": "", "filter": "",
                "HistoricResourceAddress": "", "HistoricResourceCityName": "",
                "HistoricResourceCountyIdToString": FIPS[self.slug], "HistoricResourceAppendToSurveyIdToString": "",
                "HistoricResourceId": "", "HistoricResourceName": "", "HistoricResourceSurveyIdToString": "",
                "searchTabIndex": 0, "basicOrAdvancedSearch": "B"})
            got = j.get("Data")
            if not isinstance(got, list):
                raise _Down("gnahrgis list")
            for r in got:
                parts = addr_parts(r.get("Addresses"))
                if not parts or r.get("Id") is None or r.get("ArchiveId"):
                    continue
                lat, lon = _num(r.get("Latitude")), _num(r.get("Longitude"))
                rows.append({"id": r["Id"], "survey": r.get("SurveyId"), "name": str(r.get("SurveyName") or "").strip(),
                             "parts": parts, "town": _town(r.get("CityOrCommunityName")), "rank": r.get("SurveyId") or 0,
                             "pt": (lat, lon) if lat and lon else None})
            page += 1
            if not got or (page - 1) * 1000 >= int(j.get("Total") or 0):
                break
        self.rows = rows
        for r in rows:
            self.index.setdefault(r["parts"][:2], []).append(r)

    def candidates(self, parcel):
        parts = addr_parts(parcel.get("address"))
        hits = _matches(parcel, self.index.get(parts[:2], [])) if parts else []
        out = []
        for r in hits[:MAX_RECORDS]:
            j = _GN.post("/HistoricResourceImageFile/GetList", {
                "sort": "", "page": 1, "pageSize": 100, "group": "", "filter": "",
                "resourceIdToString": r["id"], "surveyIdToString": r["survey"]})
            year = re.findall(r"(?<!\d)(?:19|20)\d\d(?!\d)", r["name"])
            files = []
            for im in j.get("Data") or []:
                cat, name = str(im.get("FileCategoryTypeDescription") or "").lower(), str(im.get("FileName") or "")
                if im.get("Id") is None or re.search(r"\b(map|plan|plat|drawing|sketch|document|form)s?\b", cat + " " + name.lower()):
                    continue
                order = 0 if "exterior" in cat else 2 if "interior" in cat else 1
                files.append((order, name.lower(), im))
            for _, _, im in sorted(files, key=lambda x: x[:2]):
                meta = {"kind": "assessor", "credit": GNAHRGIS_CREDIT + (", " + r["name"] if r["name"] else ""),
                        "page": GNAHRGIS + "/", "headers": {"Cookie": _GN.cookie}, "session": _GN, "pause": BIG_PAUSE,
                        "timeout": 240}
                m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})$", str(im.get("ImageDateFormatted") or "").strip())
                if m:
                    meta["date"] = "%s-%02d-%02d" % (m.group(3), int(m.group(1)), int(m.group(2)))
                elif len(year) == 1:
                    meta["date"] = year[0]
                out.append(("%s/HistoricResourceImageFile/GetFile/%s" % (GNAHRGIS, im["Id"]), meta))
        return out


# ---------------------------------------------------------------- source 2: ArcGIS survey layers

class _ArcSurvey:
    """A public ArcGIS feature layer of surveyed buildings with photo attachments."""

    def __init__(self, cfg):
        self.cfg, self.rows = cfg, None

    def load(self):
        if self.rows is not None:
            return
        c, rows, off = self.cfg, [], 0
        fields = ",".join(x for x in (c["oid"], c["addr"], c.get("date")) if x)
        while True:
            j = _get_json(c["layer"] + "/query?where=1%3D1&outFields=" + fields + "&returnGeometry=true&outSR=4326"
                          + "&orderByFields=" + c["oid"] + "&resultOffset=" + str(off) + "&resultRecordCount=1000&f=json")
            feats = j.get("features")
            if not isinstance(feats, list):
                raise _Down(c["layer"])
            for f in feats:
                a, g = f.get("attributes") or {}, f.get("geometry") or {}
                parts = addr_parts(a.get(c["addr"]))
                if parts and a.get(c["oid"]) is not None:
                    lat, lon = _num(g.get("y")), _num(g.get("x"))
                    rows.append({"oid": a[c["oid"]], "parts": parts, "date": _day(a.get(c.get("date"))),
                                 "pt": (lat, lon) if lat is not None and lon is not None else None})
            if not feats or not j.get("exceededTransferLimit"):
                break
            off += len(feats)
        self.rows = rows

    def candidates(self, parcel):
        hits = _matches(parcel, self.rows)[:MAX_RECORDS]
        if not hits:
            return []
        c, out = self.cfg, []
        j = _get_json(c["layer"] + "/queryAttachments?objectIds=" + ",".join(str(r["oid"]) for r in hits) + "&f=json")
        groups = {g.get("parentObjectId"): g.get("attachmentInfos") or [] for g in j.get("attachmentGroups") or []}
        for r in hits:
            for a in sorted(groups.get(r["oid"], []), key=lambda x: x.get("id") or 0):
                if not str(a.get("contentType") or "").startswith("image/") or a.get("id") is None:
                    continue
                meta = {"kind": c["kind"], "credit": c["credit"], "page": c["page"]}
                if r["date"]:
                    meta["date"] = r["date"]
                out.append(("%s/%s/attachments/%s" % (c["layer"], r["oid"], a["id"]), meta))
        return out


# ---------------------------------------------------------------- source 3: land bank inventories

class _LandBank:
    """A land bank's public ePropertyPlus inventory."""

    def __init__(self, cfg):
        self.cfg, self.rows = cfg, None
        self.base = cfg["host"] + "/landmgmtpub/remote/public/property/"

    def load(self):
        if self.rows is not None:
            return
        rows, start = {}, 0
        while True:
            j = _get_json(self.base + "getPublishedProperties?limit=200&page=%d&start=%d" % (start // 200 + 1, start))
            got = j.get("rows")
            if not isinstance(got, list):
                raise _Down(self.base)
            for r in got:
                k = _parcel_key(r.get("parcelNumber"))
                if k and r.get("id") is not None:
                    rows[k] = r
            start += len(got)
            if not got or start >= int(j.get("size") or 0):
                break
        self.rows = rows

    def candidates(self, parcel):
        r = self.rows.get(_parcel_key(parcel.get("parcel")))
        if not r:
            return []
        j = _get_json(self.base + "getImages?ownerType=PROPERTY&page=1&start=0&limit=50&ownerId=%s" % r["id"])
        page = self.base + "viewSummary?parcelNumber=" + urllib.parse.quote(str(r.get("parcelNumber")))
        out = []
        for im in sorted(j.get("rows") or [], key=lambda x: x.get("seq") or 0):
            big, name = str(im.get("bigimg") or ""), str(im.get("filename") or "")
            if (not big.startswith("/landmgmt/remote/image/") or not name or im.get("published") is False
                    or re.search(r"\bmap\b|map view|notavailable", name + " " + big, re.I)):
                continue                                # the stock "photo not available" card, or a map
            url = self.cfg["host"] + big.replace("/landmgmt/remote/image/", "/landmgmtpub/remote/public/property/")
            meta = {"kind": self.cfg["kind"], "credit": self.cfg["credit"], "page": page,
                    "screenshots": bool(self.cfg.get("screenshots"))}
            if _day(im.get("capturedDate") or im.get("dateUploaded")):
                meta["date"] = _day(im.get("capturedDate") or im.get("dateUploaded"))
            out.append((url.replace(" ", "%20"), meta))
        return out


def _sources(slug):
    if slug not in SLUGS:
        return []
    return (([_Gnahrgis(slug)] if ACCEPT_DISCLAIMER.get("gnahrgis") and slug in FIPS else [])
            + [_ArcSurvey(c) for c in ARC_SURVEYS.get(slug, [])]
            + [_LandBank(c) for c in LAND_BANKS.get(slug, [])])


# ---------------------------------------------------------------- placeholder / duplicate bookkeeping

def _placeholders(cache):
    try:
        PLACEHOLDERS.update(cache.get("_placeholders") or [])
    except Exception:
        pass
    cache["_placeholders"] = sorted(PLACEHOLDERS)


def _register(cache, h, key, src):
    """Remember which parcel uses picture hash h. Returns False (and purges it everywhere) once the same picture
    has turned up for PLACEHOLDER_AT different parcels."""
    reg = cache.setdefault("_hashes", {})
    users = reg.setdefault(h, {})
    users[key] = src
    if len(users) < PLACEHOLDER_AT:
        return True
    PLACEHOLDERS.add(h)
    cache["_placeholders"] = sorted(PLACEHOLDERS)
    for k, s in users.items():
        if isinstance(cache.get(k), list):
            cache[k] = [x for x in cache[k] if x.get("src") != s]
        _remove(s)
        _stat(k.split("|")[0], "placeholders_rejected")
    reg.pop(h, None)
    return False


def _forget(cache, key):
    reg = cache.get("_hashes") or {}
    for h in list(reg):
        reg[h].pop(key, None)
        if not reg[h]:
            reg.pop(h, None)


def _store(slug, pid, key, cands, cache):
    """Download, verify, de-duplicate and save the candidate pictures of one parcel.
    Returns (photos, complete); complete is False when a download failed."""
    photos, seen, complete = [], set(), True
    for url, meta in cands:
        if len(photos) >= MAX_PHOTOS:
            break
        raw = _get(url, timeout=meta.get("timeout", 90), headers=meta.get("headers"), pause=meta.get("pause", PAUSE))
        if raw is not None and _is_html(raw) and meta.get("session"):
            try:                                    # the public session ran out: enter again, ask once more
                meta["session"].enter()
                raw = _get(url, timeout=meta.get("timeout", 90), headers={"Cookie": meta["session"].cookie},
                           pause=meta.get("pause", PAUSE))
            except Exception:
                raw = None
        if raw is None or _is_html(raw):            # dropped connection, or a login / error page instead of a file
            complete = False
            _stat(slug, "download_failed")
            continue
        h = ps.digest(raw)
        if h in PLACEHOLDERS:
            _stat(slug, "placeholders_rejected")
            continue
        if h in seen or not _usable(raw):
            continue
        seen.add(h)
        if meta.get("screenshots") and _streetview_screenshot(raw) is not False:
            _stat(slug, "screenshots_rejected")     # somebody else's street-level imagery, not a photo of theirs
            continue
        src = ps.save(slug, pid, ("h%d" % (len(photos) + 1)) if slug in METRO else len(photos) + 1, raw)
        if not src:
            continue
        photo = {"src": src, "kind": meta["kind"], "credit": meta["credit"]}
        if meta.get("date"):
            photo["date"] = meta["date"]
        photo["page"] = meta["page"]
        photos.append(photo)
        cache[key] = photos
        if not _register(cache, h, key, src):       # third parcel with this picture: it was a placeholder
            photos = [x for x in photos if x["src"] != src]
            cache[key] = photos
    return photos, complete


# ---------------------------------------------------------------- find()

def find(slug, parcels, cache, limit=None):
    """Photos for the given parcels of one county. `limit` caps new lookups (for testing)."""
    out = {}
    try:
        todo, asked = [], []
        for p in parcels or []:
            pid = _pid(p)
            if not pid or pid in asked:
                continue
            asked.append(pid)
            hit = cache.get(f"{slug}|{pid}")
            if isinstance(hit, list) and all(_exists(x) for x in hit):
                continue
            todo.append(p)
        sources = _sources(slug)
        if sources and todo and (limit is None or limit > 0):
            _placeholders(cache)
            ready, all_up = [], True
            for s in sources:
                for attempt in (0, 1):
                    try:
                        s.load()
                        ready.append(s)
                        break
                    except Exception:
                        if attempt:
                            all_up = False
                            _stat(slug, "source_down")
                        else:
                            time.sleep(3)
            new = 0
            for p in todo if ready else []:
                if limit is not None and new >= limit:
                    break
                new += 1
                pid = _pid(p)
                key, cands, complete = f"{slug}|{pid}", [], all_up
                for s in ready:
                    try:
                        cands += s.candidates(p)
                    except Exception:
                        complete = False
                        _stat(slug, "failed")
                _forget(cache, key)
                cache.pop(key, None)
                try:
                    photos, got_all = _store(slug, pid, key, cands, cache) if cands else ([], True)
                except Exception:
                    photos, got_all = [], False
                photos = [x for x in (cache.get(key) or photos) if _exists(x)]
                if complete and got_all:
                    cache[key] = photos                 # [] = checked, none
                else:                                   # a request failed: leave the parcel for the next run
                    _forget(cache, key)
                    cache.pop(key, None)
                    if photos:
                        out[pid] = [dict(x) for x in photos]
                _stat(slug, "looked_up")
                if photos:
                    _stat(slug, "with_photo")
        for pid in asked:
            hit = cache.get(f"{slug}|{pid}")
            if isinstance(hit, list) and hit:
                good = [dict(x) for x in hit if _exists(x)]
                if good:
                    out[pid] = good
    except Exception:
        pass
    return out


# ---------------------------------------------------------------- command line

def _county_parcels(slug):
    d = json.loads((ps.HERE / "data" / f"{slug}.json").read_text())
    seen = {}
    for a in d.get("auctions") or []:
        for p in a.get("parcels") or []:
            if _pid(p) and _pid(p) not in seen:
                seen[_pid(p)] = p
    return list(seen.values())


def main(argv):
    limit, slugs = None, []
    it = iter(argv)
    for a in it:
        if a == "--limit":
            limit = int(next(it))
        else:
            slugs.append(a)
    path = ps.ROOT / "outer_cache.json"
    try:
        cache = json.loads(path.read_text())
    except Exception:
        cache = {}
    for slug in slugs or SLUGS:
        parcels = _county_parcels(slug)
        got = find(slug, parcels, cache, limit)
        files = [ps.HERE / x["src"] for v in got.values() for x in v if x.get("src")]
        print("%-11s parcels %4d  with photo %3d  photos %3d  bytes %8d  %s" % (
            slug, len(parcels), len(got), len(files), sum(f.stat().st_size for f in files if f.is_file()),
            STATS.get(slug) or ("no source" if not _sources(slug) else "")), flush=True)
        ps.ROOT.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache, indent=1, sort_keys=True))


if __name__ == "__main__":
    main(sys.argv[1:])
