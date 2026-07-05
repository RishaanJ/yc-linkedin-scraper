#!/usr/bin/env python3
import argparse
import csv
import html
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote_plus, urlencode, urlparse
from urllib.request import Request, urlopen
from urllib.parse import quote





BATCHES = [
    "Winter 2027",
    "Fall 2026",
    "Summer 2026",
    "Spring 2026",
    "Fall 2025",
]

DEFAULT_URL = (
    "https://www.ycombinator.com/companies?"
    + "&".join(f"batch={quote(batch)}" for batch in BATCHES)
)
ALGOLIA_INDEX = "YCCompany_production"
BASE_URL = "https://www.ycombinator.com"
USER_AGENT = "yc-linkedin-scraper/1.0 (+https://www.ycombinator.com/companies)"


def fetch_text(url, *, method="GET", data=None, headers=None, retries=3, timeout=30):
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")

    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/json",
    }
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    if headers:
        request_headers.update(headers)

    last_error = None
    for attempt in range(1, retries + 1):
        try:
            req = Request(url, data=body, headers=request_headers, method=method)
            with urlopen(req, timeout=timeout) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                return response.read().decode(charset, errors="replace")
        except (HTTPError, URLError, TimeoutError) as exc:
            last_error = exc
            if attempt == retries:
                break
            time.sleep(0.75 * attempt)
    raise last_error


def get_algolia_options(directory_url):
    page = fetch_text(directory_url)
    match = re.search(r"window\.AlgoliaOpts\s*=\s*(\{.*?\});", page)
    if not match:
        raise RuntimeError("Could not find window.AlgoliaOpts on the YC directory page")
    return json.loads(match.group(1))


def batches_from_url(directory_url):
    query = parse_qs(urlparse(directory_url).query)
    return query.get("batch", [])


def algolia_filter_for_batches(batches):
    if not batches:
        return ""
    parts = [f'batch:"{batch.replace(chr(34), r"\"")}"' for batch in batches]
    return "(" + " OR ".join(parts) + ")"


def algolia_query(app_id, api_key, batches, page, hits_per_page):
    endpoint = f"https://{app_id}-dsn.algolia.net/1/indexes/{ALGOLIA_INDEX}/query"
    params = {
        "query": "",
        "page": page,
        "hitsPerPage": hits_per_page,
    }
    batch_filter = algolia_filter_for_batches(batches)
    if batch_filter:
        params["filters"] = batch_filter

    headers = {
        "X-Algolia-Application-Id": app_id,
        "X-Algolia-API-Key": api_key,
    }
    text = fetch_text(
        endpoint,
        method="POST",
        data={"params": urlencode(params)},
        headers=headers,
    )
    return json.loads(text)


def fetch_companies(app_id, api_key, batches, limit=None):
    companies = []
    page = 0
    hits_per_page = 100

    while True:
        payload = algolia_query(app_id, api_key, batches, page, hits_per_page)
        hits = payload.get("hits", [])
        companies.extend(hits)

        if limit and len(companies) >= limit:
            return companies[:limit], payload.get("nbHits")
        if page + 1 >= payload.get("nbPages", 0) or not hits:
            return companies, payload.get("nbHits")
        page += 1


def extract_company_payload(page_html):
    for raw_attr in re.findall(r'data-page="([^"]+)"', page_html):
        try:
            payload = json.loads(html.unescape(raw_attr))
        except json.JSONDecodeError:
            continue
        company = payload.get("props", {}).get("company")
        if company:
            return company
    raise ValueError("Could not find company payload")


FOUNDER_ANCHOR_RE = re.compile(
    r'<a\s[^>]*?href="([^"]*linkedin\.com/in/[^"]*)"[^>]*?founder-social-tooltip'
    r'|founder-social-tooltip[^>]*?href="([^"]*linkedin\.com/in/[^"]*)"'
)


def extract_founders(company, page_html):
    founders = []
    for founder in company.get("founders") or []:
        founders.append(
            {
                "founder_name": founder.get("full_name") or founder.get("name") or "",
                "founder_title": founder.get("title") or "",
                "founder_linkedin_url": normalize_linkedin_url(founder.get("linkedin_url")),
            }
        )

    if any(f["founder_linkedin_url"] for f in founders):
        return founders

    # Fallback: pull LinkedIn hrefs from the founder social anchors in the HTML.
    fallback = []
    for match in FOUNDER_ANCHOR_RE.finditer(page_html):
        url = normalize_linkedin_url(html.unescape(match.group(1) or match.group(2)))
        if url and url not in [f["founder_linkedin_url"] for f in fallback]:
            fallback.append(
                {"founder_name": "", "founder_title": "", "founder_linkedin_url": url}
            )
    return fallback or founders


def normalize_linkedin_url(raw_url):
    raw_url = (raw_url or "").strip()
    if not raw_url:
        return ""

    parsed = urlparse(raw_url)
    host = parsed.netloc.lower()
    if host == "linkedin.com" or host.endswith(".linkedin.com"):
        return raw_url

    # Some YC profiles contain a copied Google redirect URL. Recover the target
    # when it points to LinkedIn, but keep the raw value for auditability.
    redirected = parse_qs(parsed.query).get("url", [""])[0].strip()
    redirected_host = urlparse(redirected).netloc.lower()
    if redirected_host == "linkedin.com" or redirected_host.endswith(".linkedin.com"):
        return redirected

    return ""


def scrape_linkedin(company):
    slug = company["slug"]
    yc_url = f"{BASE_URL}/companies/{quote_plus(slug)}"
    row = {
        "name": company.get("name", ""),
        "batch": company.get("batch", ""),
        "slug": slug,
        "yc_url": yc_url,
        "linkedin_url": "",
        "raw_linkedin_url": "",
        "founders": [],
        "status": "ok",
        "error": "",
    }

    try:
        page = fetch_text(yc_url)
        payload = extract_company_payload(page)
        row["raw_linkedin_url"] = payload.get("linkedin_url") or ""
        row["linkedin_url"] = normalize_linkedin_url(row["raw_linkedin_url"])
        row["founders"] = extract_founders(payload, page)
    except Exception as exc:
        row["status"] = "error"
        row["error"] = f"{type(exc).__name__}: {exc}"
    return row


def write_outputs(rows, output_csv):
    output_csv = Path(output_csv).expanduser().resolve()
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    output_json = output_csv.with_suffix(".json")

    fieldnames = [
        "name",
        "batch",
        "slug",
        "yc_url",
        "linkedin_url",
        "raw_linkedin_url",
        "status",
        "error",
    ]
    with output_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    with output_json.open("w", encoding="utf-8") as handle:
        json.dump(rows, handle, indent=2, ensure_ascii=False)
        handle.write("\n")

    founders_csv = output_csv.with_name(output_csv.stem + "_founders.csv")
    founder_fieldnames = [
        "company_name",
        "batch",
        "slug",
        "yc_url",
        "founder_name",
        "founder_title",
        "founder_linkedin_url",
    ]
    with founders_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=founder_fieldnames)
        writer.writeheader()
        for row in rows:
            for founder in row["founders"]:
                writer.writerow(
                    {
                        "company_name": row["name"],
                        "batch": row["batch"],
                        "slug": row["slug"],
                        "yc_url": row["yc_url"],
                        **founder,
                    }
                )

    return output_csv, output_json, founders_csv


def parse_args():
    parser = argparse.ArgumentParser(
        description="Scrape LinkedIn URLs for YC companies in one or more batches."
    )
    parser.add_argument("--url", default=DEFAULT_URL, help="YC companies URL with batch filters")
    parser.add_argument(
        "--batch",
        action="append",
        dest="batches",
        help="Batch name, e.g. 'Summer 2026'. Can be supplied multiple times. Overrides --url batches.",
    )
    parser.add_argument(
        "--out",
        default="yc_company_linkedin.csv",
        help="Output CSV path. A JSON file with the same basename is also written.",
    )
    parser.add_argument("--workers", type=int, default=12, help="Concurrent company page fetches")
    parser.add_argument("--limit", type=int, help="Only process the first N companies")
    return parser.parse_args()


def main():
    args = parse_args()
    batches = args.batches if args.batches else batches_from_url(args.url)
    if not batches:
        print("No batches found. Pass --batch or use a YC URL with batch= filters.", file=sys.stderr)
        return 2

    print(f"Reading YC directory for batches: {', '.join(batches)}")
    algolia = get_algolia_options(args.url)
    companies, expected_count = fetch_companies(
        algolia["app"],
        algolia["key"],
        batches,
        limit=args.limit,
    )
    print(f"Found {len(companies)} companies" + (f" ({expected_count} matching total)" if expected_count else ""))

    rows = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = [executor.submit(scrape_linkedin, company) for company in companies]
        for index, future in enumerate(as_completed(futures), start=1):
            row = future.result()
            rows.append(row)
            founder_links = sum(1 for f in row["founders"] if f["founder_linkedin_url"])
            marker = row["linkedin_url"] or row["status"]
            print(f"[{index}/{len(companies)}] {row['name']} -> {marker} ({founder_links} founder links)")

    rows.sort(key=lambda row: (row["batch"], row["name"].lower()))
    csv_path, json_path, founders_path = write_outputs(rows, args.out)

    missing = sum(1 for row in rows if not row["linkedin_url"])
    errors = sum(1 for row in rows if row["status"] != "ok")
    founder_links = sum(
        1 for row in rows for f in row["founders"] if f["founder_linkedin_url"]
    )
    print(f"Wrote {csv_path}")
    print(f"Wrote {json_path}")
    print(f"Wrote {founders_path}")
    print(f"Founder LinkedIn links: {founder_links}")
    print(f"Companies without LinkedIn: {missing}; fetch errors: {errors}")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
