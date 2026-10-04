"""Assessor photos (and, where cheap, year built / living area) for the core metro counties.

    find(slug, parcels, cache, limit=None)    -> {parcel_id: [photo, ...]}      (photo dict: see photo_store.py)
    details(slug, parcels, cache, limit=None) -> {parcel_id: {"yearBuilt": int, "sqft": int}}

Photo sources (tested 2026-10-03):
  dekalb  - Tyler iasWorld document store behind publicaccess.dekalbtaxga.gov: the photo list comes from
            /api/documents/Parcel Photo/<jur>/<base64 parcel>, the pictures from /api/document/<id>/standard, with
            /iDoc2/Services/GetPhoto.ashx as the fallback. Needs the accuracy disclaimer click-through (approved by the
            site owner on 2026-10-01, the same approval prices_metro.py relies on).
  clayton - same software (publicaccess.claytoncountyga.gov) but the county has loaded no pictures: every parcel
            answers with Tyler's stock "Sorry, no photo available" card, which is rejected as a placeholder.
  fulton  - same software (iaspublicaccess.fultoncountyga.gov), but the host no longer answers (connection reset
            from this server, error page in a desktop browser), so it is configured below but switched off.
  cobb, gwinnett, henry, douglas, rockdale, fayette, forsyth - no source: their parcel photos are only on qPublic
            (not used here), Cobb's own iasWorld site sits behind a browser challenge, and none of the county GIS
            services, tax sale lists or tax payment sites carries pictures. find() returns {} and caches nothing.

Detail sources: dekalb (Property Appraisal residential / commercial datalets), cobb (assessor YearBuilt table),
gwinnett (Property Improvements table).

Cache keys: "<slug>|<parcel>" -> list of photo dicts ([] = checked, none); "d|<slug>|<parcel>" -> details dict
({} = checked, none); "_placeholders" -> hashes of "no photo" cards; "_hashes" -> {hash: {cache key: stored file}}
so that a picture served for three or more parcels is recognised as a placeholder and dropped everywhere.
Nothing is cached when a lookup fails on the network. Neither function raises.
"""
import base64
import http.cookiejar
import io
import json
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from html import unescape

import photo_store as ps
from ga_common import UA, clean, get_json

PAUSE = 0.3
TIMEOUT = 30
MAX_PHOTOS = 3
MAX_FAILS = 6          # consecutive failed lookups before a county is given up for this run
PLACEHOLDER_AT = 3     # the same picture on this many parcels is a "no photo available" card

ACCEPT_DISCLAIMER = {"dekalb": True, "clayton": True}   # plain accuracy disclaimers; approved by the site owner

# Tyler's stock 136x136 "Sorry, no photo available for this record" card (identical on every iasWorld site).
PLACEHOLDERS = {"31d3de69177f65511d815e26d369071d4f653a93"}
STATS = {}             # per county counters from the last find()/details() calls, for reporting


# ---------------------------------------------------------------- small helpers

def _stat(slug, name, n=1):
    s = STATS.setdefault(slug, {})
    s[name] = s.get(name, 0) + n


def _pid(p):
    try:
        return str(p.get("parcel") or "").strip()
    except Exception:
        return ""


def _num(s):
    try:
        v = float(re.sub(r"[^0-9.]", "", str(s)))
        return int(round(v))
    except (TypeError, ValueError):
        return None


def _year(s):
    v = _num(s)
    return v if v and 1700 <= v <= time.gmtime().tm_year + 1 else None


def _usable(data):
    """A real picture: decodes, at least 200 px on the short side, not a flat colour card."""
    return bool(data) and ps.is_image(data, min_bytes=4000, min_side=200) and not ps.is_blank(data)


def _caption_date(data, pid=""):
    """County street-level pictures carry a white strip with 'parcel MM/DD/YYYY' burned in under the image.
    Returns (has_strip, date): 'YYYY-MM-DD' when the strip was read with confidence (tesseract), 'YYYY' when
    only the year is certain, None when it could not be read."""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data)).convert("L")
        w, h = im.size
        hist = im.crop((0, int(h * 0.93), w, h)).histogram()
        if sum(hist[236:]) < 0.6 * sum(hist):
            return False, None
        # top edge of the strip: its left margin is pure white on every row (the text is centred)
        col = im.crop((0, 0, max(8, int(w * 0.08)), h)).resize((1, h), Image.BOX).tobytes()
        bot = h
        while bot > h - 8 and col[bot - 1] <= 235:      # some strips have a thin black frame
            bot -= 1
        top = bot
        while top > int(h * 0.6) and col[top - 1] > 235:
            top -= 1
        if bot - top < 12 or not shutil.which("tesseract"):
            return True, None
        strip = im.crop((0, top + 2, w, bot))
        if strip.height < 60:
            strip = strip.resize((int(w * 60 / strip.height), 60))
        now, want, seen = time.gmtime().tm_year, re.sub(r"\D", "", str(pid)), []
        with tempfile.NamedTemporaryFile(suffix=".png") as f:
            strip.save(f.name)
            for psm in ("7", "6"):
                txt = subprocess.run(["tesseract", f.name, "-", "--psm", psm], capture_output=True, text=True, timeout=30).stdout
                hits = [m for m in re.finditer(r"(\d{1,2})/(\d{1,2})/((?:19|20)\d\d)", txt)
                        if 1 <= int(m.group(1)) <= 12 and 1 <= int(m.group(2)) <= 31 and 1990 <= int(m.group(3)) <= now]
                if not hits:
                    continue
                m = hits[-1]
                day = "%04d-%02d-%02d" % (int(m.group(3)), int(m.group(1)), int(m.group(2)))
                # trusted when the parcel number in front of the date was read correctly, or two readings agree
                if (want and want in re.sub(r"\D", "", txt[:m.start()])) or day in seen:
                    return True, day
                seen.append(day)
        if seen and len({d[:4] for d in seen}) == 1:
            return True, seen[0][:4]
        return True, None
    except Exception:
        return False, None


def _exists(photo):
    try:
        return bool(photo.get("url")) or (ps.HERE / photo["src"]).is_file()
    except Exception:
        return False


# ---------------------------------------------------------------- placeholder / duplicate bookkeeping

def _placeholders(cache):
    """Merge the module level set with the cached list and return the live set."""
    try:
        PLACEHOLDERS.update(cache.get("_placeholders") or [])
    except Exception:
        pass
    cache["_placeholders"] = sorted(PLACEHOLDERS)
    return PLACEHOLDERS


def _mark_placeholder(cache, h):
    PLACEHOLDERS.add(h)
    cache["_placeholders"] = sorted(PLACEHOLDERS)


def _remove(src):
    try:
        (ps.HERE / src).unlink()
    except Exception:
        pass


def _register(cache, h, key, src):
    """Remember which parcel uses picture hash h. Returns False (and purges it everywhere) once the same
    picture has turned up for PLACEHOLDER_AT different parcels."""
    reg = cache.setdefault("_hashes", {})
    users = reg.setdefault(h, {})
    users[key] = src
    if len(users) < PLACEHOLDER_AT:
        return True
    _mark_placeholder(cache, h)
    for k, s in users.items():
        if isinstance(cache.get(k), list):
            cache[k] = [x for x in cache[k] if x.get("src") != s]
        _remove(s)
        _stat(k.split("|")[0], "placeholders_rejected")
    reg.pop(h, None)
    return False


def _forget(cache, key):
    """Drop a parcel from the hash registry (before it is looked up again)."""
    reg = cache.get("_hashes") or {}
    for h in list(reg):
        reg[h].pop(key, None)
        if not reg[h]:
            reg.pop(h, None)


# ---------------------------------------------------------------- Tyler iasWorld public access

class _Down(Exception):
    """The site could not be reached or did not answer usefully; nothing may be cached."""


class _Ias:
    def __init__(self, slug, base, credit, page, jur="000", parid=None, accept=False, use_docs=True):
        self.slug, self.base, self.credit, self.page, self.jur = slug, base.rstrip("/"), credit, page, jur
        self.parid = parid or (lambda s: s)
        self.accept, self.use_docs = accept, use_docs
        self.op = None
        self.token = None       # None = not asked yet, "" = photo viewer not available to the public role
        self.agreed = False

    # --- transport
    def _fetch(self, url, data=None, tries=3):
        """(final url, bytes, content type). Retries dropped connections; raises _Down otherwise."""
        if self.op is None:
            self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
            self.op.addheaders = [("User-Agent", UA), ("Accept", "*/*")]
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        last = None
        for i in range(tries):
            try:
                with self.op.open(url, body, timeout=TIMEOUT) as r:
                    return r.geturl(), r.read(), (r.headers.get("Content-Type") or "").lower()
            except urllib.error.HTTPError as e:
                last = e
                if e.code not in (429, 502, 503, 504):
                    break
            except Exception as e:  # reset, timeout, DNS
                last = e
            finally:
                time.sleep(PAUSE)
            time.sleep(1.5 * (i + 1))
        raise _Down(str(last))

    def _session(self):
        """Open the search page once and, where approved, agree to the accuracy disclaimer."""
        if self.agreed or not self.accept:
            return
        url, raw, _ = self._fetch(self.base + "/search/commonsearch.aspx?mode=realprop")
        if "disclaimer" in url.lower():
            html = raw.decode("utf-8", "replace")
            form = {}
            for tag in re.findall(r"<input[^>]*>", html):
                nm = re.search(r"name=[\"']([^\"']+)", tag)
                ty = re.search(r"type=[\"']([^\"']+)", tag)
                v = re.search(r"value=[\"']([^\"']*)", tag)
                if nm and (not ty or ty.group(1).lower() in ("hidden", "text")):
                    form[nm.group(1)] = unescape(v.group(1)) if v else ""
            form["btAgree"] = ""
            url, _, _ = self._fetch(url, form)
            if "disclaimer" in url.lower():
                raise _Down("disclaimer not accepted")
        self.agreed = True

    def reset(self):
        self.op, self.agreed, self.token = None, False, None

    # --- pictures
    def _viewer_token(self, parid):
        """The photo viewer page carries the token its document API expects."""
        if self.token is None:
            self._session()
            q = urllib.parse.quote(parid)
            url, raw, _ = self._fetch(f"{self.base}/idoc2/photoviewfixed.aspx?UseSearch=no&pin={q}&jur={self.jur}")
            m = re.search(r"var token = '([^']+)'", raw.decode("utf-8", "replace"))
            self.token = m.group(1) if m and "accesserror" not in url.lower() else ""
        return self.token

    def _documents(self, parid):
        """Photo records for a parcel, front view first. [] when the document API lists none or is not offered."""
        if not (self.use_docs and self.accept):
            return []
        tok = self._viewer_token(parid)
        if not tok:
            return []
        b64 = base64.b64encode(parid.encode()).decode()
        _, raw, _ = self._fetch(f"{self.base}/api/documents/{urllib.parse.quote('Parcel Photo')}/{self.jur}/{b64}?token={tok}")
        try:
            docs = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            raise _Down("document list is not JSON")
        out = []
        for d in docs if isinstance(docs, list) else []:
            f = d.get("Fields") or {}
            cat = clean(f.get("Photo Category"))
            if not d.get("Id") or re.search(r"sketch|plat|map|document|floor", cat, re.I):
                continue
            when = str(f.get("Photo Capture Date") or "")
            out.append({"id": d["Id"], "rank": d.get("Rank") or 99, "front": 0 if re.search(r"front|primary", cat + " " + clean(f.get("Title")), re.I) else 1,
                        "date": when[:10] if re.match(r"(19|20)\d\d-\d\d-\d\d", when) else None})
        out.sort(key=lambda x: (x["front"], x["rank"]))
        return out

    def _picture(self, url):
        """Bytes of one picture, b'' when the answer is not a picture; raises _Down on a network failure."""
        _, raw, ct = self._fetch(url)
        return raw if raw[:3] == b"\xff\xd8\xff" or raw[:4] == b"\x89PNG" or "image" in ct else b""

    def placeholder(self):
        """Ask for a parcel that cannot exist: whatever comes back is the site's 'no photo' card."""
        raw = self._picture(f"{self.base}/iDoc2/Services/GetPhoto.ashx?parid=00%20000%2000%20000ZZ&jur={self.jur}&Rank=1")
        return ps.digest(raw) if raw and ps.is_image(raw, min_bytes=500, min_side=20) else None

    def lookup(self, pid):
        """[(bytes, date), ...] candidate pictures of one parcel, best first. Raises _Down when unsure."""
        parid = self.parid(pid)
        if not parid:
            raise _Down("parcel id not understood")
        out = []
        docs = self._documents(parid)
        for d in docs[:MAX_PHOTOS + 2]:
            raw = self._picture(f"{self.base}/api/document/{urllib.parse.quote(d['id'], safe='')}/standard?token={self.token}")
            if raw:
                out.append((raw, d["date"]))
        if out:
            return out
        # No document list (or an empty one, which is also what an expired session returns): ask the photo
        # service directly, rank by rank, until it answers with the placeholder.
        q = urllib.parse.quote(parid)
        for rank in range(1, MAX_PHOTOS + 1):
            raw = self._picture(f"{self.base}/iDoc2/Services/GetPhoto.ashx?parid={q}&jur={self.jur}&Rank={rank}&size=1200x900")
            if not raw:
                if rank == 1:
                    raise _Down("photo service did not return a picture")
                break
            if ps.digest(raw) in PLACEHOLDERS:
                if rank == 1:
                    _stat(self.slug, "placeholders_rejected")
                break
            if not _usable(raw):
                break
            out.append((raw, None))
        if out and self.use_docs and self.accept and self.token:
            self.reset()        # the list said "none" but there are pictures: the session had gone stale
        return out


def _fulton_parid(pid):
    """'11-0821-0303-169-1' -> '11 082103031691'; '09F-1505-0078-050-5' -> '09F150500780505' (15 characters)."""
    t = [x for x in re.split(r"[^0-9A-Za-z]+", str(pid).upper()) if x]
    if len(t) < 2:
        return None
    rest = "".join(t[1:])
    return (t[0] + " " + rest) if len(t[0]) == 2 else t[0] + rest


def _ias(slug):
    if slug == "dekalb":
        return _Ias("dekalb", "https://publicaccess.dekalbtaxga.gov", "DeKalb County Property Appraisal",
                    "https://publicaccess.dekalbtaxga.gov/Datalets/Datalet.aspx?mode=dek_profile&UseSearch=no&pin={q}",
                    accept=ACCEPT_DISCLAIMER.get("dekalb", False))
    if slug == "clayton":
        return _Ias("clayton", "https://publicaccess.claytoncountyga.gov", "Clayton County Tax Assessor",
                    "https://publicaccess.claytoncountyga.gov/Datalets/Datalet.aspx?mode=profileall&UseSearch=no&pin={q}",
                    accept=ACCEPT_DISCLAIMER.get("clayton", False))
    if slug == "fulton":
        return _Ias("fulton", "https://iaspublicaccess.fultoncountyga.gov", "Fulton County Board of Assessors",
                    "https://iaspublicaccess.fultoncountyga.gov/Datalets/Datalet.aspx?mode=profileall&UseSearch=no&pin={q}",
                    parid=_fulton_parid, accept=False, use_docs=False)
    return None


# Fulton's iasWorld host answered neither this server nor a desktop browser on 2026-10-03 (the Board of Assessors
# now sends its property search to qPublic), so it is switched off; add "fulton" here if the host comes back.
PHOTO_SLUGS = ("dekalb",)


# ---------------------------------------------------------------- find()

def _store(site, slug, pid, key, cands, cache):
    """Verify, de-duplicate and save the candidate pictures of one parcel. Returns the photo list."""
    photos, seen = [], set()
    page = site.page.format(q=urllib.parse.quote(site.parid(pid) or pid))
    for raw, when in cands:
        if len(photos) >= MAX_PHOTOS:
            break
        h = ps.digest(raw)
        if h in PLACEHOLDERS:
            _stat(slug, "placeholders_rejected")
            continue
        if h in seen or not _usable(raw):
            continue
        seen.add(h)
        src = ps.save(slug, pid, len(photos) + 1, raw)
        if not src:
            continue
        photo = {"src": src, "kind": "assessor", "credit": site.credit}
        strip, shot = _caption_date(raw, pid)
        if shot or (when and not strip):    # the list's own date is only a batch date for captioned pictures
            photo["date"] = shot or when
        photo["page"] = page
        photos.append(photo)
        cache[key] = photos
        if not _register(cache, h, key, src):       # third parcel with this picture: it was a placeholder
            photos = [x for x in photos if x["src"] != src]
            cache[key] = photos
    return photos


def find(slug, parcels, cache, limit=None):
    """Photos for the given parcels of one county. `limit` caps new lookups (for testing)."""
    out = {}
    try:
        _placeholders(cache)
        todo, asked = [], []
        for p in parcels or []:
            pid = _pid(p)
            if not pid or pid in asked:
                continue
            asked.append(pid)
            key = f"{slug}|{pid}"
            hit = cache.get(key)
            if isinstance(hit, list) and all(_exists(x) for x in hit):
                continue
            todo.append(pid)
        site = _ias(slug) if slug in PHOTO_SLUGS else None
        if site and todo and (limit is None or limit > 0):
            try:
                h = site.placeholder()
                if h:
                    _mark_placeholder(cache, h)
            except _Down:
                site = None     # host unreachable from here: leave everything uncached
                _stat(slug, "unreachable")
        if site:
            new = fails = 0
            for pid in todo:
                if limit is not None and new >= limit:
                    break
                if fails >= MAX_FAILS:
                    _stat(slug, "gave_up")
                    break
                new += 1
                key = f"{slug}|{pid}"
                try:
                    cands = site.lookup(pid)
                except _Down:
                    fails += 1
                    _stat(slug, "failed")
                    site.reset()
                    continue
                except Exception:
                    fails += 1
                    _stat(slug, "failed")
                    continue
                fails = 0
                _forget(cache, key)
                cache[key] = []
                try:
                    _store(site, slug, pid, key, cands, cache)
                except Exception:
                    cache.pop(key, None)
                    continue
                _stat(slug, "looked_up")
        for pid in asked:
            hit = cache.get(f"{slug}|{pid}")
            if isinstance(hit, list) and hit:
                good = [dict(x) for x in hit if _exists(x)]
                if good:
                    out[pid] = good
    except Exception:
        pass
    return out


# ---------------------------------------------------------------- details()

DEK_APPR = "https://propertyappraisal.dekalbcountyga.gov"
COBB_YEARBUILT = "https://gis.cobbcounty.gov/gisserver/rest/services/tax/taxassessorsdaily/MapServer/5/query"
GWINNETT_IMPR = "https://services3.arcgis.com/RfpmnkSAQleRbndX/arcgis/rest/services/Property_and_Tax/FeatureServer/8/query"


def _cells(html, label):
    """Every value shown next to an exact side heading in an iasWorld datalet."""
    pat = r"class=\"DataletSideHeading\">\s*" + re.escape(label) + r"\s*</td>\s*<td[^>]*>([^<]*)</td>"
    return [clean(unescape(x)) for x in re.findall(pat, html)]


def _dekalb_details(site, pid, residential_first=True):
    """Year built and living (or gross building) area from the Property Appraisal datalets. {} for land only."""
    site._session()
    modes = [("residential", "Living Area"), ("com_summary", "Gross Area")]
    if not residential_first:
        modes.reverse()
    for mode, area in modes:
        url = f"{DEK_APPR}/Datalets/Datalet.aspx?mode={mode}&UseSearch=no&pin={urllib.parse.quote(pid)}"
        _, raw, _ = site._fetch(url)
        html = raw.decode("utf-8", "replace")
        m = re.search(r'id="hdXPin"[^>]*value="([^"]*)"', html)
        if not m or clean(m.group(1)) != clean(pid):
            raise _Down("datalet is not for this parcel")
        years = [y for y in (_year(x) for x in _cells(html, "Year Built")) if y]
        sizes = [s for s in (_num(x) for x in _cells(html, area)) if s]
        d = {}
        if years:
            d["yearBuilt"] = min(years)
        if sizes:
            d["sqft"] = sum(sizes)
        if d:
            return d
    return {}


def _arc_rows(url, field, ids, fields):
    """Attribute rows of an ArcGIS table for a list of ids, 60 ids per request. Raises on a service error."""
    rows = []
    for i in range(0, len(ids), 60):
        where = "%s IN (%s)" % (field, ",".join("'%s'" % x.replace("'", "''") for x in ids[i:i + 60]))
        d = get_json(url + "?" + urllib.parse.urlencode({"where": where, "outFields": fields, "returnGeometry": "false", "f": "json"}))
        if "error" in d:
            raise _Down(str(d["error"]))
        rows += [f.get("attributes") or {} for f in d.get("features", [])]
        time.sleep(PAUSE)
    return rows


def _cobb_details(pids):
    """{parcel: details} from the assessor's YearBuilt table (latest tax year; cards added up)."""
    pin = {re.sub(r"[^0-9A-Za-z]", "", p): p for p in pids}
    by = {}
    for r in _arc_rows(COBB_YEARBUILT, "PIN", sorted(pin), "PIN,TAXYR,CARD,YRBLT,SQFT"):
        by.setdefault(clean(r.get("PIN")), []).append(r)
    out = {}
    for k, p in pin.items():
        rows = by.get(k, [])
        top = max([r.get("TAXYR") or 0 for r in rows], default=0)
        rows = [r for r in rows if (r.get("TAXYR") or 0) == top]
        years = [y for y in (_year(r.get("YRBLT")) for r in rows) if y]
        sizes = [s for s in (_num(r.get("SQFT")) for r in rows) if s]
        d = {}
        if years:
            d["yearBuilt"] = min(years)
        if sizes:
            d["sqft"] = sum(sizes)
        out[p] = d
    return out


def _gwinnett_details(pids):
    """{parcel: details} from the Property Improvements table (dwellings and commercial buildings)."""
    by = {}
    for r in _arc_rows(GWINNETT_IMPR, "PIN", sorted(pids), "PIN,IMPRTYPE,YRBUILT,FINSIZE,IMPSTAT"):
        by.setdefault(clean(r.get("PIN")), []).append(r)
    out = {}
    for p in pids:
        rows = [r for r in by.get(clean(p), []) if clean(r.get("IMPRTYPE")) in ("DWELLING", "COMMERCIAL")]
        active = [r for r in rows if clean(r.get("IMPSTAT")) == "A"]
        rows = active or rows
        years = [y for y in (_year(r.get("YRBUILT")) for r in rows) if y]
        sizes = [s for s in (_num(r.get("FINSIZE")) for r in rows) if s]
        d = {}
        if years:
            d["yearBuilt"] = min(years)
        if sizes:
            d["sqft"] = sum(sizes)
        out[p] = d
    return out


def details(slug, parcels, cache, limit=None):
    """Year built and living area per parcel where an assessor source gives them. Parcels without a
    building are cached as {} and left out of the result."""
    out = {}
    try:
        todo, asked, kind = [], [], {}
        for p in parcels or []:
            pid = _pid(p)
            if not pid or pid in asked:
                continue
            asked.append(pid)
            kind[pid] = p.get("ptype")
            if not isinstance(cache.get(f"d|{slug}|{pid}"), dict):
                todo.append(pid)
        if limit is not None:
            todo = todo[:max(0, limit)]
        if todo and slug == "dekalb" and ACCEPT_DISCLAIMER.get("dekalb"):
            site = _Ias("dekalb", DEK_APPR, "", "", accept=True)
            fails = 0
            for pid in todo:
                if fails >= MAX_FAILS:
                    break
                try:
                    cache[f"d|{slug}|{pid}"] = _dekalb_details(site, pid, kind.get(pid) not in ("Commercial", "Industrial"))
                    fails = 0
                    _stat(slug, "details_looked_up")
                except Exception:
                    fails += 1
                    site.reset()
        elif todo and slug in ("cobb", "gwinnett"):
            try:
                got = _cobb_details(todo) if slug == "cobb" else _gwinnett_details(todo)
                for pid, d in got.items():
                    cache[f"d|{slug}|{pid}"] = d
                    _stat(slug, "details_looked_up")
            except Exception:
                pass
        for pid in asked:
            d = cache.get(f"d|{slug}|{pid}")
            if isinstance(d, dict) and d:
                out[pid] = dict(d)
    except Exception:
        pass
    return out

