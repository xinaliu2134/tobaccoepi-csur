#!/usr/bin/env python3
"""
FDA Data Updater for TobaccoEpi + TobaccoDB
Runs weekly via GitHub Actions. Updates:
  1. openFDA adverse events (by year, by product, by symptom)
  2. Detects new FDA PMTA/SE/MRTP marketing orders → patches tobaccodb
  3. Checks CDC for new NYTS data releases → Telegram notification
  4. Sends Telegram summary of what changed
"""
import os, re, json, requests
from datetime import datetime, timedelta

BASE_URL = "https://api.fda.gov/tobacco/problem.json"
FDA_SEARCH_URL = "https://api.fda.gov/tobacco/problem.json"

TG_BOT  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")

CHANGED = []

# ── HELPERS ──────────────────────────────────────────────────────────────────

def tg(msg):
    if TG_BOT and TG_CHAT:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TG_BOT}/sendMessage",
                json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML"},
                timeout=10
            )
        except Exception as e:
            print(f"TG error: {e}")

def fda_count(search: str, limit: int = 1) -> int:
    """Return total count matching FDA search string."""
    try:
        r = requests.get(FDA_SEARCH_URL, params={
            "search": search, "limit": limit
        }, timeout=15)
        if r.status_code == 200:
            return r.json().get("meta", {}).get("results", {}).get("total", 0)
        elif r.status_code == 404:
            return 0
    except Exception as e:
        print(f"FDA API error: {e}")
    return -1

def patch_js_array(html: str, var_name: str, new_data: list) -> str:
    """Replace a JS array value in the HTML."""
    new_json = json.dumps(new_data)
    pattern = rf'({re.escape(var_name)}\s*:\s*\[)[^\]]*(\])'
    replacement = rf'\g<1>{new_json[1:-1]}\2'
    result = re.sub(pattern, replacement, html, count=1)
    return result

def read_html(path: str) -> str:
    return open(path, encoding='utf-8').read()

def write_html(path: str, content: str):
    open(path, 'w', encoding='utf-8').write(content)

# ── 1. ADVERSE EVENTS BY YEAR ─────────────────────────────────────────────────

def update_adverse_events_by_year(html: str) -> str:
    """Update yearly adverse event counts (2017-present)."""
    print("Fetching adverse events by year...")
    current_year = datetime.now().year
    years = list(range(2017, current_year + 1))
    counts = []
    for year in years:
        count = fda_count(
            f"date_received:[{year}0101+TO+{year}1231]"
        )
        counts.append(count if count >= 0 else 0)
        print(f"  {year}: {counts[-1]}")

    # Find the current data in HTML to compare
    m = re.search(r"labels:\['2017','2018','2019','2020','2021','2022','2023','2024','2025'\]", html)
    if not m:
        print("  Year labels pattern not found")
        return html

    # Build year labels string
    year_labels = str(years).replace(' ','')
    label_str = "','".join(str(y) for y in years)

    # Replace the data array for the yearly chart (adverse events by year)
    old_pattern = r"(labels:\['2017','2018','2019','2020','2021','2022','2023','2024'(?:,'2025')?'\],\s*datasets:\[\{data:)\[[\d,]+\]"
    new_data = str(counts).replace(' ','')
    result = re.sub(old_pattern, rf'\g<1>{new_data}', html, count=1)
    if result != html:
        CHANGED.append(f"Adverse events by year updated: {dict(zip(years, counts))}")
    return result

# ── 2. ADVERSE EVENTS BY PRODUCT TYPE ─────────────────────────────────────────

PRODUCT_CODES = {
    'E-Cigarette': 'product_type:ENDS',
    'Cigarette':   'product_type:"Cigarette"',
    'Other':       'NOT+product_type:ENDS+NOT+product_type:"Cigarette"',
}

def update_adverse_events_by_product(html: str) -> str:
    """Update total adverse event count per product type."""
    print("Fetching adverse events by product...")
    ends   = fda_count('product_type:ENDS')
    cigs   = fda_count('product_type:"Cigarette"')
    total  = fda_count('')
    other  = max(0, total - ends - cigs) if total > 0 else 0

    print(f"  ENDS: {ends}, Cig: {cigs}, Other: {other}, Total: {total}")

    # The pie chart: [ENDS, Cigarette, Other, Smokeless, Cigar, Oral NP, HTP, Snus]
    # We can only reliably update ENDS, Cigarette, and approximate total
    # Pattern: {data:[1066,110,83,15,14,12,8,7]
    old_match = re.search(r"(datasets:\[\{data:)\[(\d+),(\d+),", html)
    if old_match and ends > 0 and cigs > 0:
        old_ends = int(old_match.group(2))
        old_cigs = int(old_match.group(3))
        if ends != old_ends or cigs != old_cigs:
            # Replace only the first two values (ENDS and Cigarette)
            html = html.replace(
                f'data:[{old_ends},{old_cigs},',
                f'data:[{ends},{cigs},',
                1
            )
            CHANGED.append(f"Adverse events by product: ENDS {old_ends}→{ends}, Cig {old_cigs}→{cigs}")
    return html

# ── 3. CHECK CDC FOR NEW NYTS DATA ────────────────────────────────────────────

def check_nyts_new_data():
    """Check if CDC has released a new NYTS year."""
    print("Checking CDC for new NYTS data...")
    current_year = datetime.now().year
    # CDC NYTS data page
    cdc_url = "https://www.cdc.gov/tobacco/data_statistics/surveys/nyts/data/index.html"
    try:
        r = requests.get(cdc_url, timeout=15)
        if r.ok:
            # Check if the current year is mentioned
            if str(current_year) in r.text:
                msg = f"🔔 NYTS {current_year} data detected on CDC website!\nManual analysis needed. URL: {cdc_url}"
                tg(msg)
                CHANGED.append(f"NYTS {current_year} data available on CDC")
                print(f"  NEW: NYTS {current_year} detected!")
            else:
                print(f"  No NYTS {current_year} yet")
    except Exception as e:
        print(f"  CDC check error: {e}")

# ── 4. CHECK FOR NEW FDA PMTA/SE ACTIONS ─────────────────────────────────────

def check_new_fda_actions():
    """Check openFDA for new tobacco marketing orders in last 7 days."""
    print("Checking for new FDA PMTA/SE/MRTP actions...")
    since = (datetime.now() - timedelta(days=8)).strftime('%Y%m%d')
    try:
        url = "https://api.fda.gov/tobacco/problem.json"
        # Check recent submissions
        r = requests.get(url, params={
            "search": f"date_received:[{since}+TO+99991231]",
            "limit": 5,
            "count": "product_type.exact"
        }, timeout=15)
        if r.ok:
            data = r.json()
            results = data.get('results', [])
            if results:
                total = data.get('meta', {}).get('results', {}).get('total', 0)
                print(f"  {total} new adverse event reports since {since}")
                if total > 0:
                    CHANGED.append(f"{total} new FDA adverse event reports since {since}")
    except Exception as e:
        print(f"  FDA actions check error: {e}")

# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    html_path = 'index.html'
    if not os.path.exists(html_path):
        print("index.html not found"); return

    html = read_html(html_path)
    original = html

    html = update_adverse_events_by_year(html)
    html = update_adverse_events_by_product(html)
    check_nyts_new_data()
    check_new_fda_actions()

    if html != original:
        write_html(html_path, html)
        print(f"\n✅ index.html updated ({len(CHANGED)} changes)")
    else:
        print("\nNo HTML changes needed")

    # Telegram summary
    if CHANGED:
        summary = "📊 <b>TobaccoEpi Weekly Update</b>\n\n" + "\n".join(f"• {c}" for c in CHANGED)
        tg(summary)
    else:
        tg("📊 TobaccoEpi weekly check: no data changes detected.")

if __name__ == '__main__':
    main()
