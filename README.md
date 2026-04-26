# Hillsborough County Motivated Seller Lead Scraper

Pulls Lis Pendens, Tax Deed Notices, Judgments, Liens, and Probate filings
from the Hillsborough County Clerk portal. Scores leads 0–100 and outputs
a filterable dashboard + CSV.

---

## QUICK START (Local Test)

### Step 1 — Install Python (if you don't have it)
Download from https://python.org/downloads — get version 3.11 or newer.

### Step 2 — Open a terminal in this folder
- **Mac**: right-click the folder → "New Terminal at Folder"
- **Windows**: hold Shift + right-click → "Open PowerShell window here"

### Step 3 — Install dependencies
```
pip install -r scraper/requirements.txt
python -m playwright install chromium
```

### Step 4 — Run in debug mode (browser window opens so you can watch)
```
HEADLESS=false python scraper/fetch.py
```
On Windows:
```
set HEADLESS=false && python scraper/fetch.py
```

### Step 5 — Open the dashboard
Open `dashboard/index.html` in Chrome or Firefox.

---

## WHAT GETS CREATED

After running, you'll have:
- `dashboard/records.json` — all leads (loaded by the dashboard)
- `data/records.json` — backup copy
- `data/leads_YYYY-MM-DD.csv` — ready to upload to REISift/DataSift
- `data/scraper.log` — full run log
- `data/debug_home.html` — what the portal looked like (for debugging)
- `data/debug_home.png` — screenshot of the portal
- `data/debug_*.html` — search result pages for each doc type

---

## TROUBLESHOOTING

**Got 0 records?**
1. Open `data/debug_home.html` in your browser — this shows exactly what
   the scraper saw when it hit the portal.
2. Share that file back to Claude and say "fix the scraper based on this"
3. Claude will map the correct field names and fix it.

**Portal requires login or CAPTCHA?**
Let Claude know — we'll add a manual-login step before the automated search.

**Want more days back?**
```
LOOKBACK_DAYS=30 python scraper/fetch.py
```

---

## AUTOMATED DAILY RUNS (GitHub Actions)

1. Create a free GitHub account at github.com
2. Create a new repository (can be private)
3. Upload this entire folder to it
4. Go to Settings → Pages → Source: "GitHub Actions"
5. The scraper runs automatically every day at 8 AM ET
6. Your dashboard will be live at: https://YOUR-USERNAME.github.io/YOUR-REPO/

---

## FILE STRUCTURE

```
hillsborough-scraper/
├── scraper/
│   ├── fetch.py           ← main scraper
│   └── requirements.txt   ← Python packages
├── dashboard/
│   ├── index.html         ← lead viewer dashboard
│   └── records.json       ← lead data (updated by scraper)
├── data/
│   ├── records.json       ← backup copy
│   ├── leads_*.csv        ← CSV exports
│   └── scraper.log        ← run logs
└── .github/
    └── workflows/
        └── scrape.yml     ← daily automation
```
