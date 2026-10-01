"""Shared helpers for the Atlanta-area tax sale fetchers (standard library plus pdftotext/openpyxl/xlrd)."""
import io
import json
import re
import subprocess
import tempfile
import time
import urllib.request
from datetime import datetime

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
MONEY = re.compile(r"\$?\s*([0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]{1,2})?|[0-9]+(?:\.[0-9]{1,2})?)")


def get(url, headers=None, tries=3, timeout=60):
    h = {"User-Agent": UA, "Accept": "*/*"}
    h.update(headers or {})
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # retry transient resets
            last = e
            time.sleep(2 + 2 * i)
    raise last


def get_json(url, headers=None):
    return json.loads(get(url, headers).decode("utf-8", "replace"))


def pdf_text(data, layout=True, ocr=False):
    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(data)
        f.flush()
        args = ["pdftotext"] + (["-layout"] if layout else []) + [f.name, "-"]
        txt = subprocess.run(args, capture_output=True, text=True).stdout
        if ocr and len(txt.strip()) < 50:
            txt = ocr_pdf(f.name)
        return txt


def ocr_pdf(path):
    """OCR a scanned PDF page by page (needs pdftoppm + tesseract)."""
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(["pdftoppm", "-r", "250", "-gray", path, f"{d}/p"], check=False)
        import glob
        out = []
        for img in sorted(glob.glob(f"{d}/p*")):
            out.append(subprocess.run(["tesseract", img, "-", "--psm", "6"], capture_output=True, text=True).stdout)
        return "\n".join(out)


def doc_text(data, ext):
    """Convert .doc/.docx/.xls to text with LibreOffice (headless)."""
    with tempfile.TemporaryDirectory() as d:
        src = f"{d}/in.{ext}"
        open(src, "wb").write(data)
        target = "csv" if ext in ("xls", "xlsx") else "txt:Text"
        subprocess.run(["soffice", "--headless", "--convert-to", target, "--outdir", d, src], capture_output=True, timeout=180)
        outp = f"{d}/in.{'csv' if target == 'csv' else 'txt'}"
        try:
            return open(outp, encoding="utf-8", errors="replace").read()
        except FileNotFoundError:
            return ""


def xlsx_rows(data, sheet=None):
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    ws = wb[sheet] if sheet else wb.worksheets[0]
    return [[c for c in r] for r in ws.iter_rows(values_only=True)]


def xls_rows(data):
    import xlrd
    wb = xlrd.open_workbook(file_contents=data)
    ws = wb.sheet_by_index(0)
    out = []
    for i in range(ws.nrows):
        row = []
        for j, c in enumerate(ws.row(i)):
            if c.ctype == xlrd.XL_CELL_DATE:
                row.append(datetime(*xlrd.xldate_as_tuple(c.value, wb.datemode)))
            else:
                row.append(c.value)
        out.append(row)
    return out


def money(s):
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    m = MONEY.search(str(s).replace(" ", "") if "$" in str(s) else str(s))
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def iso(d):
    """Parse many date styles to YYYY-MM-DD."""
    if d is None or d == "":
        return None
    if isinstance(d, datetime):
        return d.strftime("%Y-%m-%d")
    s = str(d).strip().replace(".", "/")
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%d-%b-%Y", "%d-%b-%y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", s)
    if m:
        mo, da, yr = map(int, m.groups())
        yr = yr + 2000 if yr < 100 else yr
        try:
            return datetime(yr, mo, da).strftime("%Y-%m-%d")
        except ValueError:
            return None
    return None


def cols(line):
    return [c.strip() for c in re.split(r"\s{2,}", line.strip()) if c.strip()]


def clean(s):
    return re.sub(r"\s+", " ", str(s or "")).strip()


def parcel_rec(parcel, owner=None, address=None, min_bid=None, winning=None, buyer=None, excess=None, status=None, years=None, value=None, extra=None):
    r = {"parcel": clean(parcel)}
    if owner: r["owner"] = clean(owner)
    if address: r["address"] = clean(address)
    if min_bid is not None: r["minBid"] = round(min_bid, 2)
    if winning is not None: r["winningBid"] = round(winning, 2)
    if buyer: r["buyer"] = clean(buyer)
    if excess is not None: r["excess"] = round(excess, 2)
    if years: r["years"] = clean(years)
    if value is not None: r["value"] = round(value, 2)
    if status: r["status"] = status
    if extra: r.update(extra)
    return r


def group_by_date(recs, kind, source, label_suffix=""):
    """Turn [(date_iso, parcel_rec)] into auctions grouped by sale date."""
    by = {}
    for d, r in recs:
        if not d:
            continue
        by.setdefault(d, []).append(r)
    return [{"date": d, "kind": kind, "source": source, "note": label_suffix, "parcels": ps} for d, ps in sorted(by.items(), reverse=True)]


def govwin_feed(site):
    """Government Window tax-sale feed (JSON with html + rows of files)."""
    base = site.rstrip("/")
    return get_json("https://www.governmentwindow.com/api/tax/tax_sale", {"Origin": base, "Referer": base + "/tax-sales.html"})


def wp_media(site, search=None, pages=3):
    out = []
    for p in range(1, pages + 1):
        q = f"{site.rstrip('/')}/wp-json/wp/v2/media?per_page=100&page={p}&_fields=date,title,source_url,mime_type"
        if search:
            q += "&search=" + urllib.request.quote(search)
        try:
            items = get_json(q)
        except Exception:
            break
        if not items:
            break
        out.extend(items)
        if len(items) < 100:
            break
    return out
