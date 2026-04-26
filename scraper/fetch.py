#!/usr/bin/env python3
"""
Hillsborough County FL - Motivated Seller Lead Scraper
Portal : https://publicaccess.hillsclerk.com/oripublicaccess/
PA     : https://gis.hcpafl.org/arcgis/rest/services/Webmaps/HillsboroughFL_WebParcels/MapServer/0
"""
import asyncio, csv, json, logging, os, re, sys, time
from datetime import datetime, timedelta
from pathlib import Path

import requests
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "7"))
HEADLESS      = os.getenv("HEADLESS", "true").lower() != "false"
BASE_URL      = "https://publicaccess.hillsclerk.com/oripublicaccess/"

# Confirmed working HCPA ArcGIS layer (discovered 2026-04-25)
PA_LAYER = "https://gis.hcpafl.org/arcgis/rest/services/Webmaps/HillsboroughFL_WebParcels/MapServer/0"
# Fields confirmed: Owner1, FullAddress, SiteCity, SiteZip, NameLabel

ROOT = Path(__file__).parent.parent
(ROOT / "data").mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(ROOT / "data" / "scraper.log", mode="w", encoding="utf-8"),
    ]
)
log = logging.getLogger(__name__)

# Portal column indices (confirmed from live HTML)
COL_GRANTEE = 5
COL_OWNER   = 6
COL_DATE    = 7
COL_DTYPE   = 8
COL_BOOK    = 9
COL_PAGE    = 10
COL_LEGAL   = 11
COL_DOCNUM  = 12

DOC_TYPE_OPTIONS = [
    ("LP",     "(LP) LIS PENDENS"),
    ("JUD",    "(JUD) JUDGMENT"),
    ("LN",     "(LN) LIEN"),
    ("MEDLN",  "(MEDLN) MEDICAID LIEN"),
    ("PRO",    "(PRO) PROBATE DOCUMENTS"),
    ("NOTICE", "(TAXDEED) TAX DEED"),
]

DOC_CATS = {
    "LP": "Lis Pendens", "NOTICE": "Tax Deed", "JUD": "Judgment",
    "LN": "Lien", "LNMECH": "Mechanic Lien", "LNHOA": "HOA Lien",
    "MEDLN": "Medicaid Lien", "PRO": "Probate",
}

def infer_cat(doc_type):
    u = doc_type.upper()
    if "PENDENS" in u:                     return "LP"
    if "TAX DEED" in u or "TAXDEED" in u: return "NOTICE"
    if "MEDICAID" in u:                    return "MEDLN"
    if "HOA" in u or "HOMEOWNER" in u:    return "LNHOA"
    if "MECHANIC" in u:                    return "LNMECH"
    if "JUDGMENT" in u:                    return "JUD"
    if "PROBATE" in u:                     return "PRO"
    if "LIEN" in u:                        return "LN"
    return "LN"

def score_record(rec):
    s, flags = 30, []
    cat   = rec.get("cat", "")
    amt   = rec.get("amount") or 0
    owner = (rec.get("owner") or "").upper()
    flag_map = {
        "LP": ["Lis pendens","Pre-foreclosure"], "NOTICE": ["Tax lien"],
        "JUD": ["Judgment lien"], "LN": ["Mechanic lien"],
        "LNMECH": ["Mechanic lien"], "LNHOA": ["Mechanic lien"],
        "MEDLN": ["Judgment lien"], "PRO": ["Probate / estate"],
    }
    for f in flag_map.get(cat, []):
        flags.append(f); s += 10
    if cat == "LP":    s += 20
    if amt > 100_000:  s += 15
    elif amt > 50_000: s += 10
    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    if rec.get("filed","") >= week_ago:
        flags.append("New this week"); s += 5
    if rec.get("prop_address") or rec.get("mail_address"):
        flags.append("Has address"); s += 5
    if any(k in owner for k in ("LLC","INC","CORP","LTD","TRUST","VENTURES","PROPERTIES","GROUP")):
        flags.append("LLC / corp owner"); s += 10
    rec["flags"] = list(dict.fromkeys(flags))
    rec["score"] = min(s, 100)
    return rec


# ============================================================
# PROPERTY APPRAISER LOOKUP
# ============================================================
def pa_lookup(owner_name, session):
    """
    Query HillsboroughFL_WebParcels ArcGIS layer by owner name.
    Fields: Owner1, FullAddress, SiteCity, SiteZip
    """
    if not owner_name or len(owner_name) < 3:
        return {}
    try:
        # Use last name for search (most selective)
        search = owner_name.split(",")[0].strip() if "," in owner_name else owner_name.split()[0].strip()
        if len(search) < 3:
            return {}

        r = session.get(f"{PA_LAYER}/query", params={
            "where":             f"UPPER(Owner1) LIKE '{search.upper()}%'",
            "outFields":         "Owner1,Owner2,FullAddress,SiteCity,SiteZip,NameLabel",
            "returnGeometry":    "false",
            "f":                 "json",
            "resultRecordCount": "10",
        }, timeout=10)

        if not r.ok:
            log.debug("PA query failed: %s", r.status_code)
            return {}

        data = r.json()
        features = data.get("features", [])
        if not features:
            return {}

        # Find best match by scoring word overlap with full owner name
        owner_words = set(owner_name.upper().replace(",","").split())
        best, best_score = features[0], 0
        for feat in features:
            a = feat["attributes"]
            candidate = str(a.get("Owner1","") or "").upper().replace(",","")
            score = len(owner_words & set(candidate.split()))
            if score > best_score:
                best_score = score
                best = feat

        a = best["attributes"]
        full_addr = str(a.get("FullAddress","") or "").strip()
        city      = str(a.get("SiteCity","") or "").strip()
        zipcode   = str(a.get("SiteZip","") or "").strip()

        if not full_addr:
            return {}

        # FullAddress typically = "4821 LONGFELLOW DR, TAMPA, FL 33624"
        # Try to split it, or just use it as prop_address
        addr_part = full_addr
        if city and city.upper() in full_addr.upper():
            addr_part = full_addr[:full_addr.upper().index(city.upper())].rstrip(", ").strip()

        return {
            "prop_address": addr_part or full_addr,
            "prop_city":    city,
            "prop_state":   "FL",
            "prop_zip":     zipcode,
            # Use property address as mailing too if owner lives there
            "mail_address": addr_part or full_addr,
            "mail_city":    city,
            "mail_state":   "FL",
            "mail_zip":     zipcode,
        }

    except Exception as e:
        log.debug("PA lookup error for '%s': %s", owner_name, e)
        return {}


def enrich_with_pa(records):
    log.info("Looking up addresses in HCPA for %d records...", len(records))
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept":     "application/json, text/plain, */*",
        "Referer":    "https://gis.hcpafl.org/propertysearch/",
    })

    # Quick test with first owner name
    test_owner = next((r.get("owner","") for r in records if r.get("owner")), "")
    if test_owner:
        test_result = pa_lookup(test_owner, session)
        if test_result:
            log.info("PA API working! Test: '%s' >> %s", test_owner, test_result.get("prop_address","?"))
        else:
            log.warning("PA API returned no result for test name: '%s'", test_owner)

    enriched = 0
    for i, rec in enumerate(records):
        owner = rec.get("owner","").strip()
        if not owner:
            continue
        pa = pa_lookup(owner, session)
        if pa:
            rec.update({k: v for k, v in pa.items() if v})
            enriched += 1
            log.info("  [%d/%d] %-30s >> %s", i+1, len(records),
                     owner[:30], pa.get("prop_address","?"))
        time.sleep(0.1)

    log.info("PA enrichment: %d/%d records have address", enriched, len(records))
    return records


# ============================================================
# CLERK PORTAL SCRAPER
# ============================================================
class HillsboroughScraper:
    def __init__(self, start_mdy, end_mdy):
        self.start   = start_mdy
        self.end     = end_mdy
        self.records = []

    async def run(self):
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=HEADLESS,
                args=["--disable-blink-features=AutomationControlled"]
            )
            ctx = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 900},
            )
            page = await ctx.new_page()
            try:
                await self._scrape(page)
            except Exception as e:
                log.error("Fatal error: %s", e, exc_info=True)
                await page.screenshot(path=str(ROOT / "data" / "error.png"))
            finally:
                await browser.close()
        return self.records

    async def _scrape(self, page):
        log.info("Loading clerk portal...")
        await page.goto(BASE_URL, wait_until="networkidle", timeout=45_000)
        await asyncio.sleep(2)
        await page.click('div[queryid="322"]')
        await asyncio.sleep(2)
        log.info("Document Type search active.")

        for cat, option_value in DOC_TYPE_OPTIONS:
            log.info("--- Searching: %s ---", cat)
            try:
                recs = await self._search_all_pages(page, cat, option_value)
                log.info("  Total: %d records", len(recs))
                self.records.extend(recs)
            except Exception as e:
                log.warning("  Failed for %s: %s", cat, e)
                await page.screenshot(path=str(ROOT / "data" / f"error_{cat}.png"))
            try:
                await page.click("#reset")
                await asyncio.sleep(1)
            except:
                pass

    async def _search_all_pages(self, page, cat, option_value):
        await page.fill("input.record-begin", self.start)
        await asyncio.sleep(0.3)
        await page.fill("input.record-end", self.end)
        await asyncio.sleep(0.3)
        await page.evaluate(f"""
            var sel = document.getElementById('OBKey__1285_1');
            for (var i = 0; i < sel.options.length; i++) {{
                sel.options[i].selected = (sel.options[i].value === {json.dumps(option_value)});
            }}
            $(sel).trigger('chosen:updated');
        """)
        await asyncio.sleep(0.5)
        await page.click("#sub")
        log.info("  Submitted. Waiting...")

        try:
            await page.wait_for_selector(
                "#jsgrid-table tr.jsgrid-row, #jsgrid-table tbody tr",
                timeout=20_000
            )
            await asyncio.sleep(1)
        except PWTimeout:
            log.info("  No results.")
            return []

        all_rows = []
        page_num = 1

        while True:
            rows = await self._extract_rows(page)
            all_rows.extend(rows)
            log.info("  Page %d: %d rows (total: %d)", page_num, len(rows), len(all_rows))

            # Log the pager HTML so we can see what pagination controls exist
            pager_info = await page.evaluate("""
                () => {
                    var pager = document.querySelector('.jsgrid-pager, #resultsActionBar, [class*="pager"]');
                    return pager ? pager.outerHTML.substring(0, 500) : 'no pager found';
                }
            """)
            if page_num == 1:
                log.info("  Pager HTML: %s", pager_info[:200])

            # Check for any clickable next button
            next_clicked = await page.evaluate("""
                () => {
                    // Try every possible next-page button pattern
                    var candidates = document.querySelectorAll(
                        '[title*="Next"], [aria-label*="Next"], [class*="next"]:not([class*="disabled"]), ' +
                        '.jsgrid-pager a, #resultsActionBar button, #resultsActionBar a'
                    );
                    for (var i = 0; i < candidates.length; i++) {
                        var el = candidates[i];
                        var txt = (el.textContent || el.title || el.getAttribute('aria-label') || '').toLowerCase();
                        var disabled = el.disabled || el.classList.contains('disabled') ||
                                       el.getAttribute('aria-disabled') === 'true';
                        if (!disabled && (txt.includes('next') || txt === '>' || txt === '>>' || txt === 'next page')) {
                            el.click();
                            return 'clicked: ' + (el.title || el.textContent || 'element');
                        }
                    }
                    return null;
                }
            """)

            if not next_clicked:
                log.info("  No more pages (tried all next-button patterns).")
                break

            log.info("  Next page: %s", next_clicked)
            await asyncio.sleep(2)
            page_num += 1
            if page_num > 100:
                log.warning("  Hit 100-page safety cap.")
                break

        html = await page.content()
        (ROOT / "data" / f"debug_{cat}_results.html").write_text(html, encoding="utf-8")
        return self._parse_rows(all_rows, cat)

    async def _extract_rows(self, page):
        return await page.evaluate("""
            () => {
                var out = [];
                document.querySelectorAll('#jsgrid-table tr').forEach(function(tr) {
                    var cells = tr.querySelectorAll('td');
                    if (cells.length < 2) return;
                    var row = [];
                    cells.forEach(function(td) {
                        var a = td.querySelector('a[href]:not([href="javascript:;"])');
                        row.push({ text: td.innerText.trim(), href: a ? a.href : '' });
                    });
                    out.push(row);
                });
                return out;
            }
        """)

    def _parse_rows(self, rows, cat):
        out, seen = [], set()
        for row in rows:
            if len(row) <= COL_DOCNUM:
                continue
            owner   = row[COL_OWNER]["text"].strip()
            grantee = row[COL_GRANTEE]["text"].strip()
            filed   = row[COL_DATE]["text"].strip()
            dtype   = row[COL_DTYPE]["text"].strip()
            legal   = row[COL_LEGAL]["text"].strip()
            doc_num = row[COL_DOCNUM]["text"].strip()
            book    = row[COL_BOOK]["text"].strip()
            page_n  = row[COL_PAGE]["text"].strip()
            if not owner and not doc_num:
                continue
            if not doc_num and book and page_n:
                doc_num = f"BK{book}-PG{page_n}"
            if doc_num in seen:
                continue
            if doc_num:
                seen.add(doc_num)
            final_cat = infer_cat(dtype) if dtype else cat
            out.append({
                "doc_num": doc_num, "doc_type": dtype, "filed": filed,
                "cat": final_cat, "cat_label": DOC_CATS.get(final_cat, dtype),
                "owner": owner, "grantee": grantee, "amount": None, "legal": legal,
                "prop_address": "", "prop_city": "", "prop_state": "FL", "prop_zip": "",
                "mail_address": "", "mail_city": "", "mail_state": "FL", "mail_zip": "",
                "clerk_url": f"{BASE_URL}?instrument={doc_num}" if doc_num else BASE_URL,
                "flags": [], "score": 30,
            })
        return out


# ============================================================
# OUTPUT
# ============================================================
def split_name(full):
    clean = re.sub(r'\b(JR\.?|SR\.?|II|III|IV|EST\.?|LLC|INC\.?|CORP\.?|TRUSTEE)\b',
                   '', full, flags=re.I).strip().rstrip(",").strip()
    if "," in clean:
        last, first = clean.split(",", 1)
        return first.strip(), last.strip()
    parts = clean.split()
    return (parts[0], " ".join(parts[1:])) if len(parts) >= 2 else ("", clean)

def save_output(records, start_mdy, end_mdy):
    today  = datetime.now().strftime("%Y-%m-%d")
    scored = [score_record(r) for r in records]
    with_a = sum(1 for r in scored if r.get("prop_address") or r.get("mail_address"))
    payload = {
        "fetched_at": datetime.now().isoformat(),
        "source": "Hillsborough County Clerk of Court",
        "date_range": {"from": start_mdy, "to": end_mdy},
        "total": len(scored), "with_address": with_a, "records": scored,
    }
    for p in [ROOT/"dashboard"/"records.json", ROOT/"data"/"records.json"]:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        log.info("Saved >> %s", p)
    cols = ["First Name","Last Name","Mailing Address","Mailing City","Mailing State","Mailing Zip",
            "Property Address","Property City","Property State","Property Zip",
            "Lead Type","Document Type","Date Filed","Document Number",
            "Amount/Debt Owed","Seller Score","Motivated Seller Flags","Source","Public Records URL"]
    csv_p = ROOT / "data" / f"leads_{today}.csv"
    with open(csv_p, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in scored:
            fn, ln = split_name(r.get("owner",""))
            w.writerow({
                "First Name": fn, "Last Name": ln,
                "Mailing Address": r.get("mail_address",""), "Mailing City": r.get("mail_city",""),
                "Mailing State": r.get("mail_state","FL"), "Mailing Zip": r.get("mail_zip",""),
                "Property Address": r.get("prop_address",""), "Property City": r.get("prop_city",""),
                "Property State": r.get("prop_state","FL"), "Property Zip": r.get("prop_zip",""),
                "Lead Type": r.get("cat_label",""), "Document Type": r.get("doc_type",""),
                "Date Filed": r.get("filed",""), "Document Number": r.get("doc_num",""),
                "Amount/Debt Owed": r.get("amount",""), "Seller Score": r.get("score",0),
                "Motivated Seller Flags": "; ".join(r.get("flags",[])),
                "Source": "Hillsborough County Clerk of Court",
                "Public Records URL": r.get("clerk_url",""),
            })
    log.info("CSV  >> %s  (%d rows)", csv_p, len(scored))
    return payload

async def main():
    end_dt    = datetime.now()
    start_dt  = end_dt - timedelta(days=LOOKBACK_DAYS)
    start_mdy = start_dt.strftime("%m/%d/%Y")
    end_mdy   = end_dt.strftime("%m/%d/%Y")
    log.info("=" * 56)
    log.info("Hillsborough County Motivated Seller Lead Scraper")
    log.info("Look-back : %d days  (%s to %s)", LOOKBACK_DAYS, start_mdy, end_mdy)
    log.info("=" * 56)

    scraper = HillsboroughScraper(start_mdy, end_mdy)
    records = await scraper.run()
    log.info("Clerk records: %d", len(records))

    if records:
        records = enrich_with_pa(records)

    payload = save_output(records, start_mdy, end_mdy)
    log.info("=" * 56)
    log.info("Total        : %d", payload["total"])
    log.info("With address : %d", payload["with_address"])
    if payload["records"]:
        hot  = sum(1 for r in payload["records"] if r.get("score",0) >= 70)
        warm = sum(1 for r in payload["records"] if 50 <= r.get("score",0) < 70)
        log.info("Hot  (>=70)  : %d", hot)
        log.info("Warm (50-69) : %d", warm)
    log.info("Dashboard >> python -m http.server 8000 --directory dashboard")
    log.info("=" * 56)

if __name__ == "__main__":
    asyncio.run(main())
