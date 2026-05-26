#!/usr/bin/env python3
"""
Vancouver Daycare Finder
Pulls from BC Gov data, WSTCOAST vacancy PDF, and Wee Queue to build
a prioritized contact list for daycare openings near 228 E 14th Ave.
"""

import csv
import io
import math
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

import pdfplumber
import requests
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

# --- Configuration ---
HOME_LAT = 49.2581508
HOME_LNG = -123.1003308
HOME_ADDR = "228 E 14th Ave"
MAX_DISTANCE_KM = 5.0
TARGET_AGE_MONTHS = 13

BC_GOV_CSV_URL = (
    "https://catalogue.data.gov.bc.ca/dataset/"
    "4cc207cc-ff03-44f8-8c5f-415af5224646/resource/"
    "9a9f14e1-03ea-4a11-936a-6e77b15eeb39/download/childcare_locations.csv"
)
WSTCOAST_SEARCH_URL = "https://www.wstcoast.org/choosing-child-care/search"
WEE_QUEUE_URL = "https://weequeue.ca/infant-daycare-openings/vancouver"

GIST_ID = "5cd2bf016b161048eb2c37cb440e670e"
GIST_URL = f"https://gist.github.com/jasonesanders/{GIST_ID}"
DISCORD_WEBHOOK_URL = ""  # disabled

VCH_INSPECTION_SEARCH = "https://inspections.vch.ca/#/home"
VCH_PROGRAM_ID = "6e0e9442-3016-4294-83f4-0ea25b22ec5b"
VCH_BASE = "https://inspections.vch.ca"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) DaycareSearch/1.0"
}


@dataclass
class Facility:
    name: str
    address: str
    city: str
    postal_code: str
    lat: float
    lng: float
    distance_km: float
    phone: str
    email: str
    website: str
    service_type: str
    serves_under_36: bool
    serves_30mo_5yr: bool
    vacancy_under_36: str
    vacancy_30mo_5yr: str
    vacancy_last_update: str
    inspection_url: str
    bc_gov_source: bool = True
    wstcoast_vacancy: str = ""
    wstcoast_neighbourhood: str = ""
    weequeue_status: str = ""
    weequeue_updated: str = ""
    weequeue_badges: str = ""
    vch_facility_id: str = ""
    vch_inspections: int = -1  # -1 = not looked up
    vch_critical_infractions: int = 0
    vch_noncritical_infractions: int = 0
    vch_outstanding_critical: int = 0
    vch_outstanding_noncritical: int = 0
    vch_last_inspection: str = ""
    tier: int = 3


def haversine(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ---- Source 1: BC Gov CSV ----

def download_bc_gov_csv():
    print("  Downloading BC Gov Child Care Map CSV...")
    resp = requests.get(BC_GOV_CSV_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def parse_bc_gov_csv(csv_text):
    facilities = []
    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        try:
            lat = float(row.get("LATITUDE", 0))
            lng = float(row.get("LONGITUDE", 0))
        except (ValueError, TypeError):
            continue

        if lat == 0 or lng == 0:
            continue

        dist = haversine(HOME_LAT, HOME_LNG, lat, lng)
        if dist > MAX_DISTANCE_KM:
            continue

        if row.get("IS_DUPLICATE", "N") == "Y":
            continue
        if row.get("IS_INCOMPLETE_IND", "N") == "Y":
            continue

        serves_under_36 = row.get("SRVC_UNDER36_YN", "") == "Y"
        serves_30mo_5yr = row.get("SRVC_30MOS_5YRS_YN", "") == "Y"

        fac = Facility(
            name=row.get("NAME", "").strip(),
            address=row.get("ADDRESS_1", "").strip(),
            city=row.get("CITY", "").strip(),
            postal_code=row.get("POSTAL_CODE", "").strip(),
            lat=lat,
            lng=lng,
            distance_km=round(dist, 2),
            phone=row.get("PHONE", "").strip(),
            email=row.get("EMAIL", "").strip(),
            website=row.get("WEBSITE", "").strip(),
            service_type=row.get("SERVICE_TYPE_CD", "").strip(),
            serves_under_36=serves_under_36,
            serves_30mo_5yr=serves_30mo_5yr,
            vacancy_under_36=row.get("VACANCY_SRVC_UNDER36", "").strip(),
            vacancy_30mo_5yr=row.get("VACANCY_SRVC_30MOS_5YRS", "").strip(),
            vacancy_last_update=row.get("VACANCY_LAST_UPDATE", "").strip(),
            inspection_url=VCH_INSPECTION_SEARCH,
        )
        facilities.append(fac)

    print(f"  Found {len(facilities)} licensed facilities within {MAX_DISTANCE_KM} km")
    return facilities


# ---- Source 2: WSTCOAST Vacancy PDF ----

def discover_wstcoast_pdf_url():
    print("  Discovering latest WSTCOAST vacancy PDF URL...")
    try:
        resp = requests.get(WSTCOAST_SEARCH_URL, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        print(f"  Warning: Could not load WSTCOAST search page: {e}")
        return None

    match = re.search(
        r'href="(https://www\.wstcoast\.org/application/files/[^"]*[Vv]acancy[^"]*\.pdf)"',
        resp.text,
    )
    if match:
        url = match.group(1)
        print(f"  Found: {url}")
        return url

    print("  Warning: Could not find vacancy PDF link on WSTCOAST search page")
    return None


def download_and_parse_wstcoast_pdf():
    pdf_url = discover_wstcoast_pdf_url()
    if not pdf_url:
        return [], None

    print("  Downloading WSTCOAST vacancy PDF...")
    try:
        resp = requests.get(pdf_url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        print(f"  Warning: Could not download WSTCOAST PDF: {e}")
        return [], pdf_url

    with open("/tmp/wstcoast_vacancy.pdf", "wb") as f:
        f.write(resp.content)

    entries = []
    with pdfplumber.open("/tmp/wstcoast_vacancy.pdf") as pdf:
        for page in pdf.pages:
            tables = page.extract_tables()
            for table in tables:
                for row in table:
                    if not row or len(row) < 5:
                        continue
                    # Skip header row
                    if row[0] and "Program Name" in row[0]:
                        continue
                    entry = parse_table_row(row)
                    if entry:
                        entries.append(entry)

    print(f"  Extracted {len(entries)} vacancy entries from PDF")
    return entries, pdf_url


def clean_cell(text):
    if not text:
        return ""
    return re.sub(r"\s+", " ", text.replace("\n", " ")).strip()


def parse_table_row(row):
    name = clean_cell(row[0])
    contact_info = clean_cell(row[2]) if len(row) > 2 else ""
    neighbourhood = clean_cell(row[3]) if len(row) > 3 else ""
    vacancy_info = clean_cell(row[4]) if len(row) > 4 else ""

    if not name or not vacancy_info:
        return None

    phone_match = re.search(r"(\d{3}[-.]?\d{3}[-.]?\d{4})", contact_info)
    phone = phone_match.group(1) if phone_match else ""

    email_match = re.search(r"[\w.+-]+@[\w.-]+\.\w+", contact_info)
    email = email_match.group(0) if email_match else ""

    website_match = re.search(r"(https?://\S+|www\.\S+)", contact_info)
    website = website_match.group(1) if website_match else ""

    return {
        "name": name,
        "phone": phone,
        "email": email,
        "website": website,
        "neighbourhood": neighbourhood,
        "vacancy_info": vacancy_info,
    }


# ---- Source 3: Wee Queue ----

def scrape_wee_queue():
    print("  Scraping Wee Queue infant openings...")
    try:
        resp = requests.get(WEE_QUEUE_URL, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        print(f"  Warning: Could not scrape Wee Queue: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    entries = []
    rows = soup.find_all("tr")

    for row in rows:
        name_div = row.find(
            "div",
            class_=lambda c: c and "flex-wrap" in c and "items-center" in c and "gap-2" in c,
        )
        if not name_div:
            continue

        name = ""
        for child in name_div.children:
            t = child.string if hasattr(child, "string") and child.string else ""
            if t and t.strip():
                name = t.strip()
                break
        if not name:
            name = name_div.get_text(strip=True)

        addr_link = row.find(
            "a", class_=lambda c: c and "group" in c and "space-x-2" in c
        )
        addr1 = ""
        addr2 = ""
        if addr_link:
            a1 = addr_link.find(
                "div", class_=lambda c: c and "text-gray-900" in c
            )
            a2 = addr_link.find(
                "div",
                class_=lambda c: c and "text-gray-500" in c and "mt-1" in c,
            )
            addr1 = a1.get_text(strip=True) if a1 else ""
            addr2 = a2.get_text(strip=True) if a2 else ""

        badges = [
            b.get_text(strip=True)
            for b in row.find_all(
                "span", class_=lambda c: c and "bg-green-50" in c
            )
        ]
        badges = list(dict.fromkeys(badges))

        date_div = row.find(
            "div",
            class_=lambda c: c and "text-xs" in c and "text-gray-400" in c,
        )
        updated = (
            date_div.get_text(strip=True).replace("Updated ", "")
            if date_div
            else ""
        )

        # Phone from tel: links
        phone_link = row.find("a", href=lambda h: h and h.startswith("tel:"))
        phone = ""
        if phone_link:
            phone = phone_link.get("href", "").replace("tel:", "").strip()

        has_infant = any("0-36" in b for b in badges)
        address = f"{addr1}, {addr2}" if addr1 and addr2 else addr1 or addr2

        entries.append(
            {
                "name": name,
                "address": address,
                "phone": phone,
                "updated": updated,
                "has_infant_opening": has_infant,
                "badges": ", ".join(badges),
            }
        )

    print(f"  Found {len(entries)} entries from Wee Queue")
    return entries


# ---- Cross-referencing ----

def normalize_name(name):
    name = name.lower().strip()
    name = re.sub(
        r"\s*(licensed family child care|family child care|child care centre|"
        r"child care center|childcare|child care|day care centre|daycare centre|"
        r"daycare center|day care center|daycare|day care|group child care|"
        r"early learning and|early learning|in-home multi-age|multi-age|"
        r"preschool|rlnr|inc\.|inc|ltd\.|ltd|society|centre|center)\s*",
        " ",
        name,
    )
    name = re.sub(r"[^\w\s]", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def name_match(name1, name2):
    n1 = normalize_name(name1)
    n2 = normalize_name(name2)
    if not n1 or not n2:
        return False
    if n1 == n2:
        return True
    if len(n1) > 3 and len(n2) > 3:
        if n1 in n2 or n2 in n1:
            return True
    words1 = set(n1.split())
    words2 = set(n2.split())
    if len(words1) >= 2 and len(words2) >= 2:
        overlap = words1 & words2
        min_len = min(len(words1), len(words2))
        if len(overlap) >= max(2, min_len * 0.6):
            return True
    return False


def enrich_with_wstcoast(facilities, wstcoast_entries):
    print("  Cross-referencing with WSTCOAST vacancy data...")
    matched = 0
    unmatched = []

    for entry in wstcoast_entries:
        found = False
        for fac in facilities:
            if name_match(fac.name, entry["name"]):
                fac.wstcoast_vacancy = entry["vacancy_info"]
                fac.wstcoast_neighbourhood = entry["neighbourhood"]
                if not fac.phone and entry["phone"]:
                    fac.phone = entry["phone"]
                if not fac.email and entry["email"]:
                    fac.email = entry["email"]
                if not fac.website and entry.get("website"):
                    fac.website = entry["website"]
                fac.tier = 1
                matched += 1
                found = True
                break
        if not found:
            unmatched.append(entry)

    for entry in unmatched:
        fac = Facility(
            name=entry["name"],
            address="",
            city="Vancouver",
            postal_code="",
            lat=0,
            lng=0,
            distance_km=-1,
            phone=entry["phone"],
            email=entry["email"],
            website=entry.get("website", ""),
            service_type="",
            serves_under_36=True,
            serves_30mo_5yr=True,
            vacancy_under_36="",
            vacancy_30mo_5yr="",
            vacancy_last_update="",
            inspection_url="",
            bc_gov_source=False,
            wstcoast_vacancy=entry["vacancy_info"],
            wstcoast_neighbourhood=entry["neighbourhood"],
            tier=1,
        )
        facilities.append(fac)

    print(f"  Matched {matched} to BC Gov data; {len(unmatched)} WSTCOAST-only entries added")


def enrich_with_weequeue(facilities, weequeue_entries):
    print("  Cross-referencing with Wee Queue data...")
    matched = 0
    unmatched = []

    for entry in weequeue_entries:
        found = False
        for fac in facilities:
            if name_match(fac.name, entry["name"]):
                if entry["has_infant_opening"]:
                    fac.weequeue_status = "Infant opening listed"
                    if fac.tier > 2:
                        fac.tier = 2
                else:
                    fac.weequeue_status = "Listed (check age)"
                fac.weequeue_updated = entry.get("updated", "")
                fac.weequeue_badges = entry.get("badges", "")
                if not fac.address and entry.get("address"):
                    fac.address = entry["address"]
                if not fac.phone and entry.get("phone"):
                    fac.phone = entry["phone"]
                matched += 1
                found = True
                break
        if not found:
            unmatched.append(entry)

    # Add unmatched Wee Queue entries with infant openings
    for entry in unmatched:
        if not entry["has_infant_opening"]:
            continue
        fac = Facility(
            name=entry["name"],
            address=entry.get("address", ""),
            city="Vancouver",
            postal_code="",
            lat=0,
            lng=0,
            distance_km=-1,
            phone=entry.get("phone", ""),
            email="",
            website="",
            service_type="",
            serves_under_36=True,
            serves_30mo_5yr=False,
            vacancy_under_36="",
            vacancy_30mo_5yr="",
            vacancy_last_update="",
            inspection_url="",
            bc_gov_source=False,
            weequeue_status="Infant opening listed",
            weequeue_updated=entry.get("updated", ""),
            weequeue_badges=entry.get("badges", ""),
            tier=2,
        )
        facilities.append(fac)

    print(f"  Matched {matched} to existing data; {len([e for e in unmatched if e['has_infant_opening']])} Wee Queue-only infant entries added")


def assign_tiers(facilities):
    for fac in facilities:
        if fac.tier <= 2:
            continue
        if fac.vacancy_under_36 and fac.vacancy_under_36.upper() not in ("N", "NO", ""):
            fac.tier = 2


def age_relevant(fac):
    """Check if this facility serves children 30 months and younger."""
    vacancy_text = (fac.wstcoast_vacancy + " " + fac.weequeue_badges).lower()
    name_text = fac.name.lower()

    # Hard exclude: school-age-only facilities
    school_only = (
        "out of school" in name_text
        or "after school" in name_text
        or "before and after school" in name_text
        or re.search(r"kindergarten\s+to\s+grade", vacancy_text)
        or re.search(r"grade\s+[1-7]", vacancy_text)
    )
    if school_only:
        return False

    # Preschool-only (serves 30mo-5yr but NOT under 36mo)
    if fac.serves_30mo_5yr and not fac.serves_under_36:
        # Keep it only if vacancy text or badges suggest younger children too
        if fac.weequeue_badges and "0-36" in fac.weequeue_badges:
            return True
        if fac.wstcoast_vacancy:
            text = fac.wstcoast_vacancy.lower()
            if re.search(
                r"(under\s*3|12\s*m|15\s*m|18\s*m|0-3|0\s*-\s*3|1\s*year|8\s*month|10\s*m|infant|toddler|under\s*36)",
                text,
            ):
                return True
        return False

    # For Tier 1 entries, exclude if vacancy only mentions 3+ year olds
    if fac.tier == 1 and fac.wstcoast_vacancy:
        text = fac.wstcoast_vacancy.lower()
        has_young = re.search(
            r"(under\s*3|12\s*m|15\s*m|18\s*m|0-3|0\s*-\s*3|1\s*year|8\s*month|10\s*m|infant|toddler|under\s*36|30\s*month)",
            text,
        )
        has_only_old = re.search(
            r"(\b[3-5]\s*years?\s*old\b|\b3-5\b|\b2\.5-[456]\b|\b3\s*½)", text
        ) and not has_young
        if has_only_old:
            return False

    # Positive signals: serves under 36 months
    if fac.serves_under_36:
        return True
    if fac.weequeue_badges and "0-36" in fac.weequeue_badges:
        return True
    if fac.tier == 1 and fac.wstcoast_vacancy:
        text = fac.wstcoast_vacancy.lower()
        if re.search(
            r"(under\s*3|12\s*m|15\s*m|18\s*m|0-3|1\s*year|8\s*month|10\s*m|infant|toddler|under\s*36|30\s*month)",
            text,
        ):
            return True

    # No age data at all: keep only if BC Gov flags are all blank (unknown)
    if not fac.serves_under_36 and not fac.serves_30mo_5yr:
        return True

    return False


# ---- Source 4: VCH Inspection Reports (Playwright) ----

def enrich_with_vch_inspections(facilities):
    if not HAS_PLAYWRIGHT:
        print("  Playwright not installed, skipping VCH inspection lookup")
        return

    print("  Launching browser for VCH inspection lookup...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        # Load the site once to establish session
        page.goto(f"{VCH_BASE}/#/home", wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(2000)

        # Fetch all child care facilities via API using browser context
        all_vch = []
        page_num = 0
        page_size = 200
        while True:
            data = page.evaluate(
                """async ([programId, pageNum, pageSize]) => {
                    const resp = await fetch(
                        `/api/v0/portal/disclosure/program/facilities`,
                        {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({
                                pageNumber: pageNum,
                                pageSize: pageSize,
                                criteria: '',
                                sort: [{field: 'facilityName', order: 'asc'}],
                                disclosureProgramId: programId,
                                fields: ['community', 'facilityName', 'facilityType', 'phoneNumber', 'siteAddress', 'website'],
                                filters: []
                            })
                        }
                    );
                    return await resp.json();
                }""",
                [VCH_PROGRAM_ID, page_num, page_size],
            )
            results = data.get("result", [])
            all_vch.extend(results)
            total = data.get("totalNumberOfRecords", 0)
            if len(all_vch) >= total or not results:
                break
            page_num += 1

        print(f"  Loaded {len(all_vch)} VCH facility records")

        # Build lookup by normalized name (multiple VCH records can share a name)
        vch_by_name = {}
        for vf in all_vch:
            name = (vf.get("facilityName") or "").strip()
            if name:
                norm = normalize_name(name)
                vch_by_name.setdefault(norm, []).append(vf)

        def addr_score(fac, vf):
            """Score how well a VCH record matches a facility by address."""
            vch_addr = (vf.get("siteAddress") or "").lower()
            vch_name = (vf.get("facilityName") or "").lower()
            score = 0
            fac_num = re.search(r"^#?\d+[-]?(\d+)?", fac.address.strip())
            if fac_num:
                num = fac_num.group().lstrip("#")
                if num in vch_addr or num in vch_name:
                    score += 10
            if fac.postal_code:
                pc = fac.postal_code.lower().replace(" ", "")
                if pc in vch_addr.replace(" ", ""):
                    score += 5
            fac_street = re.sub(r"^\d+[-\s]*", "", fac.address.lower()).strip()
            if fac_street and len(fac_street) > 3 and fac_street in vch_addr:
                score += 3
            return score

        def best_vch_match(fac, candidates):
            """Pick the best VCH match by comparing address."""
            if len(candidates) == 1:
                return candidates[0]
            scored = [(addr_score(fac, vf), vf) for vf in candidates]
            scored.sort(key=lambda x: -x[0])
            return scored[0][1]

        # Match our facilities to VCH records
        matched = 0
        for fac in facilities:
            fac_norm = normalize_name(fac.name)
            candidates = vch_by_name.get(fac_norm, [])[:]
            if not candidates:
                for vch_norm, vch_list in vch_by_name.items():
                    if name_match(fac.name, vch_list[0].get("facilityName", "")):
                        candidates.extend(vch_list)
            if candidates:
                vf = best_vch_match(fac, candidates)
                fac.vch_facility_id = vf["id"]
                fac.inspection_url = f"{VCH_BASE}/#/{VCH_PROGRAM_ID}/disclosure/facility/{vf['id']}"
                if not fac.phone and vf.get("phoneNumber"):
                    fac.phone = vf["phoneNumber"]
                if not fac.website and vf.get("website"):
                    fac.website = vf["website"]
                matched += 1

        print(f"  Matched {matched} facilities to VCH records (deep links created)")

        # Fetch inspection history + outstanding infractions concurrently
        to_inspect = [f for f in facilities if f.vch_facility_id]
        inspected = 0
        batch_size = 10
        for i in range(0, len(to_inspect), batch_size):
            batch = to_inspect[i : i + batch_size]
            fac_ids = [f.vch_facility_id for f in batch]
            try:
                results = page.evaluate(
                    """async ([programId, facIds]) => {
                        const out = {};
                        await Promise.all(facIds.map(async (fid) => {
                            try {
                                const [detailResp, inspResp] = await Promise.all([
                                    fetch(`/api/v0/portal/disclosure/facilityDetails/${programId}`, {
                                        method: 'POST',
                                        headers: {'Content-Type': 'application/json'},
                                        body: JSON.stringify([fid])
                                    }),
                                    fetch(`/api/v0/portal/disclosure/program/${programId}/facility/${fid}/inspectionDetails/`)
                                ]);
                                const detail = await detailResp.json();
                                const inspections = await inspResp.json();
                                out[fid] = {
                                    detail: Array.isArray(detail) && detail.length > 0 ? detail[0] : null,
                                    inspections: Array.isArray(inspections) ? inspections : []
                                };
                            } catch(e) {
                                out[fid] = null;
                            }
                        }));
                        return out;
                    }""",
                    [VCH_PROGRAM_ID, fac_ids],
                )
                for fac in batch:
                    r = results.get(fac.vch_facility_id)
                    if not r:
                        continue
                    if r.get("detail"):
                        d = r["detail"]
                        fac.vch_outstanding_critical = d.get("outstandingCriticalInfractions") or 0
                        fac.vch_outstanding_noncritical = d.get("outstandingNonCriticalInfractions") or 0
                    insps = r.get("inspections", [])
                    if insps:
                        fac.vch_inspections = len(insps)
                        fac.vch_critical_infractions = sum(
                            (x.get("criticalInfractionCount") or 0) for x in insps
                        )
                        fac.vch_noncritical_infractions = sum(
                            (x.get("nonCriticalInfractionCount") or 0) for x in insps
                        )
                        dates = [
                            x.get("inspectionDate", "")[:10]
                            for x in insps
                            if x.get("inspectionDate")
                        ]
                        if dates:
                            fac.vch_last_inspection = max(dates)
                        inspected += 1
            except Exception:
                pass

        print(f"  Fetched inspection data for {inspected}/{len(to_inspect)} facilities")

        # Legacy single-fetch fallback for any that failed in batch
        missed = [f for f in to_inspect if f.vch_inspections < 0]
        for fac in missed:
            try:
                inspections = page.evaluate(
                    """async ([programId, facilityId]) => {
                        const resp = await fetch(
                            `/api/v0/portal/disclosure/program/${programId}/facility/${facilityId}/inspectionDetails/`
                        );
                        return await resp.json();
                    }""",
                    [VCH_PROGRAM_ID, fac.vch_facility_id],
                )
                if isinstance(inspections, list):
                    fac.vch_inspections = len(inspections)
                    fac.vch_critical_infractions = sum(
                        (insp.get("criticalInfractionCount") or 0) for insp in inspections
                    )
                    fac.vch_noncritical_infractions = sum(
                        (insp.get("nonCriticalInfractionCount") or 0) for insp in inspections
                    )
                    dates = [
                        insp.get("inspectionDate", "")[:10]
                        for insp in inspections
                        if insp.get("inspectionDate")
                    ]
                    if dates:
                        fac.vch_last_inspection = max(dates)
                    inspected += 1
            except Exception:
                pass

        if missed:
            print(f"  Retried {len(missed)} missed facilities individually")
        print(f"  Fetched inspection data for {inspected}/{len(to_inspect)} facilities total")
        browser.close()


# ---- Output ----

def format_facility(fac, lines):
    dist_str = f"{fac.distance_km} km" if fac.distance_km >= 0 else "distance unknown"
    addr_parts = [fac.address, fac.city, fac.postal_code]
    addr_str = ", ".join(p for p in addr_parts if p)

    vacancy_label = ""
    if fac.tier == 1:
        vacancy_label = " **[VACANCY]**"
    elif fac.tier == 2:
        vacancy_label = " *[likely vacancy]*"

    lines.append(f"#### - [ ] {fac.name}{vacancy_label}")

    if addr_str:
        lines.append(f"{addr_str} ({dist_str})")
    elif fac.wstcoast_neighbourhood:
        lines.append(f"{fac.wstcoast_neighbourhood} ({dist_str})")
    else:
        lines.append(f"({dist_str})")

    contact_parts = []
    if fac.phone:
        contact_parts.append(fac.phone)
    if fac.email:
        contact_parts.append(fac.email)
    if fac.website:
        contact_parts.append(fac.website)
    if contact_parts:
        lines.append(f"Contact: {' | '.join(contact_parts)}")

    if fac.wstcoast_vacancy:
        lines.append(f"Vacancy: {fac.wstcoast_vacancy}")
    if fac.weequeue_status:
        wq_line = f"Wee Queue: {fac.weequeue_status}"
        if fac.weequeue_updated:
            wq_line += f" (updated {fac.weequeue_updated})"
        lines.append(wq_line)

    if fac.vch_inspections >= 0:
        insp_parts = [f"{fac.vch_inspections} inspections on file"]
        if fac.vch_last_inspection:
            insp_parts.append(f"last: {fac.vch_last_inspection}")

        flags = []
        if fac.vch_outstanding_critical > 0:
            flags.append(f"**{fac.vch_outstanding_critical} outstanding critical**")
        if fac.vch_outstanding_noncritical > 0:
            flags.append(f"**{fac.vch_outstanding_noncritical} outstanding non-critical**")
        if flags:
            insp_parts.extend(flags)
        else:
            insp_parts.append("no outstanding infractions")

        insp_str = ", ".join(insp_parts)
        if fac.inspection_url:
            lines.append(f"Inspections: {insp_str} ([view]({fac.inspection_url}))")
        else:
            lines.append(f"Inspections: {insp_str}")
    elif fac.inspection_url:
        lines.append(f"[Inspection reports]({fac.inspection_url})")

    lines.append("")


def format_markdown(facilities, wstcoast_pdf_url=None):
    today = datetime.now().strftime("%Y-%m-%d")
    wstcoast_link = f"[WSTCOAST Vacancy List]({wstcoast_pdf_url})" if wstcoast_pdf_url else "WSTCOAST Vacancy List (not available)"

    group_facs = [f for f in facilities if f.service_type == "Licensed Group"]
    family_facs = [f for f in facilities if f.service_type == "Licensed Family"]
    other_facs = [f for f in facilities if f.service_type not in ("Licensed Group", "Licensed Family")]

    def sort_key(f):
        return (f.tier, f.distance_km if f.distance_km >= 0 else 999)

    group_facs.sort(key=sort_key)
    family_facs.sort(key=sort_key)
    other_facs.sort(key=sort_key)

    lines = [
        "---",
        f"created: {today}",
        "tags: [daycare, nora, vancouver]",
        "---",
        "",
        "# Vancouver Daycare Search",
        "",
        f"Generated {today} for Nora (~{TARGET_AGE_MONTHS} months old).",
        f"From {HOME_ADDR}. Radius: {MAX_DISTANCE_KM} km. Filtered to under-30-month availability.",
        "",
        "**Sources:**",
        "- [BC Gov Child Care Map](https://catalogue.data.gov.bc.ca/dataset/child-care-map-data) (daily CSV)",
        f"- {wstcoast_link} (weekly PDF, Fridays)",
        f"- [Wee Queue Infant Openings]({WEE_QUEUE_URL}) (provincial vacancy data)",
        "",
        "**How to use:** Facilities marked **[VACANCY]** have confirmed openings (WSTCOAST PDF). Those marked *[likely vacancy]* have provincial vacancy flags (Wee Queue). Unmarked facilities have no known vacancy but may have unlisted openings. Check the box after contacting each one.",
        "",
        "---",
        "",
        "## Quick Reference: Licensed Group vs Licensed Family",
        "",
        "| | Licensed Group (Centre) | Licensed Family (Home) |",
        "|---|---|---|",
        "| **Setting** | Commercial facility/centre | Provider's personal residence |",
        "| **Max capacity** | Up to 12 infants (under 36 mo) | Up to 7 children total (birth-12 yrs) |",
        "| **Staff ratio (infant)** | 1:4 (ECE-certified required) | 1 adult for all 7 children |",
        "| **Staff training** | ~900 hrs ECE certification | 20 hrs child care training |",
        "| **Typical cost (infant, after CCFRI)** | ~$800-1,200/mo | ~$600-1,000/mo |",
        "| **Pros** | Structured program, backup staff, regulated curriculum | Smaller group, home environment, flexible |",
        "| **Cons** | Larger groups, more structured, less flexible hours | Single caregiver, less oversight |",
        "",
        "**Fee note:** Most licensed facilities participate in BC's Child Care Fee Reduction Initiative (CCFRI), which reduces fees by up to $900/month per child. The `IS_CCFRI_AUTH` flag in BC data indicates participation. You may also qualify for the Affordable Child Care Benefit (income-tested, up to $1,250/month).",
        "",
        "**Inspection reports:** [Search VCH inspection reports](https://inspections.vch.ca/#/home) by facility name to see compliance history, substantiated complaints, and routine inspection results.",
        "",
        "---",
        "",
    ]

    # Licensed Group section
    g_t1 = len([f for f in group_facs if f.tier == 1])
    g_t2 = len([f for f in group_facs if f.tier == 2])
    lines.append(f"## Licensed Group (Centre) ({len(group_facs)} facilities, {g_t1} confirmed + {g_t2} likely vacancies)")
    lines.append("")
    if not group_facs:
        lines.append("None found.")
        lines.append("")
    else:
        for fac in group_facs:
            format_facility(fac, lines)

    # Licensed Family section
    f_t1 = len([f for f in family_facs if f.tier == 1])
    f_t2 = len([f for f in family_facs if f.tier == 2])
    lines.append(f"## Licensed Family (Home) ({len(family_facs)} facilities, {f_t1} confirmed + {f_t2} likely vacancies)")
    lines.append("")
    if not family_facs:
        lines.append("None found.")
        lines.append("")
    else:
        for fac in family_facs:
            format_facility(fac, lines)

    # Other/unknown type
    if other_facs:
        o_t1 = len([f for f in other_facs if f.tier == 1])
        o_t2 = len([f for f in other_facs if f.tier == 2])
        lines.append(f"## Other / Type Unknown ({len(other_facs)} facilities, {o_t1} confirmed + {o_t2} likely vacancies)")
        lines.append("")
        for fac in other_facs:
            format_facility(fac, lines)

    # Summary
    lines.append("---")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    t1 = len([f for f in facilities if f.tier == 1])
    t2 = len([f for f in facilities if f.tier == 2])
    t3 = len([f for f in facilities if f.tier == 3])
    lines.append(f"| | Group | Family | Other | Total |")
    lines.append(f"|---|---|---|---|---|")
    lines.append(f"| Confirmed vacancy | {g_t1} | {f_t1} | {len([f for f in other_facs if f.tier == 1])} | {t1} |")
    lines.append(f"| Likely vacancy | {g_t2} | {f_t2} | {len([f for f in other_facs if f.tier == 2])} | {t2} |")
    lines.append(f"| No known vacancy | {len([f for f in group_facs if f.tier == 3])} | {len([f for f in family_facs if f.tier == 3])} | {len([f for f in other_facs if f.tier == 3])} | {t3} |")
    lines.append(f"| **Total** | **{len(group_facs)}** | **{len(family_facs)}** | **{len(other_facs)}** | **{len(facilities)}** |")
    lines.append("")
    lines.append(f"Data pulled: {today}")
    lines.append("")
    lines.append("Re-run `python3 01-Projects/Daycare-Search/find_daycare.py` to refresh.")
    lines.append("")

    return "\n".join(lines)


def update_gist(md_content):
    """Update the GitHub Gist via gh CLI."""
    output_path = os.path.join(SCRIPT_DIR, "Daycare Search Results.md")
    try:
        import subprocess
        result = subprocess.run(
            ["gh", "gist", "edit", GIST_ID, output_path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            print(f"  Gist updated: {GIST_URL}")
            return True
        else:
            print(f"  Warning: Could not update gist: {result.stderr.strip()}")
            return False
    except FileNotFoundError:
        print("  Warning: gh CLI not found, skipping gist update")
        return False
    except Exception as e:
        print(f"  Warning: Gist update failed: {e}")
        return False


def send_discord_notification(facilities, prev_md=None):
    """Send a summary to Discord, highlighting changes if previous results exist."""
    t1 = [f for f in facilities if f.tier == 1]
    t2 = [f for f in facilities if f.tier == 2]
    today = datetime.now().strftime("%Y-%m-%d")

    # Build the message
    lines = [f"**Daycare Search Update** ({today})\n"]

    if t1:
        lines.append(f"**{len(t1)} confirmed vacancies:**")
        for fac in sorted(t1, key=lambda f: f.distance_km if f.distance_km >= 0 else 999):
            dist = f"{fac.distance_km} km" if fac.distance_km >= 0 else "?"
            vacancy = ""
            if fac.wstcoast_vacancy:
                vacancy = f" - {fac.wstcoast_vacancy[:80]}"
            lines.append(f"- **{fac.name}** ({dist}){vacancy}")
        lines.append("")

    lines.append(f"{len(t2)} likely vacancies, {len(facilities)} total facilities")
    lines.append(f"\nFull list: {GIST_URL}")

    message = "\n".join(lines)

    # Discord has a 2000 char limit
    if len(message) > 1900:
        message = message[:1900] + f"\n\n... [full list]({GIST_URL})"

    try:
        resp = requests.post(
            DISCORD_WEBHOOK_URL,
            json={"content": message},
            timeout=10,
        )
        if resp.status_code in (200, 204):
            print("  Discord notification sent")
        else:
            print(f"  Warning: Discord returned {resp.status_code}")
    except Exception as e:
        print(f"  Warning: Discord notification failed: {e}")


def main():
    print("=== Vancouver Daycare Finder ===\n")
    print("[1/5] Pulling BC Gov facility data...")
    csv_text = download_bc_gov_csv()
    facilities = parse_bc_gov_csv(csv_text)

    print("[2/5] Pulling WSTCOAST vacancy list...")
    wstcoast_entries, wstcoast_pdf_url = download_and_parse_wstcoast_pdf()

    print("[3/5] Pulling Wee Queue infant openings...")
    weequeue_entries = scrape_wee_queue()

    print("[4/5] Cross-referencing sources...")
    enrich_with_wstcoast(facilities, wstcoast_entries)
    enrich_with_weequeue(facilities, weequeue_entries)
    assign_tiers(facilities)

    # Filter out facilities clearly serving only older children
    before = len(facilities)
    facilities = [f for f in facilities if age_relevant(f)]
    removed = before - len(facilities)
    if removed:
        print(f"  Removed {removed} facilities serving only older children")

    print("[5/6] Looking up VCH inspection reports...")
    enrich_with_vch_inspections(facilities)

    print("[6/8] Writing results...")
    md = format_markdown(facilities, wstcoast_pdf_url)
    output_path = os.path.join(SCRIPT_DIR, "Daycare Search Results.md")
    with open(output_path, "w") as f:
        f.write(md)

    t1 = len([f for f in facilities if f.tier == 1])
    t2 = len([f for f in facilities if f.tier == 2])
    t3 = len([f for f in facilities if f.tier == 3])

    print("[7/8] Updating gist...")
    update_gist(md)

    print("[8/8] Sending Discord notification...")
    send_discord_notification(facilities)

    print(f"\nDone! Results written to:\n  {output_path}")
    print(f"  Gist: {GIST_URL}")
    print(f"\n  Tier 1 (confirmed vacancy): {t1}")
    print(f"  Tier 2 (likely vacancy):    {t2}")
    print(f"  Tier 3 (call to check):     {t3}")
    print(f"  Total: {len(facilities)}")


if __name__ == "__main__":
    main()
