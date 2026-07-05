# YC LinkedIn Scraper

Scrapes LinkedIn URLs for Y Combinator companies and their founders, filtered by batch. Uses YC's public Algolia search index to list companies, then fetches each company page to extract the company LinkedIn URL and founder LinkedIn profiles.

No third-party dependencies — just Python 3 and the standard library. **No API key needed** — see [How it works](#how-it-works).

## Usage

```bash
# Default batches (Winter 2027, Fall 2026, Summer 2026, Spring 2026, Fall 2025)
python3 yc_linkedin_scraper.py

# Specific batch(es)
python3 yc_linkedin_scraper.py --batch "Summer 2026" --batch "Fall 2025"

# Or paste a YC directory URL with batch filters
python3 yc_linkedin_scraper.py --url "https://www.ycombinator.com/companies?batch=Summer%202026"
```

### Changing the default batches

To change which batches run by default (i.e. with no `--batch`/`--url` flags), edit the `BATCHES` list at the top of `yc_linkedin_scraper.py`:

```python
BATCHES = [
    "Winter 2027",
    "Fall 2026",
    "Summer 2026",
    "Spring 2026",
    "Fall 2025",
]
```

Batch names must match YC's directory exactly (e.g. `"Summer 2026"`).

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--batch` | — | Batch name, e.g. `"Summer 2026"`. Repeatable. Overrides batches in `--url`. |
| `--url` | YC directory URL with default batches | YC companies URL; batches are read from its `batch=` query params. |
| `--out` | `yc_company_linkedin.csv` | Output CSV path. JSON and founders CSV use the same basename. |
| `--workers` | `12` | Concurrent company page fetches. |
| `--limit` | — | Only process the first N companies. |

## Output

Three files are written per run (basename taken from `--out`):

- **`yc_company_linkedin.csv`** — one row per company: name, batch, slug, YC URL, LinkedIn URL, raw LinkedIn URL, status, error. See [sample.csv](sample.csv).
- **`yc_company_linkedin.json`** — same data plus nested founder details. See [sample.json](sample.json).
- **`yc_company_linkedin_founders.csv`** — one row per founder: company, founder name, title, and founder LinkedIn URL.

## How it works

1. Fetches the YC companies directory page and reads the Algolia app ID/API key from `window.AlgoliaOpts`. **This is why no API key setup is required**: YC's directory is a client-side search app, so it embeds a public, search-only Algolia key in the page for browsers to use. The script picks it up fresh on every run, so it keeps working even if YC rotates the key.
2. Queries the `YCCompany_production` Algolia index with a batch filter to enumerate companies.
3. Fetches each company page concurrently and parses the embedded `data-page` JSON payload for the company LinkedIn URL and founder profiles, falling back to scraping founder social anchors from the HTML.
4. Normalizes LinkedIn URLs (including unwrapping Google redirect URLs found on some profiles) and drops non-LinkedIn links.

Failed fetches are retried with backoff; per-company errors are recorded in the `status`/`error` columns rather than aborting the run. Exit code is `0` when all companies scraped cleanly, `1` if any errored.
