"""Winning-bid sources for the outer-ring counties (Fayette, Carroll, Meriwether, Walton, Troup).

Every public function returns a list of auctions
    [{date 'YYYY-MM-DD', kind 'results'|'excess'|'list', source, note, parcels: [ga_common.parcel_rec(...)]}]
and never raises because of one bad file: a file that fails to download or parse is skipped.

Run this file directly to print a self-test (counts, samples and the overlap with data/<county>.json).
"""
import html
import io
import re
import urllib.parse
import zipfile
from datetime import datetime

from ga_common import (get, pdf_text, doc_text, xls_rows, xlsx_rows, money, iso, clean, parcel_rec, group_by_date,
                       wp_media)

S3_HOST = "https://images-governmentwindow.s3.amazonaws.com/"
EXCESS_NOTE = "Only sales that produced excess funds are listed, so this is a partial view of what sold."
DERIVED = "Derived: amount owed + excess funds"
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MON = {m: i for i, m in enumerate(MONTHS, 1)}
MONEY3 = re.compile(r"\$\s*([\d,]+\.\d{2})\s+\$\s*([\d,]+\.\d{2})\s+\$\s*([\d,]+\.\d{2}|-)")


# ----------------------------------------------------------------------------------------------------- helpers
def _s3_url(key):
    return S3_HOST + urllib.parse.quote(key)


def _s3_list(slug):
    """[(key, last_modified)] for one Government Window site, newest first."""
    out, marker = [], ""
    for _ in range(10):
        try:
            x = get(f"{S3_HOST}?prefix=resources/sites/{slug}/docs/&marker={urllib.parse.quote(marker)}").decode("utf-8", "replace")
        except Exception:
            break
        ks = [(html.unescape(k), m) for k, m in re.findall(r"<Key>([^<]*)</Key><LastModified>([^<]*)</LastModified>", x)]
        out += ks
        if "<IsTruncated>true</IsTruncated>" not in x or not ks:
            break
        marker = ks[-1][0]
    return sorted(out, key=lambda km: km[1], reverse=True)


def _first_tuesday(year, month):
    dt = datetime(year, month, 1)
    return dt.replace(day=1 + (1 - dt.weekday()) % 7).strftime("%Y-%m-%d")


def _text(url):
    """Download a .pdf/.doc/.docx and return its text ('' on failure)."""
    try:
        data = get(url)
        ext = urllib.parse.unquote(url).rsplit(".", 1)[-1].lower()
        return pdf_text(data) if ext == "pdf" else doc_text(data, ext)
    except Exception:
        return ""


def _norm(pid):
    """Join key for a parcel ID: letters and digits only."""
    if isinstance(pid, float) and pid == int(pid):
        pid = str(int(pid))
    return re.sub(r"[^A-Z0-9]", "", str(pid).upper())


def _snap(x):
    """A derived price a few dollars off a round bid (the list predates the sale, so fees moved) -> that round bid."""
    near = round(x / 25.0) * 25.0
    return near if abs(near - x) <= 6 else round(x, 2)


def _auctions(by_date, kind, note):
    """by_date: {date: {"source": url, "parcels": [..]}} -> auction list, newest first."""
    return [{"date": d, "kind": kind, "source": v["source"], "note": v.get("note", note), "parcels": v["parcels"]}
            for d, v in sorted(by_date.items(), reverse=True) if v["parcels"]]


# ----------------------------------------------------------------------------------------------------- Fayette
FAYETTE_DOCS = [
    ("2025-12-02", "resources/sites/fayettecountyga/docs/2025 Tax Sale List of Sold Properties.doc"),
    ("2024-10-01", "resources/sites/fayettecountyga/docs/Fayette County Tax Sale List 2024.doc"),
    ("2023-10-03", "resources/sites/fayettecountyga/docs/2023 Tax Sale Sold List.doc"),
]
FAYETTE_NOTE = "County published only the parcels that sold, not the unsold ones."


def _fayette_parse(t):
    ps = []
    for blk in re.split(r"Map\s*&\s*Parcel:\s*", t)[1:]:
        pm = re.match(r"([0-9A-Z]{6,12})\b", blk)
        if not pm:
            continue
        sold = re.search(r"SOLD\s+FOR:?\s*\$\s*([\d,]+(?:\.\d{2})?)", blk, re.I)
        due = re.search(r"Amount Due:\s*\$\s*([\d,]+\.\d{2})", blk, re.I)
        buyer = re.search(r"(?:SOLD TO|PURCHASER):[ \t]*([^\n]+)", blk, re.I)
        own = re.search(r"Defendant in Fi\W{0,2}Fa:\s*(.*?)\s*Current Record Holder:", blk, re.I | re.S)
        yrs = re.search(r"Tax Years Due:\s*([\d, ]+)", blk, re.I)
        ps.append(parcel_rec(pm.group(1), clean(own.group(1))[:100] if own else None, None,
                             money(due.group(1)) if due else None, money(sold.group(1)) if sold else None,
                             clean(buyer.group(1)).strip(" ,") if buyer else None,
                             status="Sold" if sold else "Listed", years=clean(yrs.group(1)).strip(" ,") if yrs else None))
    return ps


def fayette_results():
    out = {}
    docs = list(FAYETTE_DOCS)
    known = {k for _, k in docs}
    try:  # a future "sold" list posted under a new name
        for key, mod in _s3_list("fayettecountyga"):
            if re.search(r"sold", key, re.I) and re.search(r"\.docx?$", key, re.I) and key not in known:
                docs.append((None, key))
    except Exception:
        pass
    for d, key in docs:
        try:
            src = _s3_url(key)
            t = _text(src)
            if not d:
                dm = re.search(r"same being\s+([A-Z][a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", clean(t))
                d = iso(f"{dm.group(1)} {dm.group(2)}, {dm.group(3)}") if dm else None
            ps = [p for p in _fayette_parse(t) if p.get("winningBid") is not None]
            if d and ps and d not in out:
                out[d] = {"source": src, "parcels": ps}
        except Exception:
            continue
    return _auctions(out, "results", FAYETTE_NOTE)


# ----------------------------------------------------------------------------------------------------- Carroll
CARROLL_SITE = "https://carrollcountygatax.com"
CARROLL_WP = CARROLL_SITE + "/wp-content/uploads/"
CARROLL_S3 = "resources/sites/carrollcountyga/docs/"
# (sale date, url) of every county-posted results document; S3 keys are static, WordPress ones are also re-discovered
CARROLL_RESULTS = [
    ("2021-02-02", _s3_url(CARROLL_S3 + "Carroll Feb 2021 Legals(2).doc")),
    ("2021-05-04", _s3_url(CARROLL_S3 + "Carroll May 2021 Legals(6).doc")),
    ("2022-03-01", _s3_url(CARROLL_S3 + "March_2022_DELINQUENT_PROPERTY_TAX_SALE.docx")),
    ("2022-06-07", _s3_url(CARROLL_S3 + "June_2022_Tax_Sale.XLS.docx")),
    ("2022-09-06", _s3_url(CARROLL_S3 + "Carroll_Sept_2022_Legals.doc")),
    ("2023-05-02", _s3_url(CARROLL_S3 + "tax_sale.doc")),
    ("2023-08-01", CARROLL_WP + "2023/08/Carroll-August-2023-Real-Property-Legals-2-2-1.doc"),
    ("2024-05-07", CARROLL_WP + "2024/05/Carroll-Legals-May-2024docx-6.docx"),
    ("2025-03-04", CARROLL_WP + "2025/03/Carroll-March-2025-Tax-Sale-Legal.pdf"),  # header says "May 4, 2025" (typo)
    ("2025-06-03", CARROLL_WP + "2025/06/June2025Legals.pdf"),
    ("2026-04-07", CARROLL_WP + "2026/04/Carroll-Apr-2026-Legals-2.pdf"),
]
CARROLL_MOBILE = [("2023-08-01", CARROLL_WP + "2023/08/August-2023-Mobile-Home-Legals.doc")]
# last pre-sale legal ad for sales with no results document (amount due only)
CARROLL_PRESALE = [
    ("2020-10-06", _s3_url(CARROLL_S3 + "Carroll Oct 2020 Legals. Updated 10_02_20.doc")),
    ("2021-05-04", _s3_url(CARROLL_S3 + "Carroll May 2021 Legals(5).doc")),
    ("2026-07-07", CARROLL_WP + "2026/07/Carroll-July-2026-Legals-3.pdf"),
]
ADDR = re.compile(r"(\d{1,6}\s+[A-Z][A-Z0-9 ]{2,40}?\s(?:RD|ROAD|DR|DRIVE|LN|LANE|ST|STREET|CT|COURT|CIR|CIRCLE|WAY|TRL|TRAIL|HWY|PKWY|"
                  r"AVE|AVENUE|TERR|PL|PLACE|BLVD|PT|POINT|RUN|LOOP|CK DRIVE))\b\.?\s*$")
_carroll_cache = {}
_CARROLL_PRICE = re.compile(r"(?:SOLD\s+FOR|BID\s+AMOUNT|\bBID)\s*:?\s*\$?\s*(\d[\d,]*(?:\.\d{1,2})?)|\$\s*(\d[\d,]*(?:\.\d{1,2})?)", re.I)
_CARROLL_MARK = re.compile(r"(?:SOLD\s+FOR|BID\s+AMOUNT|\bBID)\s*:?\s*\$?\s*\d[\d,]*(?:\.\d{1,2})?|\$\s*\d[\d,]*(?:\.\d{1,2})?|\bNO\s*BID\b|\bNO\s*SALE\b|OWNER PAID", re.I)


def _carroll_blocks(t, mobile=False):
    """Parse one Carroll legal ad / results text into parcel records."""
    ps = []
    marker = r"ACCOUNT\s*#\s*:\s*" if mobile else r"MAP AND PARCEL\s*:?\s*"
    for b in re.split(marker, t)[1:]:
        m = re.match(r"([A-Z0-9][A-Z0-9-]*)(?: (\d{3,7})(?=\s))?", b)
        if not m or not re.search(r"\d", m.group(1)):
            continue
        parcel = m.group(1)
        if m.group(2) and len(parcel) < 6:  # "F03 0281"
            parcel += m.group(2)
        if parcel.isdigit() and len(parcel) == 6:  # county dropped the leading zero ("100055")
            parcel = "0" + parcel
        b1 = clean(b[m.end():])
        b1 = re.split(r"\s_{5,}|\sSOLD FOR\s*$|\sSOLD\s*$", b1)[0]
        pre = re.split(r"AMOUNT DUE", b1)[0]  # the result is written before the amount due
        won, st = None, "Listed"
        pm = _CARROLL_PRICE.search(pre)
        if re.search(r"\bNO\s*BID\b|\bNO\s*SALE\b", pre, re.I):
            st = "No bid"
        elif re.search(r"\bPAID\b|REDEEMED", pre, re.I):
            st = "Paid"
        elif pm:
            won, st = money(pm.group(1) or pm.group(2)), "Sold"
        own = re.search(r"CURRENT RECORD HOLDER(?:\(S\))?:\s*(.*?)\s*(?:DEFENDANT IN FI|AMOUNT DUE|TAX\s*YEARS? DUE|DEED BOOK|MOBILE HOME DESC|$)", b1)
        owner = clean(_CARROLL_MARK.sub(" ", own.group(1)))[:120] if own else None
        due = re.search(r"AMOUNT DUE:\s*\$?\s*([\d,]+\.\d{2})", b1)
        yrs = re.search(r"TAX\s*YEARS? DUE:\s*(\d{4}(?:\s*[-,&]\s*\d{4})*)", b1)
        addr = ADDR.search(b1) if not mobile else None
        extra = None
        if mobile:
            desc = re.search(r"MOBILE HOME DESCRIPTION:\s*(.*)$", b1)
            extra = {"type": "Mobile home"}
            if desc:
                extra["desc"] = clean(re.sub(r"[^\x20-\x7e]", " ", desc.group(1)))[:80]
        ps.append(parcel_rec(parcel, owner, addr.group(1) if addr else None,
                             money(due.group(1)) if due else None, won, status=st,
                             years=clean(yrs.group(1)) if yrs else None, extra=extra))
    return ps


def _carroll_doc(url, mobile=False):
    if url not in _carroll_cache:
        t = _text(url)
        try:
            _carroll_cache[url] = (_carroll_blocks(t, mobile), t)
        except Exception:
            _carroll_cache[url] = ([], t)
    return _carroll_cache[url]


def _carroll_sale_date(t):
    t = clean(t)[:1500]
    for pat in (r"RESULTS? (?:OF|FOR|FROM) THE\s+([A-Z][A-Za-z]+ \d{1,2},? \d{4})", r"same being\s+([A-Z][A-Za-z]+ \d{1,2},? \d{4})",
                r"^\s*([A-Z][A-Za-z]+ \d{1,2},? \d{4})"):
        m = re.search(pat, t, re.I)
        if m:
            d = iso(re.sub(r"(\d),?\s+(\d{4})", r"\1, \2", m.group(1).title()))
            if d:
                return d
    return None


def _carroll_discover():
    """Results documents posted on WordPress after the newest one in CARROLL_RESULTS."""
    known = {u for _, u in CARROLL_RESULTS + CARROLL_MOBILE + CARROLL_PRESALE}
    newest = max(d for d, _ in CARROLL_RESULTS)
    found = []
    try:
        media = wp_media(CARROLL_SITE, "legals")
    except Exception:
        return found
    for it in sorted(media, key=lambda x: x.get("date", ""), reverse=True):
        u = it.get("source_url", "")
        if u in known or not re.search(r"\.(pdf|docx?)$", u, re.I) or it.get("date", "")[:10] <= newest:
            continue
        mobile = "mobile" in u.lower()
        ps, t = _carroll_doc(u, mobile)
        if not ps or not re.search(r"RESULT", t[:1500], re.I) or not any(p["status"] != "Listed" for p in ps):
            continue
        d = _carroll_sale_date(t)
        if d and d > newest and d <= it["date"][:10]:
            found.append((d, u, mobile))
    return found


def carroll_results():
    out = {}
    docs = [(d, u, False) for d, u in CARROLL_RESULTS] + [(d, u, True) for d, u in CARROLL_MOBILE] + _carroll_discover()
    for d, u, mobile in docs:
        try:
            ps, _ = _carroll_doc(u, mobile)
            ps = [dict(p) for p in ps]
            if not ps:
                continue
            a = out.setdefault(d, {"source": u, "parcels": [], "note": ""})
            have = {p["parcel"] for p in a["parcels"]}
            a["parcels"] += [p for p in ps if p["parcel"] not in have]
            if mobile and a["source"] != u:
                a["note"] = "Includes the mobile home sale results posted the same day: " + u
        except Exception:
            continue
    return _auctions(out, "results", "")


def _carroll_excess_url():
    src = CARROLL_WP + "2026/09/EXCESS_FUNDS_LIST-1.xls"
    try:
        xs = [it for it in wp_media(CARROLL_SITE, "EXCESS_FUNDS_LIST") if it.get("source_url", "").lower().endswith(".xls")]
        if xs:
            src = max(xs, key=lambda x: x["date"])["source_url"]
    except Exception:
        pass
    return src


def carroll_excess_prices():
    # amount due and actual price per (date, normalized parcel) from the results and pre-sale documents
    known = {}
    for d, u in CARROLL_PRESALE + CARROLL_RESULTS:  # results last so they win
        try:
            for p in _carroll_doc(u)[0]:
                k = known.setdefault((d, _norm(p["parcel"]).lstrip("0")), {"parcel": p["parcel"]})
                for f in ("minBid", "winningBid", "owner", "address"):
                    if p.get(f) is not None:
                        k[f] = p[f]
        except Exception:
            continue
    src = _carroll_excess_url()
    try:
        rows = xls_rows(get(src))
    except Exception:
        return []
    recs, d = [], None
    for r in rows:
        r = list(r) + [""] * 6
        dates = [c for c in r[:6] if isinstance(c, datetime)]
        ex = r[5] if isinstance(r[5], (int, float)) and not isinstance(r[5], bool) else None
        if dates and ex is None:
            d = dates[0].strftime("%Y-%m-%d")
            continue
        if not d or ex is None or not str(r[0]).strip():
            continue
        raw = str(int(r[0])).zfill(7) if isinstance(r[0], float) and r[0] == int(r[0]) else clean(r[0])
        k = known.get((d, _norm(raw).lstrip("0"))) or {}
        due, actual = k.get("minBid"), k.get("winningBid")
        win, extra = None, None
        if actual is not None:
            win = actual  # the county's own results document beats the arithmetic
        elif due is not None:
            win, extra = _snap(due + float(ex)), {"priceSource": DERIVED}
        recs.append((d, parcel_rec(k.get("parcel", raw), r[1], r[3] or k.get("address"), due, win, excess=float(ex), status="Sold", extra=extra)))
    return group_by_date(recs, "excess", src, EXCESS_NOTE)


# ----------------------------------------------------------------------------------------------------- Meriwether
def _docx_rows(data):
    """Table rows of a .docx as lists of cell text (standard library only)."""
    xml = zipfile.ZipFile(io.BytesIO(data)).read("word/document.xml").decode("utf-8", "replace")
    rows = []
    for tr in re.findall(r"<w:tr[ >].*?</w:tr>", xml, re.S):
        cells = []
        for tc in re.findall(r"<w:tc[ >].*?</w:tc>", tr, re.S):
            paras = ["".join(re.findall(r"<w:t(?: [^>]*)?>([^<]*)</w:t>", p)) for p in re.findall(r"<w:p[ >].*?</w:p>", tc, re.S)]
            cells.append(clean(html.unescape(" ".join(paras))))
        rows.append(cells)
    return rows


def _meri_date(tok, year_hint):
    """'May-23' -> first Tuesday of May 2023; '7-Jul' -> that day in year_hint; full dates as written."""
    if isinstance(tok, datetime):
        ft = _first_tuesday(tok.year, tok.month)
        return tok.strftime("%Y-%m-%d") if 0 <= tok.day - int(ft[8:]) <= 1 else ft
    s = clean(tok)
    m = re.match(r"^([A-Z][a-z]{2})[a-z]*-(\d{2})$", s)
    if m and m.group(1) in MON:
        return _first_tuesday(2000 + int(m.group(2)), MON[m.group(1)])
    m = re.match(r"^(\d{1,2})-([A-Z][a-z]{2})[a-z]*$", s)
    if m and m.group(2) in MON and year_hint:
        try:
            return datetime(year_hint, MON[m.group(2)], int(m.group(1))).strftime("%Y-%m-%d")
        except ValueError:
            return None
    return iso(s)


def _meri_year(name, mod):
    m = re.search(r"(\d{1,2})-(\d{1,2})-(\d{2})(?!\d)", name)
    return 2000 + int(m.group(3)) if m else int(mod[:4])


def _meri_rec(pid, owner, buyer, due, price, ex):
    due, price = money(due), money(price)
    ex = money(ex) if ex is not None and re.search(r"\d", str(ex)) else 0.0
    if due is None or price is None:
        return None
    return parcel_rec(re.sub(r"\s+", " ", str(pid)).strip(), owner, None, due, price, buyer, ex, "Sold")


def _meri_table(rows, year):
    out = []
    for c in rows:
        c = [x for x in c]
        if len(c) < 7 or not c[0] or re.search(r"PID|OWNER", str(c[0]) + str(c[1])):
            continue
        d = _meri_date(c[2], year)
        r = _meri_rec(c[0], c[1], c[3], c[-3], c[-2], c[-1]) if d else None
        if r:
            out.append((d, r))
    return out


DATE_TOK = re.compile(r"(?<![\w/-])((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*-\d{2}|\d{1,2}-(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*|\d{1,2}/\d{1,2}/\d{2,4})(?![\w/-])")
PID_CONT = re.compile(r"^\s{0,4}([A-Z]?\d{2,3}[A-Z]?)(?=\s{2,}|\s*$)")
PID_LINE = re.compile(r"^\s{0,4}([A-Z0-9/]{2,12}(?: [A-Z0-9]{1,4}){1,3})(?=\s{2,}|\s*$)")


def _meri_pdf(t, year):
    """Rows wrap across lines; a record ends at the line carrying the three dollar amounts."""
    out, buf = [], []
    for line in t.splitlines():
        if re.search(r"PID\s+OWNER|DATE OF|TAX AMOUNT|UPDATED \d", line) or not line.strip():
            if not MONEY3.search(line):
                continue
        buf.append(line)
        am = MONEY3.search(line)
        if not am:
            continue
        pid, tok, tokcol, cont = None, None, None, []
        for ln in buf:
            pm = PID_LINE.match(ln)
            cm = PID_CONT.match(ln)
            if pm and re.search(r"\d", pm.group(1)):
                pid = pm.group(1)
            elif cm and pid and len(pid.split()) < 3:  # third part of the PID wrapped onto its own line
                pid, cont = pid + " " + cm.group(1), cont + [cm.group(1)]
            dm = DATE_TOK.search(ln)
            if dm:
                tok, tokcol = dm.group(1), dm.start(1)
        owner, buyer = [], []
        if pid and tok:
            for ln in buf:
                body = ln[:MONEY3.search(ln).start()] if MONEY3.search(ln) else ln
                pm = PID_LINE.match(body)
                start = pm.end() if pm and pm.group(1) == pid else 0
                for sm in re.finditer(r"\S+(?: \S+)*", body[start:]):
                    seg, col = sm.group(0), start + sm.start()
                    if DATE_TOK.fullmatch(seg):
                        continue
                    seg = DATE_TOK.sub("", seg).strip()
                    if seg and not (col < 5 and seg in cont):
                        (owner if col < tokcol - 2 else buyer).append(seg)
            d = _meri_date(tok, year)
            r = _meri_rec(pid, " ".join(owner), " ".join(buyer), am.group(1), am.group(2), am.group(3)) if d else None
            if r:
                out.append((d, r))
        buf = []
    return out


def meriwether_excess():
    files = [(k, m) for k, m in _s3_list("meriwethercountyga") if re.search(r"excess", k, re.I) and re.search(r"\.(pdf|docx|xlsx)$", k, re.I)]
    # tables (docx/xlsx) are cleaner than PDF text, so they get first say; within a type, newest first
    files.sort(key=lambda km: (0 if not km[0].lower().endswith(".pdf") else 1, [-ord(c) for c in km[1]]))
    by, seen, amounts = {}, set(), {}
    for key, mod in files:
        src = _s3_url(key)
        try:
            data = get(src)
            year = _meri_year(key.rsplit("/", 1)[1], mod)
            low = key.lower()
            if low.endswith(".docx"):
                recs = _meri_table(_docx_rows(data), year)
            elif low.endswith(".xlsx"):
                recs = _meri_table(xlsx_rows(data), year)
            else:
                recs = _meri_pdf(pdf_text(data), year)
        except Exception:
            continue
        for d, r in recs:
            k = (d, _norm(r["parcel"]))
            if k in seen:
                continue
            # same sale and same three amounts under a shortened PID = the same row read from a wrapped PDF line
            same = amounts.setdefault((d, r["minBid"], r["winningBid"], r.get("excess")), [])
            if any(x.startswith(k[1]) or k[1].startswith(x) for x in same):
                continue
            same.append(k[1])
            seen.add(k)
            a = by.setdefault(d, {"source": src, "mod": mod, "parcels": []})
            if mod > a["mod"]:
                a["source"], a["mod"] = src, mod
            a["parcels"].append(r)
    return _auctions(by, "excess", EXCESS_NOTE)


# ----------------------------------------------------------------------------------------------------- Walton
WALTON_ROW = re.compile(r"^\s*(.*?)\s{2,}([A-Z]{1,2}[0-9A-Z]{2,4}-\d{3}[A-Z]?)\s+\$\s*([\d,]+\.\d{2})\s+(\d{1,2}/\d{1,2}/\d{4})\s+\$\s*([\d,]+\.\d{2})\s+\$\s*([\d,]+\.\d{2})")


def _walton_parse(t):
    recs, cur = [], None
    for line in t.splitlines():
        m = WALTON_ROW.match(line)
        if m:
            name, parcel, sale, d, due, ex = m.groups()
            cur = parcel_rec(parcel, name, None, money(due), money(sale), excess=money(ex), status="Sold")
            recs.append((iso(d), cur))
            continue
        s = line.strip()
        if not cur or not s:
            continue
        if s.startswith("PURCHASER"):
            b = clean(s.split(":", 1)[1]) if ":" in s else ""
            b = re.split(r"\s+(?=\d{2,}\s+[A-Za-z])|(?<=LLC)(?=\d)|\s+P\.?\s?O\.?\s+Box", b)[0].strip(" ,")
            if b:
                cur["buyer"] = b
            cur = None
        elif "NAME" in s and "MAP/PARCEL" in s:
            cur = None
        elif "address" not in cur and len(line) - len(line.lstrip()) < 12:
            a = re.split(r"\s{2,}", s)[0]
            if not re.search(r"\$|\*", a):
                cur["address"] = clean(a)
    return recs


def walton_excess():
    files = [(k, m) for k, m in _s3_list("waltoncountyga")
             if re.search(r"excess[ _]*funds", k, re.I) and k.lower().endswith(".pdf") and "request" not in k.lower()]
    by, seen = {}, set()
    for key, mod in files:  # newest first, so the freshest copy of each row wins
        src = _s3_url(key)
        try:
            recs = _walton_parse(pdf_text(get(src)))
        except Exception:
            continue
        for d, r in recs:
            k = (d, _norm(r["parcel"]))
            if not d or k in seen:
                continue
            seen.add(k)
            by.setdefault(d, {"source": src, "parcels": []})["parcels"].append(r)
    return _auctions(by, "excess", EXCESS_NOTE)


# ----------------------------------------------------------------------------------------------------- Troup
TROUP_SITE = "https://troupcountytax.com"
TROUP_MAP = r"\d{4,5}[A-Z]?[- ]\d{3}[- ]\d{3}[A-Z]?|\d{4,5}[A-Z]?\d{6}[A-Z]?"
TROUP_EXCESS_ROW = re.compile(r"^\s*(\d{1,2}/\d{1,2}/\d{4})\s+(.*?)\s+(?:#\s*(\d+)\s+)?(" + TROUP_MAP + r")\s+(.*?)\s*\$\s*([\d,]+\.\d{2})\s*$")
TROUP_LIST_ROW = re.compile(r"^\s*(\d{1,6})\s+(.*?)\s{2,}(.*?)\s{2,}(\d{3,5}[A-Z]?\s+\d{3}\s+\d{3,4}[A-Z]?)\s+\$\s*([\d,]+\.\d{2})")
TROUP_EXTRA_LISTS = []  # sale lists that are no longer in the media library (none reachable today)


def _troup_key(mapno):
    return _norm(mapno).lstrip("0")


def _troup_fmt(mapno):
    """'0943D-025-006' / '0212C003009A' -> '0943D 025 006' (the format the sale lists use)."""
    n = _norm(mapno)
    m = re.match(r"^(\d{4,5}[A-Z]?)(\d{3})(\d{3}[A-Z]?)$", n)
    return " ".join(m.groups()) if m else clean(mapno)


def _troup_list(t):
    ps, cur = [], None
    for line in t.splitlines():
        m = TROUP_LIST_ROW.match(line)
        if m:
            cur = {"bill": m.group(1), "owner": clean(m.group(2)), "address": clean(m.group(3)),
                   "parcel": re.sub(r"\s+", " ", m.group(4)), "bid": money(m.group(5))}
            ps.append(cur)
        elif cur and line.strip():
            c = [x.strip() for x in re.split(r"\s{2,}", line.strip()) if x.strip()]
            if len(c) == 2:
                cur["address"] = f"{cur['address']}, {c[1]}"
            cur = None
    return ps


def troup_derived():
    try:
        media = [it for it in wp_media(TROUP_SITE) if it.get("source_url", "").lower().endswith(".pdf")]
    except Exception:
        media = []
    have = {it["source_url"] for it in media}
    media += [{"source_url": u, "date": "2025-03-01"} for u in TROUP_EXTRA_LISTS if u not in have]
    lists, snaps = {}, []  # lists: date -> [(posted, url, rows)] ; snaps: [(posted, url, rows)]
    for it in sorted(media, key=lambda x: x.get("date", ""), reverse=True):
        u = it["source_url"]
        if re.search(r"procedure|general|outage|holiday|form", u, re.I):
            continue
        try:
            t = pdf_text(get(u))
        except Exception:
            continue
        try:
            dm = re.search(r"DATE OF SALE\s+([A-Z]+\.? \d{1,2}, \d{4})", t, re.I)
            if dm and re.search(r"\bBID\b", t):
                mon, rest = dm.group(1).replace(".", "").split(" ", 1)
                d = iso(f"{MONTHS[MON[mon[:3].title()] - 1]} {rest}") if mon[:3].title() in MON else None
                rows = _troup_list(t)
                if d and rows:
                    lists.setdefault(d, []).append((it.get("date", ""), u, rows))
            elif re.search(r"excess funds|escrow", t[:600], re.I):
                rows = []
                for line in t.splitlines():
                    m = TROUP_EXCESS_ROW.match(line)
                    if m:
                        d, own, ppin, mapno, addr, amt = m.groups()
                        rows.append({"date": iso(d), "owner": clean(own), "ppin": ppin, "map": mapno, "address": clean(addr), "excess": money(amt)})
                if rows:
                    snaps.append((it.get("date", ""), u, rows))
        except Exception:
            continue
    # union of the snapshots: PPIN from any copy, amount from the oldest copy (before any partial payout)
    rows = {}
    for posted, u, rs in sorted(snaps, key=lambda s: s[0]):
        for r in rs:
            if not r["date"]:
                continue
            k = (r["date"], _troup_key(r["map"]))
            if k not in rows:
                rows[k] = dict(r, source=u, posted=posted)
            else:
                if r["ppin"] and not rows[k]["ppin"]:
                    rows[k]["ppin"] = r["ppin"]
                if posted >= rows[k]["posted"]:
                    rows[k]["source"], rows[k]["posted"] = u, posted
    by = {}
    for (d, key), r in rows.items():
        hit = None
        for posted, u, lrows in sorted(lists.get(d, []), key=lambda x: x[0], reverse=True):  # last list before the sale first
            hit = next((x for x in lrows if r["ppin"] and x["bill"] == r["ppin"]), None) \
                or next((x for x in lrows if _troup_key(x["parcel"]) == key), None)
            if hit:
                break
        if hit:
            pid = hit["parcel"] if _troup_key(hit["parcel"]) == key else _troup_fmt(r["map"])  # the list has a few typos
            rec = parcel_rec(pid, r["owner"], hit["address"] or r["address"], hit["bid"], _snap(hit["bid"] + r["excess"]),
                             excess=r["excess"], status="Sold", extra={"priceSource": DERIVED})
        else:
            rec = parcel_rec(_troup_fmt(r["map"]), r["owner"], r["address"], excess=r["excess"], status="Sold")
        a = by.setdefault(d, {"source": r["source"], "posted": r["posted"], "parcels": []})
        if r["posted"] > a["posted"]:
            a["source"], a["posted"] = r["source"], r["posted"]
        a["parcels"].append(rec)
    return _auctions(by, "excess", EXCESS_NOTE)


# ----------------------------------------------------------------------------------------------------- self-test
def _report(name, fn, slug):
    import json
    from pathlib import Path
    try:
        au = fn()
    except Exception as e:  # should never happen
        print(f"\n== {name}: RAISED {type(e).__name__}: {e}")
        return []
    ps = [(a["date"], p) for a in au for p in a["parcels"]]
    won = [p for _, p in ps if p.get("winningBid") is not None]
    dates = sorted(a["date"] for a in au)
    print(f"\n== {name}: {len(au)} auctions, {len(ps)} parcels, {len(won)} with winningBid, "
          f"dates {dates[0] if dates else '-'} .. {dates[-1] if dates else '-'}")
    for a in sorted(au, key=lambda a: a["date"]):
        n = len(a["parcels"])
        print(f"   {a['date']} {a['kind']:8} parcels={n:3} priced={sum(p.get('winningBid') is not None for p in a['parcels']):3} "
              f"nobid={sum(p.get('status') == 'No bid' for p in a['parcels']):3}  {urllib.parse.unquote(a['source']).rsplit('/', 1)[1][:60]}")
    for d, p in ps[:1] + ps[len(ps) // 2:len(ps) // 2 + 1] + ps[-1:]:
        print("   sample", d, p)
    f = Path(__file__).resolve().parent / "data" / f"{slug}.json"
    if f.exists():
        old = json.loads(f.read_text()).get("auctions", [])
        pairs = {(a["date"], p["parcel"]) for a in old for p in a["parcels"]}
        mine = {(d, p["parcel"]) for d, p in ps}
        loose = {(d, _norm(x)) for d, x in mine}
        print(f"   existing data/{slug}.json: {len(pairs)} (date, parcel) pairs; exact match {len(pairs & mine)}, "
              f"match ignoring punctuation {sum((d, _norm(x)) in loose for d, x in pairs)}; "
              f"existing parcels on any date {len({x for _, x in pairs} & {x for _, x in mine})}")
    return au


if __name__ == "__main__":
    import sys
    tests = [("fayette_results", fayette_results, "fayette"), ("carroll_results", carroll_results, "carroll"),
             ("carroll_excess_prices", carroll_excess_prices, "carroll"), ("meriwether_excess", meriwether_excess, "meriwether"),
             ("walton_excess", walton_excess, "walton"), ("troup_derived", troup_derived, "troup")]
    for name, fn, slug in tests:
        if len(sys.argv) > 1 and not any(a in name for a in sys.argv[1:]):
            continue
        _report(name, fn, slug)
