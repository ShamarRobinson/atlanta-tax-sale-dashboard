"""Parsers for the core metro counties."""
import re
from html.parser import HTMLParser
from ga_common import get, pdf_text, cols, money, iso, parcel_rec, group_by_date, xlsx_rows, clean

GW = "https://www.gwinnetttaxcommissioner.com/documents/d/egov/"


def gwinnett():
    out = []
    src = GW + "2021-2022-2023-2024-2025-tax-sale-results?download=true"
    t = pdf_text(get(src))
    rx = re.compile(r"^\s*(\d{1,2}/\d{1,2}/\d{4})\s+(R\d{4}\s?[A-Z0-9]{3,4}[A-Z]?)\s+(.*)$")
    recs = []
    for line in t.splitlines():
        m = rx.match(line)
        if not m:
            continue
        d, parcel, rest = m.groups()
        amts = re.findall(r"\$[\d,]+\.\d{2}|No Bid", rest)
        parts = cols(re.split(r"\$[\d,]+\.\d{2}|No Bid", rest)[0])
        owner = parts[0] if parts else ""
        addr = parts[1] if len(parts) > 1 else ""
        mid = re.split(r"\$[\d,]+\.\d{2}", rest)
        buyer = cols(mid[1])[0] if len(mid) > 2 and cols(mid[1]) else None
        mb = money(amts[0]) if amts else None
        won = money(amts[1]) if len(amts) > 1 and amts[1] != "No Bid" else None
        sold = won is not None
        recs.append((iso(d), parcel_rec(parcel, re.sub(r"\(includes.*?\)", "", owner), addr, mb, won, buyer if sold else None, status="Sold" if sold else "No bid")))
    out += group_by_date(recs, "results", src)
    # current list
    src = GW + "october-2026-tax-sale-web-list?download=true"
    try:
        t = pdf_text(get(src))
        dm = re.search(r"(January|February|March|April|May|June|July|August|September|October|November|December) (\d{1,2}), (\d{4})", t)
        d = iso(dm.group(0)) if dm else None
        ps = []
        for line in t.splitlines():
            m = re.match(r"^\s*(R\d{4}\s?[A-Z0-9]{3,4}[A-Z]?)\s+(.*?)\s{2,}(.*?)\s+(\$[\d,]+\.\d{2})\s*$", line)
            if m:
                ps.append(parcel_rec(m.group(1), m.group(2), m.group(3), money(m.group(4)), status="Listed"))
        if ps and d:
            out.append({"date": d, "kind": "list", "source": src, "parcels": ps})
    except Exception:
        pass
    # excess funds (month/year only) -> attach to results by parcel
    try:
        t = pdf_text(get(GW + "excess-funds-all-years-rev05019026-pdf?download=true"))
        ex = {}
        for line in t.splitlines():
            m = re.search(r"(R\d{4}\s?[A-Z0-9]{3,4}[A-Z]?)\s+.*?\$([\d,]+\.\d{2})\s+([A-Za-z]+ \d{4})", line)
            if m:
                ex[re.sub(r"\s", "", m.group(1))] = money(m.group(2))
        for a in out:
            for p in a["parcels"]:
                k = re.sub(r"\s", "", p["parcel"])
                if k in ex:
                    p["excess"] = ex[k]
    except Exception:
        pass
    return out


def cobb():
    src = "https://cms9files.revize.com/cobbcounty/Excess%20Funds%20for%20web%20site%20Excel%20Template%20Seal_September.pdf"
    t = pdf_text(get(src))
    recs = []
    for line in t.splitlines():
        m = re.match(r"^\s*(\d{1,2}/\d{1,2}/\d{4})\s+(.*?)\s+(\d{2}-\d{4}-\d-\d{3}-\d)\s+\$([\d,]+\.\d{2})", line)
        if not m:
            continue
        d, mid, parcel, ex = m.groups()
        parts = cols(mid)
        buyer = parts[0] if parts else None
        owner = " ".join(parts[1:]) if len(parts) > 1 else None
        recs.append((iso(d), parcel_rec(parcel, owner, None, buyer=buyer, excess=money(ex), status="Sold")))
    return group_by_date(recs, "excess", src, "Only sales that produced excess funds are listed.")


class _Table(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell, self.inc = [], None, "", False

    def handle_starttag(self, tag, a):
        if tag == "tr": self.row = []
        if tag in ("td", "th"): self.inc, self.cell = True, ""

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.row is not None:
            self.row.append(clean(self.cell)); self.inc = False
        if tag == "tr" and self.row is not None:
            if self.row: self.rows.append(self.row)
            self.row = None

    def handle_data(self, d):
        if self.inc: self.cell += d


def html_rows(html):
    p = _Table(); p.feed(html); return p.rows


def dekalb():
    out = []
    src = "https://publicaccess.dekalbtaxga.gov/content/search/tax_sale_listing.html"
    rows = html_rows(get(src).decode("utf-8", "replace"))
    hdr = None
    ps, d = [], None
    for r in rows:
        if "Parcel ID" in r and "Owner" in " ".join(r):
            hdr = r; continue
        if hdr and len(r) >= len(hdr) - 1:
            rec = dict(zip(hdr, r))
            d = d or iso(rec.get("Tax Sale Date"))
            yrs = f"{rec.get('Min Year','')}-{rec.get('Max Year','')}".strip("-")
            ps.append(parcel_rec(rec.get("Parcel ID"), rec.get("Owner"), rec.get("Address"), money(rec.get("Total Tax Due")), status="Listed", years=yrs))
    if ps and d:
        out.append({"date": d, "kind": "list", "source": src, "parcels": ps})
    src = "https://dekalbtaxga.gov/wp-content/uploads/Excess-Funds-List.pdf"
    t = pdf_text(get(src))
    recs = []
    for line in t.splitlines():
        m = re.match(r"^\s*(\d{2} \d{3} \d{2} \d{3})\s+\$([\d,]+\.\d{2})\s+(\d{1,2}/\d{1,2}/\d{4})\s+(.*)$", line)
        if m:
            parts = cols(m.group(4))
            # name parts then situs, city, zip
            zipc = parts[-1] if parts and re.match(r"\d{5}", parts[-1]) else ""
            city = parts[-2] if zipc and len(parts) > 2 else ""
            situs = parts[-3] if zipc and len(parts) > 3 else ""
            name = " ".join(parts[:-3]) if zipc else " ".join(parts)
            recs.append((iso(m.group(3)), parcel_rec(m.group(1), name, f"{situs}, {city}".strip(", "), excess=money(m.group(2)), status="Sold")))
    out += group_by_date(recs, "excess", src, "Only sales that produced excess funds are listed.")
    return out


CLAY = "https://publicaccess.claytoncountyga.gov/content/PDF/"
CLAY_LISTS = ["OCTOBER%20TAX%20SALE%20LISTING%209-8-26.pdf", "NOVEMBER%20TAX%20SALE%20LISTING%209-8-26.pdf", "september_2026_tax_sale.pdf", "august_2026_tax_sale.pdf",
              "july_2026_tax_sale.pdf", "june_2026_tax_sale.pdf", "feb_tax_sale_listing_2026.pdf", "oct_tax_sale_listing_2025.pdf", "AUGUST%202025%20TAX%20SALE%20LISTING.pdf",
              "JULY%202025%20TAX%20SALE%20LISTING.pdf", "JUNE%202025%20TAX%20SALE%20LISTING.pdf", "Tax%20Sale%20Listing%20-%20May%202025.pdf", "Updated%20October%20Tax%20Sale%20List.pdf"]


def clayton():
    out = []
    seen = set()
    for f in CLAY_LISTS:
        src = CLAY + f
        try:
            t = pdf_text(get(src))
        except Exception:
            continue
        recs, cur = [], None
        for line in t.splitlines():
            m = re.match(r"^\s*(\d{2}/\d{2}/\d{4})\s+(\d{5}[A-Z]?\s(?:[A-Z]\d{3}|\d{6})(?:\s[A-Z]\d{2})?)\s*(.*?)\s+((?:\d{4},?)+)\s+([\d,]+)\s+([\d,]+\.\d{2})\s*$", line)
            if m:
                d, parcel, mid, yrs, fmv, bid = m.groups()
                parts = cols(mid)
                own = parts[0] if parts else ""
                loc = " ".join(parts[1:]) if len(parts) > 1 else ""
                if not loc and "/" in own and not own.startswith("/"):
                    a, b = own.split("/", 1)
                    loc, own = a, b
                cur = parcel_rec(parcel, own.lstrip("/"), loc, money(bid), status="Listed", years=yrs.replace(" ", ""), value=money(fmv))
                recs.append((iso(d), cur))
            elif cur and line.strip() and not re.search(r"Date|Parcel|Clayton|Tax Sale|Page", line):
                p = cols(line)
                if p:
                    cur["owner"] = clean((cur.get("owner", "") + " " + p[0]).lstrip("/"))
                    if len(p) > 1 and len(p[1]) < 12:
                        cur["address"] = clean(cur.get("address", "") + " " + p[1])
            elif not line.strip():
                cur = None
        for a in group_by_date(recs, "list", src):
            if a["date"] not in seen:
                seen.add(a["date"]); out.append(a)
    src = CLAY + "DQ759GA.pdf"
    t = pdf_text(get(src))
    recs = []
    for line in t.splitlines():
        m = re.match(r"^\s*(.*?)\s{2,}(\d{5}[A-Z]?\s+[A-Z0-9 ]{3,14}?)\s+\$\s*([\d,]+\.\d{2})\s+(\d{2}/\d{2}/\d{2})", line)
        if m:
            recs.append((iso(m.group(4)), parcel_rec(m.group(2), m.group(1), excess=money(m.group(3)), status="Sold")))
    out += group_by_date(recs, "excess", src, "Only sales that produced excess funds are listed.")
    return out


def henry():
    src = "https://www.henrycountytax.com/DocumentCenter/View/553/Property-Tax-Sale--2026"
    t = pdf_text(get(src))
    recs = []
    for line in t.splitlines():
        m = re.match(r"^\s*([A-Z0-9][A-Z0-9-]{8,13})\s+(.*?)\s+(\d{2}/\d{2}/\d{4})", line)
        if m:
            parcel, mid, d = m.groups()
            parts = cols(mid)
            city = parts[-1] if len(parts) > 1 else ""
            body = " ".join(parts[:-1]) if len(parts) > 1 else mid
            am = re.search(r"\s(\d+\s+[A-Z].*|[A-Z]+ (?:RD|DR|LN|ST|CT|WAY|CIR|TRL|PKWY|HWY|AVE|BLVD|PL))$", body)
            owner, loc = (body[:am.start()], am.group(1)) if am else (body, "")
            recs.append((iso(d), parcel_rec(parcel, owner, f"{loc}, {city}", status="Listed")))
    return group_by_date(recs, "list", src)


def rockdale():
    src = "https://rockdaletaxoffice.org/cache/published/public/tax-sale-documents/tax-sale-properties/tax_sale_october_6_2026.xlsx"
    rows = xlsx_rows(get(src))
    hdr = None
    m = re.search(r"_([a-z]+)_(\d+)_(\d{4})", src)
    d = iso(f"{m.group(1).title()} {m.group(2)}, {m.group(3)}") if m else None
    ps = {}
    for r in rows:
        r = [x for x in r]
        if hdr is None:
            if r and any(str(c or "").strip().lower() in ("pid", "owner name") for c in r):
                hdr = [str(c or "").strip() for c in r]
            continue
        rec = dict(zip(hdr, r))
        pid = rec.get("PID")
        if not pid:
            continue
        p = ps.setdefault(str(pid), parcel_rec(str(pid), rec.get("Owner Name"), None, 0.0, status="Listed", extra={"type": clean(rec.get("Property Type"))}))
        p["minBid"] = round(p.get("minBid", 0) + (money(rec.get("Current Due")) or 0), 2)
        yrs = p.get("years", "")
        y = str(rec.get("Year") or "")
        p["years"] = ",".join(sorted(set(filter(None, yrs.split(",") + [y]))))
    return [{"date": d, "kind": "list", "source": src, "parcels": list(ps.values())}] if ps and d else []


FULTON = "https://www.fultoncountyga.gov/-/media/Departments/Sheriff/Tax-Sales/"
FULTON_LISTS = [("2026/Sheriffs-October-6-2026-Tax-Levy-Sale-List.pdf", "2026-10-06"),
                ("2026/Sheriffs-September-1-2026-Levy-Sale--1st-Website-Posting-872026.pdf", "2026-09-01"),
                ("2026/Sheriffs-August-4-2026-Levy-Sale-List--2nd-Posting.pdf", "2026-08-04"),
                ("2025/FCSO-AUGUST-5-2025-2nd-Website-Posting-812025.pdf", "2025-08-05"),
                ("2024/FCSO-April-2-2024-2nd-Website-Posting--Sheriffs-Sale-List.pdf", "2024-04-02")]
FULTON_GIS = "https://gismaps.fultoncountyga.gov/arcgispub2/rest/services/PropertyMapViewer/PropertyMapViewer/MapServer/11/query"


def fulton():
    import urllib.parse, json
    out = []
    for f, d in FULTON_LISTS:
        src = FULTON + f
        try:
            t = pdf_text(get(src), ocr=True)
        except Exception:
            continue
        ps = []
        for line in t.splitlines():
            m = re.match(r"^\s*(\d{4}-\d{4,5})\s+([0-9O]{2}[A-Z]?\s?-\s?[0-9A-Z]{4}\s?-\s?[0-9A-Z ]{2,4}\s?-\s?[0-9A-Z]{3}\s?-\s?[0-9A-Z])\s+(.*)$", line)
            if m:
                pid = re.sub(r"\s", "", m.group(2))
                pid = re.sub(r"^O", "0", pid)
                ps.append(parcel_rec(pid, None, m.group(3), status="Listed", extra={"saleNo": m.group(1)}))
        if ps:
            out.append({"date": d, "kind": "list", "source": src, "parcels": ps})
    # owner + appraised value from county GIS
    for a in out[:2]:
        for p in a["parcels"]:
            pid = p["parcel"].replace("-", "")
            key = (pid[:2] + " " + pid[2:]) if pid[2].isdigit() else (pid[:3] + pid[3:])
            try:
                q = FULTON_GIS + "?" + urllib.parse.urlencode({"where": f"ParcelID='{key}'", "outFields": "Owner,TotAppr,Address", "returnGeometry": "false", "f": "json"})
                fs = json.loads(get(q, tries=2, timeout=30)).get("features", [])
                if fs:
                    at = fs[0]["attributes"]
                    p["owner"] = clean(at.get("Owner")); p["value"] = at.get("TotAppr")
            except Exception:
                pass
    return out
