"""Parsers for the outer-ring counties."""
import re
from datetime import datetime
from ga_common import (get, pdf_text, doc_text, cols, money, iso, parcel_rec, group_by_date, xlsx_rows, xls_rows,
                       clean, wp_media, govwin_feed)

EXCESS_NOTE = "Only sales that produced excess funds are listed, so this is a partial view of what sold."
S3 = "https://images-governmentwindow.s3.amazonaws.com/resources/sites/"


def coweta():
    src = "https://www.cowetataxcom.com/resources/sites/cowetacountyga/docs/EXCESS%20FUNDS%20LIST.pdf"
    t = pdf_text(get(src))
    recs = []
    for line in t.splitlines():
        m = re.match(r"^\s*(\d{1,2}/\d{1,2}/\d{4})\s+(.*?)\s+\$\s*([\d,]+\.\d{2})\s+\$\s*([\d,]+\.\d{2})\s+\$\s*([\d,]+\.\d{2})", line)
        if m:
            d, mid, mb, sale, over = m.groups()
            p = cols(mid)
            if len(p) >= 3:
                recs.append((iso(d), parcel_rec(p[0], " ".join(p[1:-1]), None, money(mb), money(sale), p[-1], money(over), "Sold")))
    return group_by_date(recs, "excess", src, EXCESS_NOTE)


def walton():
    out = []
    src = "https://tax.waltoncountypay.com/resources/sites/waltoncountyga/docs/RP_Sale_.xlsx"
    rows = xlsx_rows(get(src))
    d, hdr, ps = None, None, []
    for r in rows:
        txt = " ".join(str(c) for c in r if c)
        if not d:
            m = re.search(r"([A-Z][a-z]+ \d{1,2}, \d{4})", txt)
            if m: d = iso(m.group(1))
        if "MAP/PARCEL" in txt:
            hdr = [str(c or "").strip() for c in r]; continue
        if hdr:
            rec = dict(zip(hdr, r))
            if rec.get("MAP/PARCEL"):
                ps.append(parcel_rec(str(rec["MAP/PARCEL"]), rec.get("NAME"), rec.get("LOCATION ADDRESS"), money(rec.get("EST. TAXES/FEES")),
                                     status="Listed", years=str(rec.get("YEARS") or ""), extra={"desc": clean(rec.get("DESCRIPTION +/-"))}))
    if ps and d:
        out.append({"date": d, "kind": "list", "source": src, "parcels": ps})
    src = "https://tax.waltoncountypay.com/resources/sites/waltoncountyga/docs/6abd0e9534a7a-Excess%20funds%20current%209-21-26.pdf"
    try:
        t = pdf_text(get(src))
    except Exception:
        return out
    recs, cur = [], None
    for line in t.splitlines():
        m = re.match(r"^\s*(.*?)\s{2,}([A-Z]{0,2}\d{3,4}[A-Z]?-\d{3}[A-Z]?)\s+\$\s*([\d,]+\.\d{2})\s+(\d{1,2}/\d{1,2}/\d{4})\s+\$\s*([\d,]+\.\d{2})\s+\$\s*([\d,]+\.\d{2})", line)
        if m:
            name, parcel, sale, d, due, ex = m.groups()
            cur = parcel_rec(parcel, name, None, money(due), money(sale), excess=money(ex), status="Sold")
            recs.append((iso(d), cur)); continue
        if cur and line.strip().startswith("PURCHASER:"):
            cur["buyer"] = clean(line.split(":", 1)[1])[:60]; cur = None
        elif cur and not cur.get("address") and line.strip() and "NAME" not in line:
            cur["address"] = clean(line)
    out += group_by_date(recs, "excess", src, EXCESS_NOTE)
    return out


def polk():
    out = []
    src = "https://www.polkcountypay.com/resources/sites/polkcountyga/docs/Tax_Sale_List.xlsx"
    data = get(src)
    import openpyxl, io
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    name = wb.sheetnames[0]
    mo = re.search(r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})", name)
    d = None
    if mo:
        dt = datetime.strptime(f"{mo.group(1)} 1 {mo.group(2)}", "%B %d %Y")
        # first Tuesday of the month
        d = dt.replace(day=1 + (1 - dt.weekday()) % 7).strftime("%Y-%m-%d")
    rows = xlsx_rows(data)
    hdr = [str(c or "").strip() for c in rows[0]]
    ps = []
    for r in rows[1:]:
        rec = dict(zip(hdr, r))
        if not rec.get("MAP") or str(rec.get("REMOVE") or "").strip() or not money(rec.get("TOTAL")):
            continue
        ps.append(parcel_rec(str(rec["MAP"]), rec.get("DEFENDANT"), None, money(rec.get("TOTAL")), status="Listed", years=str(rec.get("YRS OWED") or "")))
    if ps and d:
        out.append({"date": d, "kind": "list", "source": src, "parcels": ps})
    src = "https://www.polkcountypay.com/resources/sites/polkcountyga/docs/EXCESS_FUNDS_LIST_UPDATED_10172024.xls"
    rows = [str(r[0]).strip() if r else "" for r in xls_rows(get(src))]
    recs, cur, d = [], None, None
    for line in rows + [""]:
        if line.startswith("SALE DATE"):
            d = iso(line.split("DATE", 1)[1]); cur = {"lines": []}
        elif cur is not None and line.startswith("SOLD FOR"):
            cur["sold"] = money(line)
        elif cur is not None and line.startswith("MAP:"):
            cur["map"] = line.split(":", 1)[1].strip()
        elif cur is not None and line.startswith("EXCESS"):
            cur["excess"] = money(line)
            # each record: owner line(s) and a land lot line (in either order), then the owner's last known mailing address
            L = cur["lines"]
            mail, head = (L[-2:], L[:-2]) if len(L) >= 3 and re.search(r"\b[A-Z]{2}\.?\s+\d{5}", L[-1]) else ([], L)
            legal = [x for x in head if re.match(r"(LLS?|LTS?|LOTS?|TR|PT|BLK|DIST|LAND LOT)\b|\d+(ST|ND|RD|TH)\s+(DIST|SECT)|.*\b(DISTRICT|SECT(ION)?|S/D|SURVEY)\b", x, re.I)]
            owner = " ".join(x for x in head if x not in legal) or None
            extra = {}
            if legal:
                extra["desc"] = " ".join(legal)
            if mail:
                extra["mail"] = ", ".join(mail)     # not the property address: fetch_all.py decides whether to show it
            recs.append((d, parcel_rec(cur.get("map", "?"), owner, None, None, cur.get("sold"), excess=cur["excess"], status="Sold", extra=extra)))
            cur = None
        elif cur is not None and line:
            cur["lines"].append(line)
    out += group_by_date(recs, "excess", src, EXCESS_NOTE)
    return out


def meriwether():
    out = []
    lists = [("https://images-governmentwindow.s3.dualstack.us-east-1.amazonaws.com/resources/sites/meriwethercountyga/docs/SEPT%201%20TAX%20SALE%20LIST%20FOR%20WEBSITE.pdf", "2026-09-01"),
             ("https://images-governmentwindow.s3.dualstack.us-east-1.amazonaws.com/resources/sites/meriwethercountyga/docs/JULY%202026%20TAX%20SALE%20WEBSITE%20UPDATED%207-2-26.pdf.pdf", "2026-07-07"),
             ("https://www.meriwethertax.com/resources/sites/meriwethercountyga/docs/MAY%206,%202025%20TAX%20SALE.pdf", "2025-05-06")]
    for src, d in lists:
        try:
            t = pdf_text(get(src))
        except Exception:
            continue
        ps, pend = [], None
        for line in t.splitlines():
            m = re.match(r"^\s*([A-Z0-9]{2,5}(?:\s+[A-Z0-9]{2,4}){1,3})\s+(.*?)\s+(\d{4}(?:-\d{4})?)\s+\$([\d,]+\.\d{2})", line)
            if m:
                parcel, name, yr, due = m.groups()
                ps.append(parcel_rec(re.sub(r"\s+", " ", parcel), name or pend, None, money(due), status="Listed", years=yr)); pend = None
                continue
            m = re.match(r"^\s*([A-Z0-9]{2,5}(?:\s+[A-Z0-9]{2,4}){0,3})\s+(\d{4}(?:-\d{4})?)\s+\$([\d,]+\.\d{2})", line)
            if m:
                ps.append(parcel_rec(re.sub(r"\s+", " ", m.group(1)), pend, None, money(m.group(3)), status="Listed", years=m.group(2))); pend = None
            elif line.strip() and not re.search(r"PARCEL|QPUBLIC|qPublic", line):
                pend = clean(line)
        if ps:
            out.append({"date": d, "kind": "list", "source": src, "parcels": ps})
    src = "https://www.meriwethertax.com/resources/sites/meriwethercountyga/docs/6ab28b1724ce5-EXCESS%20FUNDS%20WEB%20UPDATED%209-22-26.pdf"
    t = pdf_text(get(src))
    recs, pid, own = [], None, []
    MON = {m: i for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
    for line in t.splitlines():
        s = line.rstrip()
        mp = re.match(r"^\s{0,3}([A-Z0-9]{2,5}(?:\s[A-Z0-9]{2,4}){1,3})(\s{2,}|$)(.*)$", s)
        if mp and not re.search(r"PID|OWNER", s):
            pid, rest = mp.group(1), mp.group(3)
            own = [cols(rest)[0]] if rest.strip() and not re.search(r"\$", rest) else []
            s = rest if re.search(r"\$", rest) else ""
        m = re.search(r"([A-Z][a-z]{2})-(\d{2})\s+(.*?)\s+\$\s*([\d,]+\.\d{2})\s+\$\s*([\d,]+\.\d{2})\s+\$\s*([\d,]+\.\d{2})", s)
        md = re.search(r"(\d{1,2}/\d{1,2}/\d{2,4})\s+(.*?)\s+\$\s*([\d,]+\.\d{2})\s+\$\s*([\d,]+\.\d{2})\s+\$\s*([\d,]+\.\d{2})", s)
        if (m or md) and pid:
            if m:
                mon, yy, buyer, due, price, ex = m.groups()
                d = None
                dt = datetime(2000 + int(yy), MON.get(mon, 1), 1)
                d = dt.replace(day=1 + (1 - dt.weekday()) % 7).strftime("%Y-%m-%d")
            else:
                dd, buyer, due, price, ex = md.groups(); d = iso(dd)
            pre = s[:(m or md).start()]
            if cols(pre):
                own.append(cols(pre)[0])
            recs.append((d, parcel_rec(pid, " ".join(own), None, money(due), money(price), cols(buyer)[0] if cols(buyer) else buyer, money(ex), "Sold")))
            pid, own = None, []
        elif pid and s.strip():
            c = cols(s)
            if c: own.append(c[0])
    out += group_by_date(recs, "excess", src, EXCESS_NOTE)
    return out


def forsyth():
    from counties_metro import html_rows
    src = "https://forsythcountytax.com/excess-funds-listing-2/"
    rows = html_rows(get(src).decode("utf-8", "replace"))
    recs = []
    for r in rows:
        if len(r) >= 8 and iso(r[0]):
            recs.append((iso(r[0]), parcel_rec(r[1], r[2], r[4], money(r[6]), money(r[5]), excess=money(r[7]), status="Sold")))
    return group_by_date(recs, "excess", src, EXCESS_NOTE)


def _carroll_parse(t):
    ps = []
    blocks = re.split(r"MAP AND PARCEL\s*:\s*", t)[1:]
    for b in blocks:
        b1 = clean(b)
        m = re.match(r"([A-Z0-9-]+)\s*(NO BID|SOLD FOR \$?[\d,]+\.?\d*)?", b1)
        if not m:
            continue
        parcel, res = m.group(1), m.group(2) or ""
        own = re.search(r"CURRENT RECORD HOLDER:\s*(.*?)\s*(?:DEFENDANT IN FI|AMOUNT DUE)", b1)
        due = re.search(r"AMOUNT DUE:\s*\$?([\d,]+\.\d{2})", b1)
        yrs = re.search(r"TAX\s*YEARS DUE:\s*([\d\-, ]+)", b1)
        addr = re.search(r"(\d{1,6}\s+[A-Z][A-Z0-9 ]{3,40}?(?:RD|ROAD|DR|DRIVE|LN|LANE|ST|STREET|CT|COURT|CIR|CIRCLE|WAY|TRL|TRAIL|HWY|PKWY|AVE|TERR|PL|PLACE)\b)[^A-Z]*$", b1)
        st = "Sold" if res.startswith("SOLD") else ("No bid" if res == "NO BID" else "Listed")
        ps.append(parcel_rec(parcel, own.group(1) if own else None, addr.group(1) if addr else None, money(due.group(1)) if due else None,
                             money(res) if st == "Sold" else None, status=st, years=yrs.group(1).strip() if yrs else None))
    return ps


MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"


def _sale_date(t):
    t = clean(t)
    for pat in (r"Results from the\s+(?:(?:" + MONTHS + r")\s+\d{1,2},?\s+\d{4})", r"same being\s+(?:(?:" + MONTHS + r")\s+\d{1,2},?\s+\d{4})",
                r"(?:Tuesday|TUESDAY),?\s+(?:(?:" + MONTHS + r")\s+\d{1,2},?\s+\d{4})"):
        m = re.search(pat, t, re.I)
        if m:
            dm = re.search(r"(" + MONTHS + r")\s+(\d{1,2}),?\s+(\d{4})", m.group(0), re.I)
            return iso(f"{dm.group(1).title()} {dm.group(2)}, {dm.group(3)}")
    return None


def carroll():
    out = []
    site = "https://carrollcountygatax.com"
    media = wp_media(site, "legals")
    by = {}
    for it in media:
        u = it["source_url"]
        if not re.search(r"\.(pdf|docx?)$", u, re.I) or "mobile" in u.lower():
            continue
        key = re.sub(r"[-_]?\d*\.(pdf|docx?)$", "", u.rsplit("/", 1)[1].lower())
        if key not in by or it["date"] > by[key]["date"]:
            by[key] = it
    for key, it in sorted(by.items(), key=lambda kv: kv[1]["date"], reverse=True)[:14]:
        u = it["source_url"]
        try:
            data = get(u)
            ext = u.rsplit(".", 1)[1].lower()
            t = pdf_text(data, layout=False) if ext == "pdf" else doc_text(data, ext)
        except Exception:
            continue
        d = _sale_date(t)
        if not d:
            mm = re.search(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[-_ ]?(\d{4})", key)
            if mm:
                dt = datetime.strptime(f"{mm.group(1)} 1 {mm.group(2)}", "%b %d %Y")
                d = dt.replace(day=1 + (1 - dt.weekday()) % 7).strftime("%Y-%m-%d")
        ps = _carroll_parse(t)
        if ps and d:
            has_res = any(p["status"] != "Listed" for p in ps)
            prev = next((a for a in out if a["date"] == d), None)
            if prev and (prev["kind"] == "results" or not has_res):
                continue
            if prev:
                out.remove(prev)
            out.append({"date": d, "kind": "results" if has_res else "list", "source": u, "parcels": ps})
    # excess funds (grouped under date rows)
    src = "https://carrollcountygatax.com/wp-content/uploads/2026/09/EXCESS_FUNDS_LIST-1.xls"
    try:
        rows = xls_rows(get(src))
        recs, d = [], None
        for r in rows:
            r = list(r) + [""] * 6
            if isinstance(r[1], datetime) and not r[0]:
                d = r[1].strftime("%Y-%m-%d"); continue
            if d and r[0] and money(r[5]) is not None:
                recs.append((d, parcel_rec(str(r[0]), r[1], r[3], excess=money(r[5]), status="Sold")))
        ex = group_by_date(recs, "excess", src, EXCESS_NOTE)
        have = {a["date"] for a in out}
        out += [a for a in ex if a["date"] not in have]
    except Exception:
        pass
    return out


def pickens():
    src = "https://pickensgatax.com/wp-content/uploads/2026/08/excess_funds.xlsx"
    for it in sorted(wp_media("https://pickensgatax.com", "excess"), key=lambda x: x["date"], reverse=True):
        if it["source_url"].lower().endswith(".xlsx"):
            src = it["source_url"]; break
    rows = xlsx_rows(get(src))
    hdr, recs = None, []
    for r in rows:
        if r and "PID" in [str(c or "").strip() for c in r]:
            hdr = [str(c or "").strip() for c in r]; continue
        if hdr:
            rec = dict(zip(hdr, r))
            if rec.get("PID") and iso(rec.get("SALE DATE")):
                recs.append((iso(rec["SALE DATE"]), parcel_rec(str(rec["PID"]), rec.get("DEFENDANT IN FI.FA"), rec.get("DEFENDANT ADDRESS"),
                                                                money(rec.get("STARTING BID")), money(rec.get("SELLING AMOUNT")), excess=money(rec.get("EXCESS FUNDS")), status="Sold")))
    return group_by_date(recs, "excess", src, EXCESS_NOTE)


def troup():
    out = []
    site = "https://troupcountytax.com"
    files = [it for it in wp_media(site) if re.search(r"sale-list|Sale-List", it["source_url"]) and it["source_url"].lower().endswith(".pdf")]
    files += [{"source_url": site + "/wp-content/uploads/2025/03/tax-sale-list-april-2025.pdf", "date": "2025-03-01"}]
    seen = set()
    for it in sorted(files, key=lambda x: x["date"], reverse=True):
        src = it["source_url"]
        try:
            t = pdf_text(get(src))
        except Exception:
            continue
        dm = re.search(r"DATE OF SALE\s+([A-Z]+ \d{1,2}, \d{4})", t, re.I)
        d = iso(dm.group(1).title()) if dm else None
        if not d or d in seen:
            continue
        ps, cur = [], None
        for line in t.splitlines():
            m = re.match(r"^\s*(\d{1,5})\s+(.*?)\s{2,}(.*?)\s{2,}([0-9A-Z]{4,5}[A-Z]?\s+\d{3}\s+\d{3}[A-Z]?)\s+\$([\d,]+\.\d{2})", line)
            if m:
                cur = parcel_rec(m.group(4), m.group(2), m.group(3), money(m.group(5)), status="Listed")
                ps.append(cur)
            elif cur and line.strip():
                c = cols(line)
                if len(c) == 2:
                    cur["type"], city = c
                    cur["address"] = f"{cur.get('address','')}, {city}"
                cur = None
        if ps:
            seen.add(d); out.append({"date": d, "kind": "list", "source": src, "parcels": ps})
    exs = [it for it in wp_media(site, "excess") + wp_media(site, "web-list") if it["source_url"].lower().endswith(".pdf")]
    src = max(exs, key=lambda x: x["date"])["source_url"] if exs else "https://troupcountytax.com/wp-content/uploads/2026/09/09.11.26-Web-list.pdf"
    t = pdf_text(get(src))
    recs = []
    for line in t.splitlines():
        m = re.match(r"^\s*(\d{1,2}/\d{1,2}/\d{4})\s+(.*?)\s{2,}(\S{4,6}-\d{3}-\d{3}\S*)\s+(.*?)\s+\$([\d,]+\.\d{2})", line)
        if m:
            recs.append((iso(m.group(1)), parcel_rec(m.group(3), m.group(2), m.group(4), excess=money(m.group(5)), status="Sold")))
    out += group_by_date(recs, "excess", src, EXCESS_NOTE)
    return out


def spalding():
    out = []
    site = "https://spaldingcountytaxga.com"
    media = wp_media(site)
    for it in sorted(media, key=lambda x: x["date"], reverse=True):
        u = it["source_url"]
        if not u.lower().endswith(".xlsx"):
            continue
        try:
            rows = xlsx_rows(get(u))
        except Exception:
            continue
        title = " ".join(str(c) for c in rows[0] if c) if rows else ""
        mo = re.search(r"(JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER)\s+(\d{4})", (title + " " + u).upper())
        if not mo:
            continue
        dt = datetime.strptime(f"{mo.group(1).title()} 1 {mo.group(2)}", "%B %d %Y")
        d = dt.replace(day=1 + (1 - dt.weekday()) % 7).strftime("%Y-%m-%d")
        if d in {a["date"] for a in out}:
            continue
        hdr, ps = None, []
        for r in rows:
            cells = [str(c or "").strip() for c in r]
            if any("Parcel" in c or "PARCEL" in c for c in cells):
                hdr = cells; continue
            if hdr:
                rec = dict(zip(hdr, r))
                pid = rec.get("MAP/Parcel") or rec.get("MAP/PARCEL")
                if pid:
                    ps.append(parcel_rec(str(pid), rec.get("DEFENDENT") or rec.get("DEFENDANT"), rec.get("ADDRESS"), status="Listed", years=str(rec.get("YEARS ") or rec.get("YEARS") or "")))
        if ps:
            out.append({"date": d, "kind": "list", "source": u, "parcels": ps})
    return out


def floyd():
    out = []
    import xml.etree.ElementTree as ET
    keys, marker = [], ""
    for _ in range(5):
        x = get(f"https://images-governmentwindow.s3.amazonaws.com/?prefix=resources/sites/floydcountyga/docs/&marker={marker}").decode()
        root = ET.fromstring(x)
        ns = {"s": root.tag.split("}")[0].strip("{")}
        ks = [(c.find("s:Key", ns).text, c.find("s:LastModified", ns).text) for c in root.findall("s:Contents", ns)]
        keys += ks
        if root.find("s:IsTruncated", ns).text != "true" or not ks:
            break
        marker = ks[-1][0]
    ads = {}
    for k, mod in keys:
        name = k.rsplit("/", 1)[1].replace("%20", " ")
        m = re.search(r"Floyd\s*(?:Co)?\s*-\s*([A-Za-z]+)\.?\s*(\d{4})?.*?[Ww]eek\s*(\d)", name)
        if m and name.lower().endswith(".pdf") and m.group(1)[:3].title() in ("Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"):
            mon, yr, wk = m.groups()
            yr = yr or mod[:4]
            key = (mon[:3].title(), yr)
            if key not in ads or int(wk) > ads[key][0]:
                ads[key] = (int(wk), k)
    for (mon, yr), (wk, k) in sorted(ads.items(), key=lambda kv: datetime.strptime(f"{kv[0][0]} {kv[0][1]}", "%b %Y"), reverse=True)[:16]:
        src = "https://images-governmentwindow.s3.amazonaws.com/" + k.replace(" ", "%20")
        try:
            t = pdf_text(get(src), layout=False)
        except Exception:
            continue
        dt = datetime.strptime(f"{mon} 1 {yr}", "%b %d %Y")
        d = dt.replace(day=1 + (1 - dt.weekday()) % 7).strftime("%Y-%m-%d")
        ps = []
        for blk in re.split(r"File\s*#\s*", t)[1:]:
            b = clean(blk)
            m = re.search(r"Map/Parcel(?: Number)?:?\s*([A-Z0-9]+(?:\s[A-Z0-9]{2,4}){0,2})", b)
            own = re.search(r"Defendant\(s\) in FiFa:?\s*(.*?)\s*(?:Current|Reference|Property)", b)
            yrs = re.search(r"Years? Due:?\s*([\d\-, &]+)", b)
            desc = re.search(r"Property Description:?\s*(.*?)\s*(?:Years|$)", b)
            if m:
                ps.append(parcel_rec(m.group(1), own.group(1).split(";")[0] if own else None, None, status="Listed", years=yrs.group(1).strip() if yrs else None))
        if ps:
            out.append({"date": d, "kind": "list", "source": src, "parcels": ps})
    return out


def clarke():
    src = "https://accgov.com/DocumentCenter/View/16566"
    t = pdf_text(get(src))
    recs = []
    for line in t.splitlines():
        m = re.match(r"^\s*(\d{1,2}/\d{1,2}/\d{4})\s+(.*?)\s{2,}(.*?)\s*/\s*(\S+(?:\s\S+){0,2}?)\s+\$\s*([\d,]+\.\d{2})\s+\$\s*([\d,]+\.\d{2})\s+\$\s*([\d,]+\.\d{2})", line)
        if m:
            d, own, addr, parcel, bid, tax, ex = m.groups()
            recs.append((iso(d), parcel_rec(parcel, own, addr, money(tax), money(bid), excess=money(ex), status="Sold")))
    return group_by_date(recs, "excess", src, EXCESS_NOTE)


def dawson():
    out = []
    for src in ["https://dawsoncountytax.com/content/files/Tax%20Sale%20-%20October%206th%20-%20Updated%209.11.26.pdf",
                "https://dawsoncountytax.com/content/files/Tax%20Sale%20-%20Oct.%207%202025%20updated%209.4.25.pdf",
                "https://dawsoncountytax.com/content/files/Tax%20Sale%20-%20May%207%202024%20-Updated%203.18.24.pdf"]:
        try:
            t = pdf_text(get(src))
        except Exception:
            continue
        dm = re.search(r"(January|February|March|April|May|June|July|August|September|October|November|December|Oct\.?)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", t + " " + src.replace("%20", " "))
        mon = {"Oct": "October", "Oct.": "October"}.get(dm.group(1), dm.group(1)) if dm else None
        d = iso(f"{mon} {dm.group(2)}, {dm.group(3)}") if dm else None
        ps = []
        for line in t.splitlines():
            m = re.match(r"^\s*(\S{3,14})\s+(.*?)\s{2,}(\d{4})\s+(\d{4,6})\s+(.*?)\s+([\d.]+)\s+\$([\d,]+\.\d{2})", line)
            if m:
                pid, own, yr, bill, desc, ac, tot = m.groups()
                ps.append(parcel_rec(pid, own, None, money(tot), status="Listed", years=yr, extra={"desc": clean(desc), "acres": money(ac)}))
        if ps and d:
            out.append({"date": d, "kind": "list", "source": src, "parcels": ps})
    src = S3 + "dawsoncountyga/docs/Excess_Funds_List.pdf"
    t = pdf_text(get(src))
    recs = []
    for line in t.splitlines():
        m = re.match(r"^\s*(\d{1,2}/\d{1,2}/\d{4})\s+(.*?)\s{2,}(.*?)\s{2,}(\d{3}\s?\d{3}(?:\s\d{3})?)\s+\$([\d,]+\.\d{2})\s+\$([\d,]+\.\d{2})\s+\$([\d,]+\.\d{2})", line)
        if m:
            d, own, desc, parcel, a, b, ex = m.groups()
            # columns are BID AMT, TAXES, EXCESS (bid is the larger of the two)
            bid, tax = max(money(a), money(b)), min(money(a), money(b))
            recs.append((iso(d), parcel_rec(parcel, own, desc, tax, bid, excess=money(ex), status="Sold")))
    out += group_by_date(recs, "excess", src, EXCESS_NOTE)
    return out


def heard():
    src = "https://www.heardcountyga.com/Tax%20Sale%202025%20Update%202.xlsx"
    rows = xlsx_rows(get(src))
    hdr = [str(c or "").strip() for c in rows[0]]
    ps = []
    for r in rows[1:]:
        rec = dict(zip(hdr, r))
        if rec.get("Map Parcel"):
            amt = money(rec.get("Amount Due"))
            ps.append(parcel_rec(str(rec["Map Parcel"]), str(rec.get("MultiDif") or "").split(";")[0], str(rec.get("Location Address") or "").replace("\n", ", "),
                                 amt if amt else None, status="Listed", years=str(rec.get("Years Due") or "")))
    return [{"date": "2025-11-04", "kind": "list", "source": src, "note": "County file is labeled November 2025; the exact sale date is not stated.", "parcels": ps}] if ps else []


def lumpkin():
    src = S3 + "lumpkincountyga/docs/lumpkin%20county%20excess%20funds%20list.pdf"
    t = pdf_text(get(src))
    recs = []
    for line in t.splitlines():
        m = re.match(r"^\s*(\d{2}/\d{2}/\d{4})\s+(\S+)\s+(.*?)\s+\$([\d,]+\.\d{2})", line)
        if m:
            recs.append((iso(m.group(1)), parcel_rec(m.group(2), m.group(3), excess=money(m.group(4)), status="Sold")))
    return group_by_date(recs, "excess", src, EXCESS_NOTE)


def jackson():
    src = S3 + "jacksoncountyga/docs/March_2017_tax_sale_results.pdf"
    t = pdf_text(get(src))
    ps = []
    for line in t.splitlines():
        m = re.match(r"^\s*([0-9]{3}[A-Z0-9]{2,6})\s+(.*?)\s+([\d,]+\.\d{2})\s+(No Bid|[\d,]+\.\d{2})\s*$", line)
        if m:
            parcel, mid, due, res = m.groups()
            own = cols(mid)[0] if cols(mid) else ""
            sold = res != "No Bid"
            ps.append(parcel_rec(parcel, own, None, money(due), money(res) if sold else None, status="Sold" if sold else "No bid"))
    return [{"date": "2017-03-07", "kind": "results", "source": src, "parcels": ps}] if ps else []


def fayette():
    out = []
    src = "https://www.fayettecountypay.com/resources/sites/fayettecountyga/docs/2025%20Tax%20Sale%20List%20of%20Sold%20Properties.doc"
    t = clean(doc_text(get(src), "doc"))
    dm = re.search(r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),\s+(\d{4})", t)
    d = iso(dm.group(0)) if dm else "2025-12-02"
    ps = []
    for blk in re.split(r"(?=Map\s*(?:&|and)\s*Parcel)", t, flags=re.I)[1:]:
        pm = re.search(r"Parcel\s*(?:#|No\.?|Number)?:?\s*([0-9A-Z]{6,12})", blk)
        sold = re.search(r"SOLD FOR\s*\$?([\d,]+\.\d{2})", blk)
        due = re.search(r"Amount Due:?\s*\$?([\d,]+\.\d{2})", blk, re.I)
        buyer = re.search(r"SOLD TO:?\s*([A-Z0-9 ,.&'-]{3,60}?)(?:\s{2}|\d|$)", blk)
        own = re.search(r"Defendant\(?s?\)? in Fi\.?\s*Fa\.?:?\s*(.*?)\s*(?:Current|Property|Amount)", blk, re.I)
        yrs = re.search(r"Tax Years? Due:?\s*([\d, ]+)", blk, re.I)
        if pm:
            ps.append(parcel_rec(pm.group(1), own.group(1)[:80] if own else None, None, money(due.group(1)) if due else None,
                                 money(sold.group(1)) if sold else None, buyer.group(1).strip(" ,") if buyer else None, status="Sold" if sold else "Listed",
                                 years=yrs.group(1).strip(" ,") if yrs else None))
    if ps:
        out.append({"date": d, "kind": "excess", "source": src, "note": "County published only the parcels that sold, not the unsold ones.", "parcels": ps})
    return out


def douglas():
    src = "https://douglastax.org/pdf/2026/05/2026-Tax-Sale-June-Week-4.pdf"
    t = pdf_text(get(src), layout=False)
    ps = _carroll_parse(t)
    d = _sale_date(t) or "2026-06-02"
    return [{"date": d, "kind": "list", "source": src, "parcels": ps}] if ps else []
