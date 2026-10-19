# tradingdata-ai

Trading data that AI assistants can query directly, as a REST API plus an MCP
server:

- **ASX short positions:** every daily ASIC report since June 2010.
- **US insider trades:** every SEC Form 3, 4 and 5 since January 2006, updated daily.

**Live:** https://tradingdata-ai.fly.dev (Fly.io app `tradingdata-ai`, Sydney).

Built as a cheap demand test: if people pay for this, add director trades
(Appendix 3Y) and substantial-holder notices next.

## Data and licence

- **Source:** [ASIC short position reports](https://asic.gov.au/regulatory-resources/markets/short-selling/short-position-reports-table/), one CSV per trading day.
- **Licence:** ASIC publishes short selling position reporting data under
  **CC BY 4.0**, which allows reproduction "for sale or for free"
  ([ASIC copyright page](https://www.asic.gov.au/about-asic/dealing-with-asic/copyright-and-linking-to-our-websites/)).
  Every response carries the required attribution:
  `© Australian Securities & Investments Commission: Reproduced with permission.`
  Don't use ASIC's logo, and don't imply ASIC endorses the service.
- **Dates:** each file is named by its **position date**. ASIC publishes it
  about four trading days later (on Mon 19 Oct 2026 the newest file was Tue 13 Oct).
- **Gaps:** ASIC has no report for three ordinary trading days (4 Sep 2013,
  9 Sep 2014, 1 Dec 2014). It answers some missing dates with an empty file
  instead of a 404; both cases are recorded as "no report".
- **Cleaning:**
  - Before mid-2022 the files are UTF-16 and tab-separated; after that they're
    UTF-8 and comma-separated.
  - ASIC's own "% short" column has used different scaling over time, so
    `short_pct` is always recomputed as `short_positions / shares_on_issue`, as
    a fraction (0.05 = 5%).
  - Rows where the short count exceeds shares on issue, or the share count is
    zero, are dropped.

## US insider trades (SEC Forms 3, 4, 5)

- **History:** the SEC's quarterly
  [Insider Transactions Data Sets](https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets)
  (2006 onwards). The download links are read from that page, because the SEC
  has moved the files between folders.
- **Daily:** EDGAR's daily form index, plus each new filing's ownership XML.
  Numbers are rounded to 2 decimals because the quarterly files are, so a trade
  stays identical when its quarterly row replaces the daily one. Tested on 50
  real filings across Forms 3, 4 and 5: identical.
- **Keys:** a filing by its accession number; a trade by (accession, derivative,
  position in the filing).
- **Not stored:** footnotes (bulky legal text) and insiders' street addresses.
- **Buying screens:** count open-market purchases only (code P on shares).
  Grants (A), tax withholding (F) and option exercises (M) aren't decisions to
  buy. A filing can list several insiders for one trade, for example a fund and
  its general partner, so values are summed per trade, never per owner.
- **Licence:** EDGAR is US government-published and freely reusable. Set
  `SEC_USER_AGENT="name contact@domain"`; the SEC asks for ≤10 requests per
  second, and the loader paces itself at 8.

```sh
uv run sec-insiders-ingest                    # all quarters + recent daily filings
uv run sec-insiders-ingest --quarters 2026q2  # one quarter
```

## Run locally

```sh
uv sync
uv run asx-shorts-ingest --since 2010-06-16   # full backfill (~4,100 files)
uv run asx-shorts-ingest                       # daily catch-up afterwards
uv run asx-shorts-keys issue "me@example.com" --tier pro
uv run asx-shorts-serve --port 8000            # http://127.0.0.1:8000  (docs at /docs)
uv run pytest
```

## API

Auth is `X-API-Key: asxs_…` or `Authorization: Bearer asxs_…`.

| Endpoint | |
|---|---|
| `GET /v1/coverage` | Public. Loaded date range and counts |
| `GET /v1/products?q=` | Search by code or name |
| `GET /v1/short-positions/{code}?from=&to=&format=json\|csv` | Daily history for one code |
| `GET /v1/most-shorted?date=&limit=&min_shares_on_issue=` | Most shorted on a date |
| `GET /v1/changes?reports_back=5&direction=up\|down&limit=` | Biggest moves in short %, in percentage points |

| Plan | History | Requests/day |
|---|---|---|
| free | last 90 days | 250 |
| pro | full history from 2010 | 20,000 |

Plans live in `src/asx_shorts/keys.py`. There's no billing yet: issue pro
keys by hand until demand is proven.

## MCP

**Remote:** `https://tradingdata-ai.fly.dev/mcp/` with header `Authorization: Bearer asxs_…`.
Tools: `search_products`, `short_history`, `most_shorted`, `short_changes`,
`data_coverage`. Each caller's plan limits apply, exactly as in the REST API.

**Local (Claude Desktop / Claude Code):** runs against your own database over
stdio, with no key:

```sh
claude mcp add asx-shorts -- uv --directory /path/to/asx-shorts run asx-shorts-mcp
```

## Deploy

Production runs on Fly.io as app `tradingdata-ai` in Sydney (`fly.toml`): one
always-on shared-cpu-1x/512 MB machine with a 3 GB encrypted volume at `/data`.
The daily update runs inside the app, because a Fly volume attaches to only one
machine.

```sh
fly deploy -a tradingdata-ai          # build on Fly and roll out
fly logs -a tradingdata-ai            # includes the daily ingest log
fly ssh console -a tradingdata-ai -C "setpriv --reuid=app --regid=app --init-groups \
  /app/.venv/bin/asx-shorts-keys issue someone@example.com --tier pro"
```

Issue keys as the `app` user, as above. Run as root, SQLite can leave
root-owned journal files that the app can't write.

SQLite needs a persistent disk, so if you move hosts, use a small VM or a
platform with volumes (Fly.io, Railway, Render), not serverless.

- `Dockerfile` serves the API on port 8000 and reads the database from
  `/data/asx_shorts.sqlite` (mount a volume there).
- Set `ASX_SHORTS_HOSTS=your.domain`. The MCP endpoint rejects unknown `Host`
  headers as DNS-rebinding protection.
- Run `asx-shorts-ingest` daily, e.g. 20:00 AEST, through cron or the
  platform's scheduler. It refetches the last 14 days so late files are picked up.

## Layout

```
src/asx_shorts/asic.py        download + parse both ASIC formats
src/asx_shorts/ingest.py      incremental loader (CLI)
src/asx_shorts/db.py          SQLite schema
src/asx_shorts/queries.py     shared read queries
src/asx_shorts/keys.py        API keys, plans, daily limits (CLI)
src/asx_shorts/api.py         FastAPI REST + /mcp mount + auth
src/asx_shorts/mcp_server.py  MCP tools (remote and stdio)
src/asx_shorts/landing.html   landing/docs page
```
