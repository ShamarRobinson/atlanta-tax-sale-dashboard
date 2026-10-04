"""Exact parcel locations (centroid lat/lon, WGS84) from public county parcel GIS layers.

    locate(slug, parcel_ids, cache) -> {parcel_id: {"lat":.., "lon":.., "city":.., "address":..,
                                                    "ptype":.., "ptypeDetail":.., "improved":..,
                                                    "acres":.., "acresSrc":.., "sqft":.., "yearBuilt":..}}

"ptype" is the property type read from the county layer's land use / digest class fields, one of
PTYPES; "ptypeDetail" is the county's own wording (or the raw class code) and "improved" says
whether the layer shows a building. All three are optional: a county whose layer has no usable
field simply returns none of them (see the p_* functions, one per layer).

"acres" is the lot size: the assessor's deeded / legal acreage where the layer has it, otherwise the
layer's GIS-calculated acreage or the area of the parcel polygon, in which case "acresSrc" is "map".
"sqft" is the heated / living / building area of the main building and "yearBuilt" its year, where
the layer has such fields (see the `ex` argument of S()). All are optional.

`cache` is a dict the caller persists: f"{slug}|{parcel_id}" -> result dict. Entries written by
this version carry "v": 3; a parcel the layer answered for but does not hold is stored as
{"v": 3, "miss": true}. Entries without that marker (older versions, and None for a miss) are re-queried once. Network failures are not cached, so
those parcels are retried on the next run. locate() never raises and only returns hits.

Every source is an open ArcGIS REST layer (no token). Each county has one or more sources, tried
in order; a source says which field holds the parcel number and how the stored ID is rewritten
into the layer's format. Matching happens in two passes:
  1. exact:  field IN (<variants of ~50 ids>)
  2. fuzzy:  field LIKE 'tok1%tok2%...'  for what is still missing
and every returned feature is verified by comparing normalised keys (letters and digits only),
so a loose LIKE can never attach the wrong parcel.

Standard library only.
"""
import json
import math
import re
import urllib.parse

import time
import urllib.request

try:
    from ga_common import UA
except Exception:  # pragma: no cover - keeps the module usable on its own
    UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"

TIMEOUT = 20
TRIES = 2
BATCH = 50          # ids per IN query
LIKE_BATCH = 15     # ids per OR-of-LIKE query
GA = (30.3, 35.1, -85.7, -80.7)  # lat min, lat max, lon min, lon max
CACHE_V = 3         # cache entries without "v": CACHE_V are re-queried once

PTYPES = ("Residential", "Commercial", "Industrial", "Agricultural", "Conservation / Timber",
          "Exempt / Government", "Utility", "Mobile Home", "Vacant Land", "Other")

# Third-party statewide parcel layer (hosted on ArcGIS Online, not a county source); used only
# for counties that publish no open layer of their own. It has no county field, so each use is
# restricted to the county's bounding box.
POOL = "https://services8.arcgis.com/kePfB0a6AiElbP14/arcgis/rest/services/ga_parcel_pool_2_15_2025/FeatureServer/"


# ---------------------------------------------------------------- id helpers
def key(s):
    """Normalised comparison key: upper-case letters and digits only."""
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


def toks(s):
    return [t for t in re.split(r"[^A-Za-z0-9]+", str(s or "").upper()) if t]


def _clean(pid):
    """Stored ids sometimes carry spreadsheet debris ('1130023.0')."""
    s = str(pid or "").strip().upper()
    return re.sub(r"\.0$", "", s)


def v_basic(pid):
    s = _clean(pid)
    t = toks(s)
    return [s, " ".join(t), "".join(t), "-".join(t)]


def v_fulton(pid):
    # '11-0821-0303-169-1' -> '11 082103031691' ; '09F-1505-0078-050-5' -> '09F150500780505'
    t = toks(_clean(pid))
    if len(t) < 2:
        return v_basic(pid)
    rest = "".join(t[1:])
    return [t[0] + " " + rest, t[0] + rest, t[0] + "  " + rest]


def v_gwinnett(pid):
    s = _clean(pid)
    out = [s]
    if not s.startswith("R"):
        out.append("R" + s)
    m = re.match(r"^(R?\d{4}[A-Z]?)\s*(\d{3}[A-Z]?)$", s)
    if m:
        a, b = m.groups()
        if not a.startswith("R"):
            a = "R" + a
        out += [a + " " + b if len(a) == 5 else a + b, a + b, a + " " + b]
    return out


def v_henry(pid):
    # '044-01016000', 'M11-0802000', '006A07016000' : map(3) + '-' or letter, then 8 digits
    s = _clean(pid)
    k = key(s)
    out = [s, k]
    if len(k) == 11:
        out.append(k[:3] + "-" + k[3:])
    return out


def v_fayette(pid):
    # '0550008' -> '0550  008' (district+square, blank lot, parcel); 9-digit numbers match as stored
    k = key(_clean(pid))
    out = v_basic(pid)
    if len(k) == 7:
        out.append(k[:4] + "  " + k[4:])
    return out


def v_troup(pid):
    # layer value has no separators and a 4-digit map: '0602B 004 001' -> '0602B004001'
    k = key(_clean(pid))
    out = [k]
    t = toks(_clean(pid))
    if t and re.fullmatch(r"\d{3}[A-Z]?", t[0]):
        out.append("0" + k)
    return out


def parent_clayton(pid):
    # condo units ('05213 213001 V03') are not drawn; their parent parcel ('05213 213001') is
    t = _clean(pid).split()
    return " ".join(t[:2]) if len(t) == 3 else None


def _wingap_split(pid):
    """Split an undelimited WinGAP number into (map, parcel): '013A043' -> ('013A', '043')."""
    k = key(_clean(pid))
    for rx in (r"^([A-Z]?\d{2,4}[A-Z]?)(\d{3}[A-Z]?)$", r"^(\d{3})(\d{4}[A-Z]?)$"):
        m = re.match(rx, k)
        if m:
            return m.groups()
    return None


def like_tokens(pid):
    t = toks(_clean(pid))
    return ["%".join(t) + "%"] if t else []


def like_wingap(pid):
    out = like_tokens(pid)
    if len(toks(_clean(pid))) == 1:
        sp = _wingap_split(pid)
        if sp:
            out.append(sp[0] + "%" + sp[1] + "%")
    return out


def like_carroll(pid):
    # stored '1280189' / '030-0136' / 'T040060091' / '380033' (leading zero lost)
    # layer  '128    0189' / '128-   -0189' / 'T04-006-0091' / 'T04 0060091'
    k = key(_clean(pid))
    if re.fullmatch(r"\d{6}", k):
        k = "0" + k
    out = []
    if len(k) == 7:
        out.append(k[:3] + "%" + k[3:] + "%")
    elif len(k) == 10:
        out += [k[:3] + "%" + k[3:6] + "%" + k[6:] + "%"]
    else:
        out.append(k + "%")
    return out


def k_carroll(s):
    return key(s).lstrip("0")


def k_troup(s):
    return key(s).lstrip("0")


def like_polk(pid):
    # stored '008 072F' ; layer '008-072F' or '008-031-'
    t = toks(_clean(pid))
    return ["%".join(t) + "%"] if t else []


def k_gwinnett(s):
    k = key(s)
    return k[1:] if k.startswith("R") else k


def k_nozero(s):
    """Key that ignores zero padding inside tokens ('C0530-053-A00' == 'C0530-053-A')."""
    out = []
    for t in toks(s):
        t2 = t.lstrip("0") or "0"
        if re.fullmatch(r"[A-Z]0+", t):
            t2 = t[0]
        out.append(t2)
    while out and out[-1] in ("0", "000"):
        out.pop()
    return "|".join(out)


# ---------------------------------------------------------------- property type
# Georgia tax digest class letters (first character of codes such as R3, C4, V5).
DIGEST = {"R": "Residential", "C": "Commercial", "I": "Industrial", "A": "Agricultural",
          "P": "Agricultural", "V": "Conservation / Timber", "F": "Conservation / Timber",
          "J": "Conservation / Timber", "W": "Conservation / Timber", "Q": "Conservation / Timber",
          "E": "Exempt / Government", "U": "Utility", "H": "Residential", "T": "Residential",
          "B": "Other"}
DIGEST_WORD = {"R": "Residential", "C": "Commercial", "I": "Industrial", "A": "Agricultural",
               "P": "Preferential agricultural", "V": "Conservation use", "F": "Forest land (FLPA)",
               "J": "Forest land (FLPA)", "W": "Timberland", "Q": "Qualified timberland",
               "E": "Exempt", "U": "Utility", "H": "Historic", "T": "Residential transitional",
               "B": "Brownfield"}
# exemption sub-classes, as named by the Gwinnett layer's coded-value domain
EXEMPT = {"E0": "non-profit aged home", "E1": "public property", "E2": "religious property",
          "E3": "charitable property", "E4": "religious burial", "E5": "non-profit hospital",
          "E6": "educational institution", "E7": "pollution equipment", "E8": "farm property",
          "E9": "other"}
BUILT = ("Residential", "Commercial", "Industrial")   # classes that become "Vacant Land" when unimproved

# Land use codes of the Tyler iasWorld CAMA system used by Fulton and Clayton. Clayton's layer
# publishes the description next to every code (LANDUSEC / LANDUSED); Fulton's layer has the code
# only (LUCode), so this table, read from Clayton's layer, decodes it. Codes missing here fall
# back to the digest class.
LUC = {
    '100': 'Residential vacant', '101': 'Residential 1 family', '102': 'Residential 2 family',
    '103': 'Residential 3 family', '104': 'Residential 4 family', '105': 'Residential 5 family',
    '106': 'Single Family Residential Condominium', '107': 'Single Family Residential Townhouse',
    '109': 'Auxiliary Improvement', '110': 'Single Family RE Condo Loft', '111': 'Homeowner Association Property',
    '112': 'CUVA/Preferential Ag-Improved', '113': 'CUVA/Preferential Ag-Vacant',
    '114': 'Residential Land w/Leasehold', '166': 'Condominium Common Element Property',
    '188': 'Homeowner Association Common Area', '200': 'Apartment Vacant Land',
    '201': 'Residential on Apartment Land', '208': 'CO-OPS (Multi-Family)', '210': 'Small Apartment',
    '212': 'Dormitories', '213': 'Mobile Home Park', '252': 'First Class Hotel', '253': 'Mid-Rise Hotel',
    '254': 'Luxury Budget Motel', '255': 'Economy Motel', '256': 'Micro-Budget Motel',
    '289': 'Low Income Housing Tax Credit Property', '2A1': 'Garden Apartment (1-3) Class A',
    '2B1': 'Garden Apartment (1-3) Class B', '2C1': 'Garden Apartment (1-3) Class C',
    '2D1': 'Garden Apartment (1-3) Class D', '300': 'Vacant Commercial Land',
    '301': 'Residential on Commercial Land', '309': 'Miscellaneous Amusement',
    '310': 'Unsound Commercial Structure', '311': 'Detention / Retention Pond',
    '312': 'Assisted Living Residence Communities', '313': 'Commercial Land w/Leasehold',
    '314': 'Income Producing Parking Lot', '318': 'Boarding-Rooming House', '319': 'Mixed Res/Comm(Built as Comm)',
    '320': 'Commercial Auxiliary Improvements', '321': 'Restaurant', '325': 'Fast Food Restaurant',
    '326': 'Convenience / Fast Food Market', '327': 'Bar/Lounge', '330': 'Used Car Lot',
    '331': 'Auto Dealer, Full Service', '332': 'Auto Service Garage', '333': 'Service Station with Bays',
    '335': 'Truck Stop', '336': 'Car Wash - Manual', '337': 'Car Wash - Automatic', '338': 'Parking Garage/Deck',
    '339': 'Parking Lot Paved', '341': 'Regional Shopping Mall', '342': 'Community Shopping Center',
    '344': 'Strip Shopping Center', '345': 'Discount Department Store', '346': 'Department Store',
    '347': 'Supermarket', '348': 'Convenience Food Market', '349': 'Medical Office Building',
    '350': 'Telecommunication Office Building', '351': 'Bank', '354': 'Office Single-Occupancy',
    '355': 'Office Condominium', '356': 'Retail Condominium', '361': 'Funeral Home', '362': 'Veterinary Clinic',
    '364': 'Motion Picture Theater', '366': 'Radio, TV or Motion Picture Studio', '367': 'Social/Fraternal Hall',
    '368': 'Hanger', '369': 'Day Care Center', '370': 'Greenhouse/Florist', '372': 'Retail - Single Occupancy',
    '373': 'Retail Multi-Occupancy', '375': 'Bowling Alley', '376': 'Skating Rink', '383': 'Health Spa',
    '388': 'Club House (Swim, Tennis, etc.)', '389': 'Country Club with Golf Course', '390': 'Warehouse Service',
    '391': 'Cold Storage Facility', '393': 'Office Flex Building', '394': 'Warehouse Distribution',
    '395': 'Truck Terminal', '396': 'Mini Warehouse', '397': 'Warehouse Office', '398': 'Warehouse Bulk',
    '3A3': 'Office Building (Low-Rise 3>) Class A', '3A4': 'Office Building (High-Rise 4<) Class A',
    '3B3': 'Office Building (Low-Rise 3>) Class B', '3B4': 'Office Building (High-Rise 4<) Class B',
    '3C3': 'Office Building (Low-Rise 3>) Class C', '3D3': 'Office Building (Low-Rise 3>) Class D',
    '400': 'Vacant Industrial Land', '401': 'Manufacturing/Processing', '405': 'Research and Development',
    '406': 'Industrial Auxiliary Improvement', '407': 'Industrial Land w/Leasehold', '413': 'Asphalt Plant',
    '421': 'Chemical Plant', '433': 'Food Processing', '451': 'Paint Manufacturing', '459': 'Quarries (Rock)',
    '500': 'Salvage / Junk Yard', '600': 'Vacant Exempt Land', '601': 'Cemetery', '609': 'Community Center',
    '610': 'Recreational/Health', '611': 'Library', '612': 'School', '613': 'College',
    '614': 'Single Family Res (Institutional)', '618': 'Historic Building',
    '620': 'Religious (Church, Synagogue, Mosque)', '621': 'Church Parking (Paved)',
    '622': 'Single Family Residential (Parsonage)', '625': 'Religious Mission (Goodwill,Salv Army)',
    '630': 'Auditorium', '640': 'Hospital', '650': 'Charitable Office (Service Center)',
    '660': 'Police or Fire Station', '670': 'Correctional', '680': 'Cultural Facilities',
    '690': 'Rail/Bus/Air Terminal', '691': 'US Postal Services (Private)', '692': 'US Postal Services (Exempt)',
    '699': 'Improved Government Owned Exempt NEC', '701': 'Railroad ROW, Switch Yard or Maintenance',
    '702': 'Electric Utility', '703': 'Gas Utility', '704': 'Water Utility', '711': 'Telephone Utility NEC',
    '799': 'Other Utility NEC', '888': 'Tax Incentive (Commercial / Industrial)',
}

LUC_OTHER = {"111", "166", "188", "311"}        # HOA / condo common area, detention pond
LUC_RANGE = {"1": "Residential", "2": "Commercial", "3": "Commercial", "4": "Industrial",
             "5": "Industrial", "6": "Exempt / Government", "7": "Utility"}


def _num(v):
    """Number from a layer value ('203600', 870.0, '', None) or None."""
    try:
        if v is None or isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            return float(v)
        t = str(v).replace(",", "").replace("$", "").strip()
        return float(t) if t else None
    except Exception:
        return None


def _txt(v):
    return re.sub(r"\s+", " ", str(v)).strip() if v is not None else ""


def _short(s, n=40):
    s = _txt(s)
    return s if len(s) <= n else s[:n - 1].rstrip(" ,/(-") + "…"


def _nice(s):
    """County descriptions are often upper case ('SF RESIDENTIAL'); make them readable."""
    s = re.sub(r"\s+,", ",", _txt(s))
    if s and s == s.upper():
        s = s.title()
        s = re.sub(r"\b(Sf|Sfr|Hoa|Row|Cuva|Nec|Us|Ii|Llc)\b", lambda m: m.group(1).upper(), s)
        s = re.sub(r"\b(Ac|Acs|Acl)\b", lambda m: m.group(1).lower(), s)
    return s


def _pt(ptype, detail=None, improved=None):
    r = {}
    if ptype in PTYPES:
        r["ptype"] = ptype
        if detail:
            r["ptypeDetail"] = _short(detail)
    if improved is not None:
        r["improved"] = bool(improved)
    return r or None


def _legal_hint(desc):
    """WinGAP legal descriptions often open with 'HSE/LOT ..' or 'V/LOT ..', 'VAC/..'."""
    d = _txt(desc).upper()
    if re.match(r"(V/|VAC\b|VACANT\b|V\s?\d|V LOT|VL/)", d):
        return False
    if re.match(r"(HSE\b|H/L\b|H&L\b|HOUSE\b|HS/|DUPLEX\b|BLDG\b|MH/|M/H\b|DWMH\b|SWMH\b)", d):
        return True
    return None


def _digest(code, improved=None, desc=None):
    """Property type from a Georgia digest class code ('R3', 'C', 'V5') and what is known about
    improvements. `desc` is the county's own description of the class or use, when it has one."""
    c = key(code)
    if not c or c[0] not in DIGEST:
        return _pt(None, None, improved)
    L, pt = c[0], DIGEST[c[0]]
    word = DIGEST_WORD[L]
    if L == "E" and c[:2] in EXEMPT:
        word = "Exempt: " + EXEMPT[c[:2]]
    d = _txt(desc)
    vac_desc = bool(re.search(r"\b(vacant|undeveloped)\b", d, re.I))
    if pt in BUILT and (improved is False or (vac_desc and not improved)):
        if vac_desc:
            return _pt("Vacant Land", "%s (%s)" % (d, c) if len(d) + len(c) < 37 else d, False)
        return _pt("Vacant Land", "Vacant %s %s (%s)" % (word.lower(), "lot" if L in "RHT" else "land", c),
                   False)
    if d and len(d) + len(c) < 37:
        return _pt(pt, "%s (%s)" % (d, c), improved)
    return _pt(pt, d or "%s, class %s" % (word, c), improved)


def _luc(code, desc=None, cls=None, impr=None):
    """Fulton / Clayton land use code -> property type. `impr` is the improvement value or None."""
    c = _txt(code).upper()
    d = _txt(desc) or LUC.get(c, "")
    improved = None if impr is None else impr > 0
    L = key(cls)[:1]
    if not c or not d:
        # code we cannot decode: the digest class decides
        r = _digest(cls, improved)
        if r and r.get("ptype") and c and r["ptype"] != "Vacant Land":
            r["ptypeDetail"] = _short("%s, land use code %s" % (r["ptypeDetail"].split(",")[0], c))
        return r
    vac = bool(re.search(r"\bvacant\b", d, re.I))
    if vac and improved is None:
        improved = False
    if c in LUC_OTHER:
        pt = "Other"
    elif c in ("112", "113"):       # CUVA / preferential agriculture
        pt = DIGEST.get(L) if L in "VPAJFWQ" and L else "Agricultural"
    else:
        pt = LUC_RANGE.get(c[0]) or DIGEST.get(L)
    if L in ("E", "U") and pt in BUILT:
        pt = DIGEST[L]              # exempt / utility digest class overrides the use
        d = "%s (class %s)" % (d, key(cls))
    if c == "213":
        d = "Mobile Home Park"
    if pt in BUILT and improved is False:
        return _pt("Vacant Land", d if vac else "Vacant %s %s (LUC %s)" % (
            pt.lower(), "lot" if pt == "Residential" else "land", c), False)
    return _pt(pt, d, improved)


def p_fulton(a):
    return _luc(a.get("LUCode"), None, a.get("ClassCode"), _num(a.get("ImprAppr")) if "ImprAppr" in a else None)


def p_clayton(a):
    impr = _num(a.get("IMPROVEMNT"))
    if impr is None:
        impr = _num(a.get("BLDGSFMV"))
    r = _luc(a.get("LANDUSEC"), a.get("LANDUSED"), None, impr)
    if r and "improved" not in r and (_num(a.get("YEARBUILT")) or _txt(a.get("STRUCTYPE"))):
        r["improved"] = True
    return r


def p_dekalb(a):
    # CLASSDSCRP holds the digest class ('R3'); the layer carries no building or improvement value
    return _digest(a.get("CLASSDSCRP") or a.get("CLASSCD"))


def p_cobb(a):
    tot, b = _num(a.get("FMV_TOTAL")), _num(a.get("FMV_BLDG"))
    return _digest(a.get("CLASS"), None if tot is None and b is None else (b or 0) > 0)


def p_fayette(a):
    # class and use fields are almost always empty; the residential structure fields are filled
    area, yr = _num(a.get("RESFLRAREA")) or 0, _num(a.get("RESYRBLT")) or 0
    land, tot = _num(a.get("LNDVALUE")), _num(a.get("CNTASSDVAL"))
    cls = _txt(a.get("CLASSCD")) or _txt(a.get("CLASSDSCRP")).split(" ")[0]
    cls = cls if re.fullmatch(r"[A-Z]\d?", cls.upper()) else ""
    if area > 0 or yr > 0:
        d = "Dwelling" + (", built %d" % yr if yr > 1700 else "") + (", %s sf" % format(int(area), ",") if area else "")
        return _pt("Residential", d, True)
    improved = None if land is None or tot is None else tot > land
    r = _digest(cls, improved)
    if r and r.get("ptype"):
        return r
    if improved is False:
        return _pt("Vacant Land", "Land only, no building value", False)
    return _pt(None, None, improved)


def p_forsyth(a):
    area, land, tot = _num(a.get("BLDGAREA")) or 0, _num(a.get("LNDVALUE")), _num(a.get("CNTASSDVAL"))
    improved = True if area > 0 else (None if land is None or tot is None else tot > land)
    use = _nice(a.get("USEDSCRP"))
    r = _digest(a.get("CLASSCD"), improved, use or _nice(a.get("CLASSDSCRP")))
    if r and r.get("ptype") == "Vacant Land" and use and not re.search(r"undeveloped|vacant", use, re.I):
        r["ptypeDetail"] = _short("%s, no building" % use)
    return r


def p_class_legal(cls, legal):
    def f(a):
        return _digest(a.get(cls), _legal_hint(a.get(legal)))
    return f


def p_troup(a):
    yr, area = _num(a.get("yearbuilt")) or 0, _num(a.get("area_build")) or 0
    c = _txt(a.get("usecode"))
    improved = True if (yr > 0 or area > 0) else (False if key(c)[:1] in ("R", "H", "T") else None)
    return _digest(c, improved)


def p_wingap(cls, vals):
    """WinGAP export: digest class plus the fair market value of residential, commercial and
    accessory improvements."""
    def f(a):
        v = [_num(a.get(x)) for x in vals]
        improved = None if any(x is None for x in v) else sum(v) > 0
        return _digest(a.get(cls), improved)
    return f


def p_dawson(a):
    tot, land = _num(a.get("CURR_VAL")), _num(a.get("Land_Val"))
    return _digest(a.get("Class"), None if tot is None or land is None else tot > land)


def p_floyd(a):
    return _digest(a.get("DIGCLASS"), None if _num(a.get("IMP_VAL")) is None else _num(a.get("IMP_VAL")) > 0)


def p_floyd_old(a):
    return _digest(a.get("DIGCLASS"))


def p_coweta(a):
    # no class field. A year built / heated area means a dwelling (WinGAP residential improvement);
    # an empty improvement value does NOT mean vacant (commercial and exempt buildings are empty too)
    yr, sf = _num(a.get("YearBuilt")) or 0, _num(a.get("HeatedSquareFeet")) or 0
    if yr > 0 or sf > 0:
        d = "Dwelling" + (", built %d" % yr if yr > 1700 else "") + (", %s sf" % format(int(sf), ",") if sf else "")
        return _pt("Residential", d, True)
    if (_num(a.get("ImprovementValue")) or 0) > 0:
        return _pt(None, None, True)
    if _legal_hint(a.get("LegalDescription")) is False:
        return _pt("Vacant Land", "Vacant (per legal description)", False)
    return None


def _zoning(z):
    z = _txt(z).upper()
    z = re.sub(r"^SRC-", "", z)
    if not z:
        return None
    if re.match(r"(R[\d\-A-Z]*$|AR$|RA$|RS|RM|RG|RD|MH$|RMH$|TH$)", z) and z not in ("ROW",):
        return "Residential"
    if re.match(r"(C\d?$|C-\d$|OI$|NC$|BG|BN$|HSB$|GC$|CC$)", z):
        return "Commercial"
    if re.match(r"(M\d?$|M-\d$|LI$|HI$|ID$)", z):
        return "Industrial"
    return None


def p_henry(a):
    # LAND_USE_CODE is the zoning district; the assessor's class is not in the layer
    z = _txt(a.get("LAND_USE_CODE"))
    yr = _num(a.get("BldgActualYearBuilt")) or 0
    pt = _zoning(z)
    return _pt(pt, "Zoning %s (%s)" % (z, pt.lower()) if pt else None, True if yr > 0 else None)


def p_rockdale(a):
    # zoning district plus year built ('0' when the assessor has no dwelling on the parcel)
    z = _txt(a.get("County_Zon")) or _txt(a.get("City_Zonin"))
    pt = _zoning(z)
    yr = _num(a.get("Yr_Built"))
    improved = True if (yr or 0) > 0 else (False if yr == 0 and pt == "Residential" else None)
    if pt == "Residential" and improved is False:
        return _pt("Vacant Land", "Vacant residential lot (zoning %s)" % z, False)
    return _pt(pt, "Zoning %s (%s)" % (z, pt.lower()) if pt else None, improved)


def p_gwinnett(a):
    # only the parcel type and exemption type are published; ordinary taxable parcels have no class
    ex, t = _txt(a.get("EXEMPTION_TYPE")).upper(), _txt(a.get("PARCELTYPE")).upper()
    if ex in EXEMPT:
        return _pt("Exempt / Government", "Exempt: " + EXEMPT[ex])
    if ex == "PU" or t == "PU":
        return _pt("Utility", "Public utility")
    if ex == "SV" or t == "CV":
        return _pt("Conservation / Timber", "Conservation use")
    if ex in ("SH", "ST"):
        return _pt("Residential", "Historic" if ex == "SH" else "Residential transitional")
    if t == "R/W":
        return _pt("Other", "Right of way")
    if t == "R":
        return _pt("Other", "Recreation area")
    return None


# Statewide third-party layer: 4-digit standardized land use codes (the vendor's own scheme). Only
# the leading digit and a few codes whose meaning the records make plain are used.
POOL_USE = {"1001": "Single family residential", "1004": "Condominium", "1006": "Mobile / manufactured home",
            "8001": "Vacant residential lot", "8002": "Vacant commercial land", "8003": "Vacant industrial land"}
POOL_RANGE = {"1": "Residential", "2": "Commercial", "3": "Commercial", "5": "Industrial", "6": "Utility",
              "7": "Agricultural", "8": "Vacant Land", "9": "Exempt / Government"}


def p_pool(a):
    c = _txt(a.get("standardized_land_use"))
    impr = _num(a.get("market_value_improvement"))
    if impr is None:
        impr = _num(a.get("assessed_improvement"))
    improved = None if impr is None else impr > 0
    pt = POOL_RANGE.get(c[:1]) if re.fullmatch(r"\d{4}", c) else None
    if not pt:
        return _pt(None, None, improved)
    if c == "1006":
        return _pt("Mobile Home", POOL_USE[c], improved)
    if pt == "Vacant Land":
        return _pt(pt, POOL_USE.get(c, "Vacant land (use code %s)" % c), False if improved is None else improved)
    if pt in BUILT and improved is False:
        return _pt("Vacant Land", "Vacant %s %s (use code %s)" % (pt.lower(), "lot" if pt == "Residential" else "land", c),
                   False)
    return _pt(pt, POOL_USE.get(c, "%s (use code %s)" % (pt.split(" /")[0], c)), improved)



# ---------------------------------------------------------------- sources
def S(url, field, variants=v_basic, like=like_tokens, addr=None, city=None, keyf=key, bbox=None, name=None,
      parent=None, ptype=None, pf=None, ex=None):
    """`ptype` maps a feature's attributes to {"ptype", "ptypeDetail", "improved"} (or None);
    `pf` lists the fields it reads. `ex` names the lot size / building area / year built fields
    (see _extras)."""
    return {"url": url, "field": field, "variants": variants, "like": like, "addr": addr, "city": city,
            "keyf": keyf, "bbox": bbox, "name": name or url, "parent": parent, "ptype": ptype,
            "pf": pf.split(",") if isinstance(pf, str) else list(pf or []), "ex": ex or {}}


def pool(bbox, like=like_tokens, keyf=key):
    return [S(POOL + str(i), "apn", like=like, addr="prop_address", city="city", keyf=keyf, bbox=bbox,
              ptype=p_pool, pf="standardized_land_use,market_value_improvement,assessed_improvement", ex={"acres": [("acres", "map")]},
              name="statewide pool (third party) " + POOL + str(i)) for i in (2, 1, 0)]


SOURCES = {
    "fulton": [S("https://gismaps.fultoncountyga.gov/arcgispub2/rest/services/PropertyMapViewer/PropertyMapViewer/MapServer/11",
                 "ParcelID", v_fulton, addr="Address", ptype=p_fulton, pf="LUCode,ClassCode,ImprAppr", ex={"acres": ["LandAcres"]}),
               S("https://services1.arcgis.com/AQDHTHDrZzfsFsB5/arcgis/rest/services/Tax_Parcels/FeatureServer/0",
                 "ParcelID", v_fulton, addr="Address", ptype=p_fulton, pf="LUCode,ClassCode", ex={"acres": ["LandAcres"]})],
    "dekalb": [S("https://dcgis.dekalbcountyga.gov/hosted/rest/services/PropertyAppraisal/Parcels_IASWorld/MapServer/0",
                 "PARCELID", addr="SITEADDRESS", city="CITY", ptype=p_dekalb, pf="CLASSCD,CLASSDSCRP", ex={"acres": ["ACREAGE", ("STATEDAREA", "sqft")], "sqft": ["RESFLRAREA", "BLDGAREA"],
                   "year": ["RESYRBLT"]})],
    "cobb": [S("https://gis.cobbcounty.gov/gisserver/rest/services/tax/taxassessorsdaily/MapServer/0",
               "PIN", addr="SITUS_ADDR", ptype=p_cobb, pf="CLASS,FMV_BLDG,FMV_TOTAL", ex={"acres": ["ACRE_DEEDED", "ACRES", ("LAND_SQFT", "sqft")]})],
    "clayton": [S("https://gis.claytoncountyga.gov/server/rest/services/TaxAssessor/Parcels/MapServer/0",
                  "PARCELID", addr="SITEADDRES", city="SITECITY", parent=parent_clayton, ptype=p_clayton,
                  pf="LANDUSEC,LANDUSED,IMPROVEMNT,BLDGSFMV,YEARBUILT,STRUCTYPE", ex={"acres": ["ACERAGE"], "sqft": ["SQRFT"], "year": ["YEARBUILT"]})],
    "fayette": [S("https://gis.fayettecountyga.gov/arcgis/rest/services/Pictometry/parcelsRO/MapServer/0", "PARCEL_NO", v_fayette, ex={"acres": ["acres"]}),
                S("https://services5.arcgis.com/Hg5aLg4LtSINzVWa/arcgis/rest/services/TaxParcels_public/FeatureServer/0",
                  "PARCELID", v_fayette, addr="SITEADDRESS", ptype=p_fayette,
                  pf="CLASSCD,CLASSDSCRP,RESFLRAREA,RESYRBLT,LNDVALUE,CNTASSDVAL", ex={"sqft": ["RESFLRAREA", "BLDGAREA"], "year": ["RESYRBLT"]})],
    "douglas": [S("https://maps.douglascountyga.gov/arcgis/rest/services/TylerTech/LandRecords/MapServer/0",
                  "PIN", addr="ADDRESS", ptype=p_class_legal("DIGCLASS", "LEGAL_DESC"), pf="DIGCLASS,LEGAL_DESC",
                  ex={"acres": ["TOTAL_ACRES", ("ACREAGE_GIS", "map")]})],
    "troup": [S("https://services6.arcgis.com/WjqAE1SlQxuk7dsk/arcgis/rest/services/Troup_County_GA_Parcel_Feature_Layer/FeatureServer/0",
                "parcelnu_1", v_troup, addr="address", city="scity", keyf=k_troup, ptype=p_troup,
                pf="usecode,yearbuilt,area_build", ex={"acres": ["deeded_acr", ("gisacre", "map"), ("ll_gisacre", "map")], "sqft": ["area_build"],
                    "year": ["yearbuilt", "year_built"]})],
    "meriwether": [S("https://services9.arcgis.com/Xv8vRekQ4FVHSSIe/arcgis/rest/services/MeriwetherParcels/FeatureServer/0",
                     "Parcel_No", like=like_wingap, addr=["house_no", "stdirect", "street_nam", "sttype"],
                     ptype=p_wingap("digclass", ["fmvres", "fmvcom", "fmvacc"]), pf="digclass,fmvres,fmvcom,fmvacc",
                 ex={"acres": ["totalacres", "Deed_Acres", ("Draw_Acres", "map")]})],
    "spalding": [S("https://services5.arcgis.com/IBG8fFojdkoiHAvQ/arcgis/rest/services/Parcels_Public_View/FeatureServer/1",
                   "PARCEL_ID", like=like_wingap)],
    "lumpkin": [S("https://services6.arcgis.com/BAJNi3EgCdtQ1BCG/arcgis/rest/services/Lumpkin_2025Parcels/FeatureServer/0",
                  "PARCEL_NO", like=like_wingap, addr=["HOUSE_NO", "STREET_NAM"],
                  ptype=p_class_legal("DIGCLASS", "LEGAL_DESC"), pf="DIGCLASS,LEGAL_DESC",
                  ex={"acres": ["TOTALACRES", ("GIS_ACRES", "map")]})],
    "gwinnett": [S("https://services3.arcgis.com/RfpmnkSAQleRbndX/arcgis/rest/services/Property_and_Tax/FeatureServer/0",
                   "TAXPIN", v_gwinnett, keyf=k_gwinnett, ptype=p_gwinnett, pf="PARCELTYPE,EXEMPTION_TYPE", ex={"acres": ["DEEDEDACREAGE", ("CALCULATEDACREAGE", "map")]})],
    "henry": [S("https://services1.arcgis.com/W8nsWsU3ZKxVPuk7/arcgis/rest/services/HenryCountyParcels/FeatureServer/0",
                "PARCEL_ID", v_henry, addr="STREET_ADDRESS", ptype=p_henry, pf="LAND_USE_CODE,BldgActualYearBuilt", ex={"acres": ["ACRES"], "year": ["BldgActualYearBuilt"]})],
    "rockdale": [S("https://services.arcgis.com/Tbke9ca9DhtF4VIx/ArcGIS/rest/services/Parcel_Polygons_working/FeatureServer/0",
                   "PARCEL_NO", addr=["Address", "Road_name", "St_Type"], ptype=p_rockdale,
                   pf="County_Zon,City_Zonin,Yr_Built", ex={"acres": [("Acreage", "map")], "year": ["Yr_Built"]})],
    "coweta": [S("https://services1.arcgis.com/AaPyNbrJpNGryRqh/arcgis/rest/services/WeeklyUpdate_WinGapParcels/FeatureServer/0",
                 "ParcelNumber", like=like_wingap, addr=["HouseNumber", "StreetDirection", "StreetName", "StreetType"],
                 ptype=p_coweta, pf="YearBuilt,HeatedSquareFeet,ImprovementValue,LegalDescription", ex={"acres": ["TotalAcres"], "sqft": ["HeatedSquareFeet"], "year": ["YearBuilt"]})],
    "forsyth": [S("https://geo.forsythco.com/gis/rest/services/Public/Tax_Parcel/FeatureServer/0",
                  "PARCELID", addr="SITEADDRESS", ptype=p_forsyth,
                  pf="CLASSCD,CLASSDSCRP,USEDSCRP,BLDGAREA,LNDVALUE,CNTASSDVAL", ex={"acres": ["STATEDAREA"], "sqft": ["BLDGAREA", "RESFLRAREA"], "year": ["RESYRBLT"]})],
    "clarke": [S("https://services2.arcgis.com/xSEULKvB31odt3XQ/arcgis/rest/services/Parcel/FeatureServer/0",
                 "PARCEL_NO", addr="PAR_ADD", ex={"acres": ["ACRES"]})],
    "dawson": [S("https://services7.arcgis.com/Ptz860OPLeIY55cX/ArcGIS/rest/services/Energov_Layers_Update2021/FeatureServer/3",
                 "PARCELID"),
               S("https://services.arcgis.com/ISpzx3B5ZsVA6e1Z/ArcGIS/rest/services/Dawson_Webmap_WFL1/FeatureServer/1",
                 "Parcel_No", like=like_wingap, ptype=p_dawson, pf="Class,CURR_VAL,Land_Val", ex={"acres": ["Acres", ("Calc_Acres", "map")]})],
    "floyd": [S("https://services2.arcgis.com/nV67H1IJR8GS6SAA/ArcGIS/rest/services/CurrentParcels/FeatureServer/0",
                "Parcel_No", addr="LOCATION", ptype=p_floyd, pf="DIGCLASS,IMP_VAL", ex={"acres": ["TOTALACRES", ("Calc_Ac", "map")]}),
              S("https://services2.arcgis.com/nV67H1IJR8GS6SAA/ArcGIS/rest/services/Current_Parcels/FeatureServer/5",
                "PARCEL", addr="PROP_ADDR", ptype=p_floyd_old, pf="DIGCLASS", ex={"acres": ["TOTALACRES"]})],
    "jackson": [S("https://services8.arcgis.com/bcbi4lYRFOsss0F5/ArcGIS/rest/services/jackson_baselayers/FeatureServer/9",
                  "PARCEL_NO", like=like_wingap, addr=["HOUSE_NO", "STREET_NAM"],
                  ptype=p_wingap("DIGCLASS", ["FMVRES", "FMVCOM", "FMVACC"]), pf="DIGCLASS,FMVRES,FMVCOM,FMVACC", ex={"acres": ["TOTALACRES", ("acre_calc", "map")]})],
    # no open county layer found: statewide third-party layer, limited to the county bounding box
    # Walton: the Social Circle layer covers only the south-east corner of the county
    "walton": [S("https://services8.arcgis.com/LtjAOW49t0cQdmjH/arcgis/rest/services/Walton_Parcels/FeatureServer/16",
                 "Parcel_No", addr=["house_no", "stdirect", "street_nam", "sttype"], keyf=k_nozero,
                 ptype=p_wingap("digclass", ["fmvres", "fmvcom", "fmvacc"]), pf="digclass,fmvres,fmvcom,fmvacc",
                 ex={"acres": ["totalacres", "Deed_Acres", ("Draw_Acres", "map")]})]
              + pool((-83.95, 33.6, -83.45, 34.0), keyf=k_nozero),
    "carroll": pool((-85.36, 33.38, -84.78, 33.82), like=like_carroll, keyf=k_carroll),
    "heard": pool((-85.32, 33.12, -84.83, 33.47)),
    "pickens": pool((-84.68, 34.36, -84.23, 34.6)),
    "polk": pool((-85.44, 33.85, -84.95, 34.12), like=like_polk),
}


def endpoint(slug):
    """Primary endpoint used for a county (for reports)."""
    src = SOURCES.get(slug)
    return src[0]["name"] if src else None


# ---------------------------------------------------------------- geometry
def centroid(geom):
    """Area-weighted centroid of an Esri polygon (rings in lon/lat). Returns (lat, lon) or None."""
    try:
        if not geom:
            return None
        if "x" in geom and "y" in geom:
            return float(geom["y"]), float(geom["x"])
        rings = geom.get("rings") or []
        A = cx = cy = 0.0
        n = sx = sy = 0
        for ring in rings:
            for i in range(len(ring) - 1):
                x0, y0 = ring[i][0], ring[i][1]
                x1, y1 = ring[i + 1][0], ring[i + 1][1]
                c = x0 * y1 - x1 * y0
                A += c
                cx += (x0 + x1) * c
                cy += (y0 + y1) * c
            for p in ring:
                sx += p[0]
                sy += p[1]
                n += 1
        if abs(A) > 1e-14:
            return cy / (3.0 * A), cx / (3.0 * A)
        if n:
            return sy / n, sx / n
    except Exception:
        pass
    return None


def area_acres(geom):
    """Area of an Esri polygon given in lon/lat (WGS84), in acres. Rings are projected onto a local
    plane around their own middle, which is exact to well under 0.1 % at parcel size; holes are
    wound the other way and subtract themselves."""
    try:
        rings = (geom or {}).get("rings") or []
        pts = [p for ring in rings for p in ring]
        if len(pts) < 3:
            return None
        lat0 = sum(p[1] for p in pts) / len(pts)
        lon0 = sum(p[0] for p in pts) / len(pts)
        ky = math.pi / 180.0 * 6371008.8
        kx = ky * math.cos(math.radians(lat0))
        A = 0.0
        for ring in rings:
            for i in range(len(ring) - 1):
                x0, y0 = (ring[i][0] - lon0) * kx, (ring[i][1] - lat0) * ky
                x1, y1 = (ring[i + 1][0] - lon0) * kx, (ring[i + 1][1] - lat0) * ky
                A += x0 * y1 - x1 * y0
        return abs(A) / 2.0 / 4046.8564224
    except Exception:
        return None


def _extras(src, a, geom):
    """Lot size, building area and year built from a feature, per the source's `ex` spec:
    {"acres": [field | (field, "map") | (field, "sqft")], "sqft": [fields], "year": [fields]}.
    Acreage fields are tried in order; "map" marks a GIS-calculated field, "sqft" one in square feet."""
    r = {}
    ex = src.get("ex") or {}
    for spec in ex.get("acres") or []:
        f, kind = (spec, "") if isinstance(spec, str) else spec
        v = _num(a.get(f))
        if v is not None and kind == "sqft":
            v = v / 43560.0
        if v is not None and 0 < round(v, 3) and v < 20000:
            r["acres"] = round(v, 3)
            if kind == "map":
                r["acresSrc"] = "map"
            break
    if "acres" not in r:
        v = area_acres(geom)
        if v is not None and 0 < round(v, 3) and v < 20000:
            r["acres"], r["acresSrc"] = round(v, 3), "map"
    for f in ex.get("sqft") or []:
        v = _num(a.get(f))
        if v is not None and 0 < v < 5e6 and int(round(v)) > 0:
            r["sqft"] = int(round(v))
            break
    for f in ex.get("year") or []:
        v = _num(a.get(f))
        if v is not None and v == int(v) and 1700 <= v <= time.localtime().tm_year:
            r["yearBuilt"] = int(v)
            break
    return r


def _in_ga(lat, lon):
    return GA[0] <= lat <= GA[1] and GA[2] <= lon <= GA[3]


# ---------------------------------------------------------------- querying
def _q(s):
    return "'" + str(s).replace("'", "''") + "'"


def _attr(attrs, spec):
    if not spec:
        return None
    if isinstance(spec, str):
        spec = [spec]
    parts = []
    for f in spec:
        v = attrs.get(f)
        if v is None:
            continue
        v = re.sub(r"\s+", " ", str(v)).strip()
        if v and v.upper() not in ("NULL", "NONE", "0"):
            parts.append(v)
    s = " ".join(parts).strip()
    return s or None


def _query(src, where):
    """Run one query. Returns list of features, or None on failure."""
    p = {"where": where, "outFields": "*", "returnGeometry": "true", "outSR": "4326",
         "geometryPrecision": "6", "f": "json"}
    fields = [src["field"]]
    for spec in (src["addr"], src["city"]):
        if spec:
            fields += [spec] if isinstance(spec, str) else list(spec)
    fields += src.get("pf") or []
    for k, specs in (src.get("ex") or {}).items():
        fields += [x if isinstance(x, str) else x[0] for x in specs]
    p["outFields"] = ",".join(dict.fromkeys(fields))
    if src["bbox"]:
        p.update({"geometry": "%s,%s,%s,%s" % src["bbox"], "geometryType": "esriGeometryEnvelope",
                  "inSR": "4326", "spatialRel": "esriSpatialRelIntersects"})
    body = urllib.parse.urlencode(p)
    url = src["url"] + "/query"
    for i in range(TRIES):
        try:
            # POST keeps long IN lists out of the URL (ArcGIS Online answers 404 to long GETs);
            # the second try falls back to GET when the request is short enough.
            if i and len(body) < 1800:
                req = urllib.request.Request(url + "?" + body, headers={"User-Agent": UA, "Accept": "*/*"})
            else:
                req = urllib.request.Request(url, data=body.encode(), headers={
                    "User-Agent": UA, "Accept": "*/*", "Content-Type": "application/x-www-form-urlencoded"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                j = json.loads(r.read().decode("utf-8", "replace"))
            if isinstance(j, dict) and "error" not in j:
                return j.get("features") or []
        except Exception:
            pass
        if i + 1 < TRIES:
            time.sleep(1)
    return None


def _batches(items, size, src, cost):
    """Chunk items: at most `size` entries and a where clause of bounded length."""
    lim = 12000
    cur, n = [], 0
    for it in items:
        c = cost(it)
        if cur and (len(cur) >= size or n + c > lim):
            yield cur
            cur, n = [], 0
        cur.append(it)
        n += c
    if cur:
        yield cur


def _take(src, feats, want, out):
    """Match returned features to wanted ids (by normalised key) and fill `out`."""
    for f in feats:
        try:
            a = f.get("attributes") or {}
            k = src["keyf"](a.get(src["field"]))
            pid = want.get(k)
            if pid is None or pid in out:
                continue
            c = centroid(f.get("geometry"))
            if not c or not _in_ga(c[0], c[1]):
                continue
            r = {"lat": round(c[0], 6), "lon": round(c[1], 6)}
            ad = _attr(a, src["addr"])
            ct = _attr(a, src["city"])
            if ad and re.search(r"[A-Za-z]", ad):
                r["address"] = ad
            if ct:
                r["city"] = ct.title()
            if src.get("ptype"):
                try:
                    r.update(src["ptype"](a) or {})
                except Exception:
                    pass
            try:
                r.update(_extras(src, a, f.get("geometry")))
            except Exception:
                pass
            if "improved" not in r and ("sqft" in r or "yearBuilt" in r):
                r["improved"] = True
            r["v"] = CACHE_V
            out[pid] = r
        except Exception:
            continue


def _run_source(src, pids):
    """Returns (found {pid: result}, answered set of pids whose queries all succeeded)."""
    found, failed = {}, set()
    fails = 0
    want = {}
    for p in pids:
        k = src["keyf"](_clean(p))
        if k:
            want.setdefault(k, p)
    todo = [p for p in pids if src["keyf"](_clean(p)) and want[src["keyf"](_clean(p))] == p]

    # pass 1: exact IN
    def vs(p):
        try:
            return [v for v in dict.fromkeys(src["variants"](p)) if v]
        except Exception:
            return [_clean(p)]
    for chunk in _batches(todo, BATCH, src, lambda p: sum(len(urllib.parse.quote(v)) + 9 for v in vs(p))):
        if fails >= 2:
            failed.update(chunk)
            continue
        vals = list(dict.fromkeys(v for p in chunk for v in vs(p)))
        feats = _query(src, "%s IN (%s)" % (src["field"], ",".join(_q(v) for v in vals)))
        if feats is None:
            fails += 1
            failed.update(chunk)
            continue
        fails = 0
        _take(src, feats, want, found)

    # pass 2: LIKE for what is still missing
    def ls(p):
        try:
            return [v for v in dict.fromkeys(src["like"](p)) if v and len(v.replace("%", "")) >= 4]
        except Exception:
            return []
    rest = [p for p in todo if p not in found and p not in failed and ls(p)]
    f = src["field"]
    for chunk in _batches(rest, LIKE_BATCH, src,
                          lambda p: sum(len(urllib.parse.quote(v)) + len(f) + 16 for v in ls(p))):
        if fails >= 2:
            failed.update(chunk)
            continue
        where = " OR ".join("%s LIKE %s" % (f, _q(v)) for p in chunk for v in ls(p))
        feats = _query(src, where)
        if feats is None:
            fails += 1
            failed.update(chunk)
            continue
        fails = 0
        _take(src, feats, want, found)
    # duplicates of the same normalised key share the result
    for p in pids:
        k = src["keyf"](_clean(p))
        first = want.get(k)
        if first is not None and first in found:
            found[p] = found[first]
        elif first in failed:
            failed.add(p)
    return found, failed


PKEYS = ("ptype", "ptypeDetail", "improved")
XKEYS = ("acres", "acresSrc", "sqft", "yearBuilt")


def _enrich(slug, sources, by, out, cache):
    """Parcels located by a layer that has no property type field (Fayette, Dawson): read the type
    from the county's other layers. A failed query leaves the entry unversioned so it is retried."""
    need = [p for p, src in by.items() if not src.get("ptype") and p in out]
    done = set()
    for src in sources:
        if not need:
            break
        if not src.get("ptype"):
            continue
        try:
            found, failed = _run_source(src, need)
        except Exception:
            found, failed = {}, set(need)
        for p in need:
            r = found.get(p)
            if r:
                if "acres" in r and "acresSrc" not in r and out[p].get("acresSrc"):
                    out[p]["acres"] = r["acres"]        # deeded acreage beats one measured from the map
                    out[p].pop("acresSrc", None)
                if "acres" in r and "acres" not in out[p]:
                    out[p]["acres"] = r["acres"]
                    if r.get("acresSrc"):
                        out[p]["acresSrc"] = r["acresSrc"]
                for k in PKEYS + ("sqft", "yearBuilt", "address", "city"):
                    if k in r and k not in out[p]:
                        out[p][k] = r[k]
            elif p in failed:
                cache["%s|%s" % (slug, p)] = {k: v for k, v in out[p].items() if k != "v"}
            if r:
                done.add(p)
        need = [p for p in need if p not in done]


def locate(slug, parcel_ids, cache):
    """Look up parcel centroids and property types. See module docstring."""
    out = {}
    try:
        if cache is None:
            cache = {}
        pids, stale = [], {}
        for p in dict.fromkeys(parcel_ids or []):
            if p is None or not str(p).strip():
                continue
            ck = "%s|%s" % (slug, p)
            e = cache.get(ck)
            if isinstance(e, dict) and e.get("v") == CACHE_V:
                if not e.get("miss") and e.get("lat") is not None:
                    out[p] = e
                continue
            if isinstance(e, dict) and e.get("lat") is not None:
                stale[p] = e        # located by an older version: keep it if the re-query fails
            pids.append(p)
        sources = SOURCES.get(slug)
        if not sources or not pids:
            out.update(stale)
            return out
        missing = list(pids)
        unanswered = set()      # pids for which some source failed (network) -> do not cache a miss
        by = {}
        for src in sources:
            if not missing:
                break
            try:
                found, failed = _run_source(src, missing)
            except Exception:
                found, failed = {}, set(missing)
            unanswered |= failed
            for p, r in found.items():
                out[p] = r
                by[p] = src
                cache["%s|%s" % (slug, p)] = r
            missing = [p for p in missing if p not in found]
        # condo units and similar: fall back to the parent parcel where the county defines one
        pf = sources[0].get("parent")
        if pf and missing:
            par = {}
            for p in missing:
                try:
                    q = pf(p)
                except Exception:
                    q = None
                if q:
                    par.setdefault(q, []).append(p)
            if par:
                try:
                    found, failed = _run_source(sources[0], list(par))
                except Exception:
                    found, failed = {}, set(par)
                for q, kids in par.items():
                    for p in kids:
                        if q in found:
                            # the parent's use (often HOA common area) does not describe the unit
                            r = {k: v for k, v in found[q].items() if k not in PKEYS + XKEYS}
                            r["note"] = "parent parcel " + q
                            out[p] = r
                            cache["%s|%s" % (slug, p)] = r
                        elif q in failed:
                            unanswered.add(p)
                missing = [p for p in missing if p not in out]
        try:
            _enrich(slug, sources, by, out, cache)
        except Exception:
            pass
        for p in missing:
            ck = "%s|%s" % (slug, p)
            if p in stale:
                out[p] = stale[p]
                if p not in unanswered:     # layer answered without this parcel: stop re-asking
                    out[p] = cache[ck] = dict(stale[p], v=CACHE_V)
            elif p not in unanswered:
                cache[ck] = {"v": CACHE_V, "miss": True}
    except Exception:
        pass
    return out


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3:
        print(json.dumps(locate(sys.argv[1], sys.argv[2:], {}), indent=1))
    else:
        print("usage: parcel_geo.py <slug> <parcel id> [<parcel id> ...]")
