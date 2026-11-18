#!/usr/bin/env python3
"""Check company job boards for paid marketing internships and write docs/jobs.json.

Uses only the Python standard library, so GitHub Actions can run it with no installs.
Companies come from companies.json, plus a Notion database if NOTION_TOKEN and
NOTION_COMPANIES_DB are set.
"""

import html
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
COMPANIES_FILE = ROOT / "companies.json"
OUT_FILE = ROOT / "docs" / "jobs.json"

PAY_FLOOR = 20  # dollars per hour
HOURS_PER_YEAR = 2080
USER_AGENT = "marketing-jobs-feed/1.0 (+https://github.com/melbatost/marketing-jobs)"

INTERN_RE = re.compile(r"\b(intern|interns|internship|internships|co-?op)\b", re.I)
MARKETING_RE = re.compile(
    r"\b(marketing|marketer|brand|branding|growth|content|creative|social|"
    r"communications?|comms|community|partnerships?|influencer|creators?|"
    r"public relations|pr|campaigns?|go-to-market|gtm|lifecycle|"
    r"consumer insights?|editorial|copywrit\w*|merchandising|events?)\b",
    re.I,
)
MARKETING_DEPT_RE = re.compile(
    r"marketing|brand|communications|comms|growth|creative|content|social|community|partnerships",
    re.I,
)
TECH_RE = re.compile(
    r"\b(engineer\w*|software|developer|scientist|science|machine learning|ml|"
    r"security|hardware|firmware|infrastructure|data)\b",
    re.I,
)
SEASON_RE = re.compile(r"\b(summer|fall|autumn|spring|winter)\s*'?(20\d\d|\d\d)\b", re.I)
REMOTE_ONLY_TOKENS = {
    "remote", "anywhere", "united states", "usa", "us", "u.s.", "north america",
    "remote - us", "remote, us", "remote (us)", "remote (united states)",
}
GRAD_RE = re.compile(r"\b(mba|phd|ph\.d|master'?s|graduate student)\b", re.I)
SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "CAD": "CA$", "AUD": "A$", "SGD": "S$", "JPY": "¥", "INR": "₹"}
HOURLY_RE = re.compile(
    r"\$\s?(\d{1,3}(?:\.\d{1,2})?)\s*(?:(?:-|–|—|to)\s*\$?\s?(\d{1,3}(?:\.\d{1,2})?))?\s*"
    r"(?:/\s*(?:hr|hour)|per\s+hour|an\s+hour|hourly)",
    re.I,
)


# ---------- helpers ----------

def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def fetch_json(url, data=None, headers=None):
    req_headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        req_headers.update(headers)
    body = json.dumps(data).encode() if data is not None else None
    req = Request(url, data=body, headers=req_headers, method="POST" if body else "GET")
    with urlopen(req, timeout=45) as resp:
        return json.loads(resp.read().decode("utf-8"))


def strip_html(text):
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def to_iso(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).replace(microsecond=0).isoformat()
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc).replace(microsecond=0).isoformat()
    except ValueError:
        return None


def is_marketing(title, departments):
    if not INTERN_RE.search(title):
        return False
    title_says_marketing = bool(MARKETING_RE.search(title))
    dept_says_marketing = any(MARKETING_DEPT_RE.search(d or "") for d in departments)
    if TECH_RE.search(title) and not re.search(r"marketing", title, re.I):
        return False
    return title_says_marketing or dept_says_marketing


def remote_only_from_location(location):
    if not location:
        return False
    if "remote" not in location.lower():
        return False
    parts = [p.strip().lower() for p in re.split(r"[,•;/|]| or ", location) if p.strip()]
    return all(p in REMOTE_ONLY_TOKENS or p.startswith("remote") for p in parts)


def season_of(*texts):
    for text in texts:
        m = SEASON_RE.search(text or "")
        if m:
            year = m.group(2)
            year = year if len(year) == 4 else "20" + year
            return f"{m.group(1).capitalize()} {year}"
    return None


def pay_from_range(lo, hi, interval, currency="USD"):
    """Return (min_hourly, max_hourly, label, currency) from a numeric range and an interval word."""
    if lo is None and hi is None:
        return None
    lo = lo if lo is not None else hi
    hi = hi if hi is not None else lo
    interval = (interval or "").lower()
    if "hour" in interval:
        factor, unit = 1, "hr"
    elif "week" in interval:
        factor, unit = 1 / 40, "wk"
    elif "month" in interval:
        factor, unit = 12 / HOURS_PER_YEAR, "mo"
    elif "year" in interval or "annual" in interval or "salary" in interval:
        factor, unit = 1 / HOURS_PER_YEAR, "yr"
    else:
        factor, unit = (1, "hr") if hi < 300 else (1 / HOURS_PER_YEAR, "yr")
    currency = (currency or "USD").upper()
    sym = SYMBOLS.get(currency, currency + " ")
    lo_h, hi_h = round(lo * factor, 2), round(hi * factor, 2)
    if unit == "yr":
        label = f"{sym}{lo / 1000:.0f}k–{sym}{hi / 1000:.0f}k/yr" if lo != hi else f"{sym}{lo / 1000:.0f}k/yr"
    elif unit == "hr":
        label = f"{sym}{lo:g}–{sym}{hi:g}/hr" if lo != hi else f"{sym}{lo:g}/hr"
    else:
        label = f"{sym}{lo:,.0f}–{sym}{hi:,.0f}/{unit}" if lo != hi else f"{sym}{lo:,.0f}/{unit}"
    return lo_h, hi_h, label, currency


def pay_from_text(text):
    m = HOURLY_RE.search(text or "")
    if not m:
        return None
    lo = float(m.group(1))
    hi = float(m.group(2)) if m.group(2) else lo
    if lo < 5 or hi > 300:
        return None
    return pay_from_range(lo, hi, "hour")


def make_job(company, board, job_id, title, url, location, remote_only, posted, pay, season):
    job = {
        "id": f"{board}:{company['slug']}:{job_id}",
        "company": company["name"],
        "title": title.strip(),
        "url": url,
        "location": location or "",
        "remote_only": bool(remote_only),
        "posted_at": posted,
        "season": season,
        "tags": company.get("tags", []),
        "grad_only": bool(GRAD_RE.search(title)),
        "pay": None,
        "pay_currency": None,
        "pay_min_hourly": None,
        "pay_max_hourly": None,
        "under_floor": False,
    }
    if pay:
        job["pay_min_hourly"], job["pay_max_hourly"], job["pay"], job["pay_currency"] = pay
        job["under_floor"] = pay[3] == "USD" and pay[1] < PAY_FLOOR
    return job


# ---------- job boards ----------

def from_greenhouse(company):
    url = f"https://boards-api.greenhouse.io/v1/boards/{company['slug']}/jobs?content=true&pay_transparency=true"
    data = fetch_json(url)
    jobs = []
    for j in data.get("jobs", []):
        title = j.get("title") or ""
        depts = [d.get("name", "") for d in j.get("departments") or []]
        if not is_marketing(title, depts):
            continue
        content = strip_html(j.get("content"))
        pay = None
        for r in j.get("pay_input_ranges") or []:
            lo, hi = r.get("min_cents"), r.get("max_cents")
            if lo is None and hi is None:
                continue
            interval = (r.get("title") or "") + " " + strip_html(r.get("blurb"))[:200]
            pay = pay_from_range(lo / 100 if lo is not None else None, hi / 100 if hi is not None else None, interval, r.get("currency_type"))
            break
        pay = pay or pay_from_text(content)
        location = (j.get("location") or {}).get("name", "")
        jobs.append(make_job(
            company, "greenhouse", j.get("id"), title, j.get("absolute_url"), location,
            remote_only_from_location(location),
            to_iso(j.get("first_published") or j.get("updated_at")),
            pay, season_of(title, content[:2000]),
        ))
    return jobs


def from_lever(company):
    url = f"https://api.lever.co/v0/postings/{company['slug']}?mode=json"
    data = fetch_json(url)
    jobs = []
    for j in data if isinstance(data, list) else []:
        title = j.get("text") or ""
        cats = j.get("categories") or {}
        commitment = cats.get("commitment") or ""
        depts = [cats.get("team") or "", cats.get("department") or ""]
        check_title = title if INTERN_RE.search(title) or not re.search(r"intern", commitment, re.I) else f"{title} intern"
        if not is_marketing(check_title, depts):
            continue
        sr = j.get("salaryRange") or {}
        pay = pay_from_range(sr.get("min"), sr.get("max"), sr.get("interval"), sr.get("currency")) if sr else None
        pay = pay or pay_from_text(j.get("salaryDescriptionPlain") or "") or pay_from_text(j.get("descriptionPlain") or "")
        location = cats.get("location") or ", ".join(cats.get("allLocations") or [])
        workplace = (j.get("workplaceType") or "").lower()
        remote_only = workplace == "remote" if workplace else remote_only_from_location(location)
        jobs.append(make_job(
            company, "lever", j.get("id"), title, j.get("hostedUrl"), location, remote_only,
            to_iso(j.get("createdAt")), pay, season_of(title, (j.get("descriptionPlain") or "")[:2000]),
        ))
    return jobs


def from_ashby(company):
    url = f"https://api.ashbyhq.com/posting-api/job-board/{company['slug']}?includeCompensation=true"
    data = fetch_json(url)
    jobs = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        title = j.get("title") or ""
        depts = [j.get("department") or "", j.get("team") or ""]
        check_title = title if INTERN_RE.search(title) or j.get("employmentType") != "Intern" else f"{title} intern"
        if not is_marketing(check_title, depts):
            continue
        comp = j.get("compensation") or {}
        pay = None
        for c in comp.get("summaryComponents") or []:
            if c.get("minValue") is None and c.get("maxValue") is None:
                continue
            if (c.get("compensationType") or "").lower() in ("salary", "hourly", ""):
                pay = pay_from_range(c.get("minValue"), c.get("maxValue"), c.get("interval"), c.get("currencyCode"))
                break
        pay = pay or pay_from_text(comp.get("compensationTierSummary") or "") or pay_from_text(comp.get("scrapeableCompensationSalarySummary") or "")
        pay = pay or pay_from_text(j.get("descriptionPlain") or "")
        locations = [j.get("location") or ""] + [s.get("location", "") for s in j.get("secondaryLocations") or []]
        location = " • ".join(l for l in locations if l)
        workplace = (j.get("workplaceType") or "").lower()
        remote_only = workplace == "remote" if workplace else bool(j.get("isRemote")) and remote_only_from_location(location)
        jobs.append(make_job(
            company, "ashby", j.get("id"), title, j.get("jobUrl"), location, remote_only,
            to_iso(j.get("publishedAt")), pay, season_of(title, (j.get("descriptionPlain") or "")[:2000]),
        ))
    return jobs


BOARDS = {"greenhouse": from_greenhouse, "lever": from_lever, "ashby": from_ashby}


# ---------- company list ----------

def notion_companies():
    """Read extra companies from a Notion database, if configured.

    The database needs a title property (company name), a select property called
    "Job board" (Greenhouse, Lever or Ashby) and a text property called "Board slug".
    An optional multi-select called "Feed tags" can hold b2b or b2b-finance.
    """
    token, db = os.environ.get("NOTION_TOKEN"), os.environ.get("NOTION_COMPANIES_DB")
    if not token or not db:
        return []
    headers = {"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28", "Content-Type": "application/json"}
    out, cursor = [], None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        data = fetch_json(f"https://api.notion.com/v1/databases/{db}/query", data=body, headers=headers)
        for page in data.get("results", []):
            props = page.get("properties", {})
            name = next(("".join(t.get("plain_text", "") for t in p.get("title", [])) for p in props.values() if p.get("type") == "title"), "")
            board = ((props.get("Job board") or {}).get("select") or {}).get("name", "")
            slug = "".join(t.get("plain_text", "") for t in (props.get("Board slug") or {}).get("rich_text", []))
            tags = [o.get("name") for o in (props.get("Feed tags") or {}).get("multi_select", [])]
            if name and board.lower() in BOARDS and slug.strip():
                out.append({"name": name.strip(), "board": board.lower(), "slug": slug.strip(), "tags": tags})
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return out


def load_companies():
    listed = json.loads(COMPANIES_FILE.read_text()).get("companies", [])
    errors = []
    try:
        from_notion = notion_companies()
    except Exception as e:  # keep the feed running if Notion is down or misconfigured
        from_notion = []
        errors.append({"company": "Notion company list", "error": str(e)[:200]})
    seen, merged = set(), []
    for c in listed + from_notion:
        key = (c["board"].lower(), c["slug"].lower())
        if key in seen:
            continue
        seen.add(key)
        merged.append({**c, "board": c["board"].lower()})
    return merged, errors


# ---------- main ----------

def check(company):
    try:
        return BOARDS[company["board"]](company), None
    except HTTPError as e:
        return [], {"company": company["name"], "error": f"HTTP {e.code}"}
    except (URLError, TimeoutError, ValueError) as e:
        return [], {"company": company["name"], "error": str(e)[:200]}


def main():
    companies, errors = load_companies()
    previous = {}
    if OUT_FILE.exists():
        try:
            previous = {j["id"]: j.get("first_seen") for j in json.loads(OUT_FILE.read_text()).get("jobs", [])}
        except (ValueError, KeyError):
            previous = {}

    stamp = now_iso()
    jobs = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for found, err in pool.map(check, companies):
            jobs.extend(found)
            if err:
                errors.append(err)

    unique = {}
    for j in jobs:
        j["first_seen"] = previous.get(j["id"]) or stamp
        unique[j["id"]] = j
    jobs = sorted(unique.values(), key=lambda j: (j["first_seen"], j["posted_at"] or ""), reverse=True)

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps({
        "updated_at": stamp,
        "pay_floor": PAY_FLOOR,
        "companies_checked": len(companies),
        "errors": errors,
        "jobs": jobs,
    }, indent=1, ensure_ascii=False) + "\n")
    print(f"{len(jobs)} roles from {len(companies)} companies, {len(errors)} errors")
    for e in errors:
        print(f"  ! {e['company']}: {e['error']}", file=sys.stderr)


if __name__ == "__main__":
    main()
