"""Winning-bid lookups for metro counties that do not publish tax sale results.

Cobb    - recorded tax deeds in the assessor's ParcelSales table (ArcGIS REST, INSTRTYP = 'TX').
Clayton - tax payment history: the amount the buyer paid toward the parcel on the auction date is the winning bid.
DeKalb  - recorded tax deeds in the Tax Commissioner's property search (needs a disclaimer click-through,
          so it stays off until ACCEPT_DEKALB_DISCLAIMER is set to True).

Standard library plus ga_common only. The enrichers change the auction lists in place and return how many
parcels they priced; they never raise on a single failed lookup.
"""
import http.cookiejar
import re
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from html import unescape
from html.parser import HTMLParser

from ga_common import UA, clean, get, get_json, iso, money, parcel_rec

ACCEPT_DEKALB_DISCLAIMER = True  # approved by the site owner on 2026-10-01

COBB_SVC = "https://gis.cobbcounty.gov/gisserver/rest/services/tax/taxassessorsdaily/MapServer"
COBB_SALES = COBB_SVC + "/9"
COBB_PARCELS = COBB_SVC + "/0"
COBB_SRC = "Cobb County deed records (tax deed)"
CLAY_URL = "https://publicaccess.claytoncountyga.gov/Datalets/Datalet.aspx?mode=county_payments&UseSearch=no&pin="
CLAY_SRC = "Clayton County payment records (tax sale payment on the auction date)"
DEK_BASE = "https://publicaccess.dekalbtaxga.gov"
DEK_SRC = "DeKalb County deed records (tax deed)"
DEK_SRC_PAY = "DeKalb County payment records (tax sale payment plus excess funds)"
PAUSE = 0.3


# ---------------------------------------------------------------- shared helpers

def _today():
    return date.today().isoformat()


def _d(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


def _near(a, b, days):
    try:
        return abs((_d(a) - _d(b)).days) <= days
    except (TypeError, ValueError):
        return False


def _past(auctions):
    """(auction, parcel) pairs for auctions already held whose parcel has no winning bid yet."""
    t = _today()
    for a in auctions:
        if not a.get("date") or a["date"] >= t:
            continue
        for p in a.get("parcels", []):
            if p.get("winningBid") is None:
                yield a, p


class _Tables(HTMLParser):
    """Collect rows per <table id=...>; <br> becomes a newline inside a cell."""

    def __init__(self):
        super().__init__()
        self.tables, self.stack, self.row, self.cell = {}, [], None, None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.stack.append(dict(attrs).get("id") or "")
        elif tag == "tr":
            self.row = []
        elif tag in ("td", "th"):
            self.cell = ""
        elif tag == "br" and self.cell is not None:
            self.cell += "\n"

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None and self.row is not None:
            self.row.append("\n".join(x for x in (clean(l) for l in self.cell.split("\n")) if x))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if self.row and self.stack:
                self.tables.setdefault(self.stack[-1], []).append(self.row)
            self.row = None
        elif tag == "table" and self.stack:
            self.stack.pop()

    def handle_data(self, d):
        if self.cell is not None:
            self.cell += d


def _tables(html):
    p = _Tables()
    try:
        p.feed(html)
    except Exception:
        pass
    return p.tables


def _tender(methods):
    """'CHECK/100,000.00\\nCHECK(123)/50,000.00' -> 150000.0"""
    return round(sum(float(x.replace(",", "")) for x in re.findall(r"/\s*(-?[\d,]+\.\d{2})", methods or "")), 2)


_NOISE = {"LLC", "INC", "LTD", "CO", "CORP", "THE", "AND", "OF", "ESTATE", "ET", "AL", "TRUST", "JR", "SR", "II", "III"}


def _same_party(a, b):
    """True when two names obviously refer to the same person or company."""
    ta = {w for w in re.findall(r"[A-Z0-9]+", (a or "").upper()) if len(w) > 1 and w not in _NOISE}
    tb = {w for w in re.findall(r"[A-Z0-9]+", (b or "").upper()) if len(w) > 1 and w not in _NOISE}
    if not ta or not tb:
        return False
    both = ta & tb
    return len(both) >= 2 or both == ta or both == tb


# ---------------------------------------------------------------- Cobb

def _cobb_pin(parcel):
    s = re.sub(r"[^0-9A-Za-z]", "", str(parcel or ""))
    return s if len(s) == 11 else None


def _cobb_fmt(pin):
    return f"{pin[:2]}-{pin[2:6]}-{pin[6]}-{pin[7:10]}-{pin[10]}" if pin and len(pin) == 11 else pin


def _arc(layer, where, fields, order=None):
    """Query an ArcGIS layer or table, following pagination. Returns attribute dicts."""
    out, off = [], 0
    while True:
        q = {"where": where, "outFields": fields, "returnGeometry": "false", "f": "json", "resultOffset": off, "resultRecordCount": 1000}
        if order:
            q["orderByFields"] = order
        d = get_json(layer + "/query?" + urllib.parse.urlencode(q))
        if "error" in d:
            raise RuntimeError(str(d["error"]))
        rows = [f["attributes"] for f in d.get("features", [])]
        out += rows
        if not rows or not d.get("exceededTransferLimit"):
            return out
        off += len(rows)


def _ms_date(ms):
    return (datetime(1970, 1, 1) + timedelta(milliseconds=ms)).strftime("%Y-%m-%d") if isinstance(ms, (int, float)) else None


def enrich_cobb(auctions):
    """Fill winningBid from recorded tax deeds (INSTRTYP='TX', sale date within 3 days of the auction, PRICE > 0)."""
    want = {}
    for a, p in _past(auctions):
        pin = _cobb_pin(p.get("parcel"))
        if pin:
            want.setdefault(pin, []).append((a, p))
    pins, n = sorted(want), 0
    for i in range(0, len(pins), 80):
        chunk = pins[i:i + 80]
        try:
            rows = _arc(COBB_SALES, "INSTRTYP='TX' AND PRICE > 0 AND PIN IN (%s)" % ",".join(f"'{x}'" for x in chunk), "PIN,SALEDT,PRICE")
        except Exception:
            continue
        for r in rows:
            sd = _ms_date(r.get("SALEDT"))
            for a, p in want.get(r.get("PIN"), []):
                if p.get("winningBid") is None and sd and _near(sd, a["date"], 3) and (r.get("PRICE") or 0) > 0:
                    p["winningBid"] = round(float(r["PRICE"]), 2)
                    p["status"] = "Sold"
                    p["priceSource"] = COBB_SRC
                    n += 1
    return n


def _sale_day(ds):
    """First Tuesday of the month, or the Wednesday after it when that Tuesday is Jan 1 or Jul 4."""
    d = _d(ds)
    if d.weekday() == 1 and d.day <= 7:
        return True
    prev = d - timedelta(days=1)
    return d.weekday() == 2 and prev.day <= 7 and (prev.month, prev.day) in ((1, 1), (7, 4))


def cobb_tax_deeds(since="2021-01-01"):
    """Every recorded Cobb tax deed since `since`, grouped into one 'results' auction per sale date."""
    rows = _arc(COBB_SALES, f"INSTRTYP='TX' AND SALEDT >= DATE '{since}'", "PIN,SALEDT,PRICE", "SALEDT DESC, PIN")
    sales = {}
    for r in rows:
        sd, pin = _ms_date(r.get("SALEDT")), clean(r.get("PIN"))
        if not sd or not pin or sd >= _today() or not _sale_day(sd):
            continue
        price = float(r.get("PRICE") or 0)
        sales[(sd, pin)] = max(price, sales.get((sd, pin), 0))  # the table sometimes repeats a deed
    info, pins = {}, sorted({pin for _, pin in sales})
    for i in range(0, len(pins), 80):
        try:
            for r in _arc(COBB_PARCELS, "PIN IN (%s)" % ",".join(f"'{x}'" for x in pins[i:i + 80]), "PIN,SITUS_ADDR,OWNER_NAM1,FMV_TOTAL"):
                info.setdefault(r.get("PIN"), r)
        except Exception:
            pass
    by = {}
    for (sd, pin), price in sorted(sales.items()):
        g = info.get(pin, {})
        val = g.get("FMV_TOTAL")
        extra = {}
        if price > 0:
            extra["priceSource"] = COBB_SRC
        if clean(g.get("OWNER_NAM1")):
            extra["currentOwner"] = clean(g.get("OWNER_NAM1"))  # owner today, usually not the owner at the sale
        by.setdefault(sd, []).append(parcel_rec(_cobb_fmt(pin), None, g.get("SITUS_ADDR"), winning=price if price > 0 else None,
                                                status="Sold", value=float(val) if val else None, extra=extra))
    note = "From tax deeds recorded with the county (assessor sales table). Covers every parcel deeded at the sale; a blank price means the county has not entered it."
    return [{"date": d, "kind": "results", "source": COBB_SALES, "note": note, "parcels": ps} for d, ps in sorted(by.items(), reverse=True)]


# ---------------------------------------------------------------- Clayton

def _clayton_lookup(parcel, day):
    """Largest walk-in payment applied to the parcel on the auction date -> {'bid', 'buyer'}; None when there is none."""
    html = get(CLAY_URL + urllib.parse.quote(parcel), tries=2, timeout=30).decode("utf-8", "replace")
    if "PARID" not in html:
        raise RuntimeError("no datalet")
    t = _tables(html)
    payer = {}
    for r in t.get("Payer Details", [])[1:]:
        if len(r) >= 6:
            payer[r[1]] = r[5].split("\n")[0]
    groups = {}
    for r in t.get("Payment Information", [])[1:]:
        if len(r) < 9:
            continue
        eff = r[3].split("\n")
        if iso(eff[0]) != day or (eff[1] if len(eff) > 1 else "") != "W":
            continue
        g = groups.setdefault((payer.get(r[1], ""), r[7]), {"applied": 0.0, "tender": _tender(r[7])})
        g["applied"] += money(r[5]) or 0
    best = None
    for (name, _), g in groups.items():
        # One check often covers several parcels bought by the same bidder, so the amount applied to this
        # parcel is the bid; the tender only has to be large enough to cover it.
        if g["applied"] > 0 and g["tender"] >= g["applied"] - 1 and (best is None or g["applied"] > best["bid"]):
            best = {"bid": round(g["applied"], 2), "buyer": clean(name)}
    return best


def _clayton_ok(p, hit):
    if not hit or not hit.get("bid"):
        return False
    if _same_party(hit.get("buyer"), p.get("owner")):
        return False
    if p.get("minBid") is not None and hit["bid"] < p["minBid"] - 1:
        return False
    if p.get("excess") is not None and hit["bid"] - p["excess"] <= 0:
        return False
    return True


def enrich_clayton(auctions, cache, limit=None):
    """Fill winningBid/buyer from the payment made on the auction date. `limit` caps new requests (for testing)."""
    n = fetched = 0
    for a, p in _past(auctions):
        parcel = clean(p.get("parcel"))
        key = f"clayton|{parcel}|{a['date']}"
        if key not in cache:
            if limit is not None and fetched >= limit:
                continue
            fetched += 1
            try:
                cache[key] = _clayton_lookup(parcel, a["date"])
            except Exception:
                continue
            finally:
                time.sleep(PAUSE)
        hit = cache.get(key)
        if _clayton_ok(p, hit):
            p["winningBid"] = hit["bid"]
            if hit.get("buyer"):
                p["buyer"] = hit["buyer"]
            p["status"] = "Sold"
            p["priceSource"] = CLAY_SRC
            n += 1
    return n


# ---------------------------------------------------------------- DeKalb

class _DekalbSession:
    """iasWorld search session: disclaimer -> parcel search -> datalets addressed by the search index."""

    def __init__(self):
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.op.addheaders = [("User-Agent", UA)]
        self.search_url = DEK_BASE + "/search/commonsearch.aspx?mode=realprop"
        url, html = self._open(self.search_url)
        if "disclaimer" in url.lower():
            f = self._inputs(html)
            f["btAgree"] = ""
            self._open(url, f)

    def _open(self, url, data=None):
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        with self.op.open(url, body, timeout=40) as r:
            return r.geturl(), r.read().decode("utf-8", "replace")

    @staticmethod
    def _inputs(html):
        f = {}
        for tag in re.findall(r"<input[^>]*>", html):
            nm = re.search(r"name=[\"']([^\"']+)", tag)
            ty = re.search(r"type=[\"']([^\"']+)", tag)
            v = re.search(r"value=[\"']([^\"']*)", tag)
            if nm and (not ty or ty.group(1).lower() in ("hidden", "text")):
                f[nm.group(1)] = unescape(v.group(1)) if v else ""
        return f

    def find(self, parcel):
        """Search one parcel; returns the (sIndex, idx) query string for its datalets, or None."""
        _, html = self._open(self.search_url)
        f = self._inputs(html)
        yr = re.search(r"<option value=\"?(\d{4})\"? selected", html)
        f.update({"inpParid": parcel, "hdAction": "Search", "btSearch": "", "selSortBy": "PARID", "selSortDir": " asc",
                  "inpTaxyr": yr.group(1) if yr else "0"})
        url, res = self._open(self.search_url, f)
        m = re.search(r"Datalet\.aspx\?sIndex=(\d+)&(?:amp;)?idx=(\d+)", res)
        if m:
            return f"sIndex={m.group(1)}&idx={m.group(2)}"
        m = re.search(r"sIndex=(\d+)&idx=(\d+)", url)  # a single match can open the datalet directly
        return f"sIndex={m.group(1)}&idx={m.group(2)}" if m else None

    def datalet(self, mode, ref):
        return _tables(self._open(f"{DEK_BASE}/Datalets/Datalet.aspx?mode={mode}&{ref}")[1])


def _dekalb_lookup(sess, parcel, day, excess):
    """Tax deed recorded for the auction date -> {'bid', 'buyer', 'via'}; None when no tax deed is on record."""
    ref = sess.find(parcel)
    if not ref:
        raise RuntimeError("parcel not found")
    deed = None
    for r in sess.datalet("dek_sales", ref).get("Sales", [])[1:]:
        if len(r) < 6:
            continue
        if ("TAX DEED" in r[5].upper() or "TAX COMMISSIONER" in r[3].upper()) and _near(iso(r[0]), day, 3):
            deed = r
            break
    if not deed:
        return None
    price = money(deed[1]) or 0
    if price > 0:
        return {"bid": round(price, 2), "buyer": clean(deed[4]), "via": "deed"}
    # Price not entered yet: buyer's payment toward the taxes on the sale date plus the excess funds.
    time.sleep(PAUSE)
    tabs = sess.datalet("payment", ref)
    rows = next((v for k, v in tabs.items() if k.startswith("Payment")), [])
    tax, extra = 0.0, {}
    for r in rows[1:]:
        if len(r) < 6 or not _near(iso(r[2]), day, 3):
            continue
        if r[1] == "W":
            tax += money(r[5]) or 0  # amount applied to this parcel (one check can cover several parcels)
        elif r[0] == "MIS":
            extra[r[4]] = _tender(r[4])  # excess funds deposit
    over = sum(extra.values()) if extra else excess
    if tax > 0 and over:
        return {"bid": round(tax + over, 2), "buyer": clean(deed[4]), "via": "payment"}
    return {"bid": None, "buyer": clean(deed[4]), "via": "deed"}


def enrich_dekalb(auctions, cache, limit=None):
    """Fill winningBid/buyer from recorded tax deeds. Does nothing until ACCEPT_DEKALB_DISCLAIMER is True."""
    if not ACCEPT_DEKALB_DISCLAIMER:
        return 0
    n = fetched = 0
    sess = None
    for a, p in _past(auctions):
        parcel = clean(p.get("parcel"))
        key = f"dekalb|{parcel}|{a['date']}"
        if key not in cache:
            if limit is not None and fetched >= limit:
                continue
            fetched += 1
            try:
                sess = sess or _DekalbSession()
                cache[key] = _dekalb_lookup(sess, parcel, a["date"], p.get("excess"))
            except Exception:
                sess = None  # start a fresh session for the next parcel
                continue
            finally:
                time.sleep(PAUSE)
        hit = cache.get(key)
        if not hit:
            continue
        if hit.get("buyer"):
            p["buyer"] = hit["buyer"]
        p["status"] = "Sold"
        if hit.get("bid"):
            p["winningBid"] = hit["bid"]
            p["priceSource"] = DEK_SRC if hit.get("via") != "payment" else DEK_SRC_PAY
            n += 1
    return n
