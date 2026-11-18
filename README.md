# Marketing internships

Live page: https://melbatost.github.io/marketing-jobs/

A job board for paid marketing internships. It checks company job boards every four hours and lists marketing, brand, growth, content, creative, communications and community internships on one page.

## How it works

- `companies.json` lists the companies to check and which job board each one uses (Greenhouse, Lever or Ashby).
- `scripts/fetch_jobs.py` reads each board's public job feed, keeps internships in marketing-type roles, and writes `docs/jobs.json`.
- A GitHub Action runs the script every four hours and saves the results.
- GitHub Pages serves `docs/index.html`, which shows the roles with filters.

Each listing links straight to the company's own application page.

## Add a company

1. Open the company's careers page and click into any job.
2. Look at the link:
   - `jobs.ashbyhq.com/SLUG/...` means `"board": "ashby"`
   - `boards.greenhouse.io/SLUG/...` or `job-boards.greenhouse.io/SLUG/...` means `"board": "greenhouse"`
   - `jobs.lever.co/SLUG/...` means `"board": "lever"`
3. Add a line to `companies.json`, like `{"name": "Figma", "board": "greenhouse", "slug": "figma"}`.

Add `"tags": ["b2b"]` or `"tags": ["b2b-finance"]` if you want the page to flag it.

Companies can also come from a Notion database. Add repository secrets named `NOTION_TOKEN` and `NOTION_COMPANIES_DB`. The database needs a "Job board" select and a "Board slug" text property.

## Filters

By default the page hides remote-only roles, roles paying under $20/hr, MBA and PhD roles, and B2B finance companies. Each filter can be switched off on the page.
