# bina-az-mcp

An MCP server for house hunting in Azerbaijan from Claude Code. It has two data sources:

- **bina.az**: live listings via the GraphQL endpoint the website uses (`https://bina.az/graphql`). This API is unofficial and needs no key.
- **bina.al**: fair-value analytics, price history and market history via the official API (`https://bina.al/api/v1`, [docs](https://bina.al/en/developers)). It needs a Pro API key in `BINA_AL_API_KEY`.

Listing ids are the same on both sites.

## Tools

### bina.az (no key)

| Tool | What it does |
|---|---|
| `search_listings` | Search live listings with filters. |
| `get_listing` | Full details for one listing: description, map, photos, contact. |
| `market_stats` | Price and price-per-m² quartiles from a sample of listings. |
| `find_locations` | Look up district, metro and settlement names and ids. |

### bina.al (Pro key)

| Tool | What it does |
|---|---|
| `find_deals` | Search where every listing carries a fair price, a % vs fair value, its price change and days on market. Sorted by best deal. Filters include owner-only, a minimum discount and price-dropped. |
| `analyze_listing` | Fair value, full **price history**, days on market, active status and similar nearby listings. |
| `estimate_price` | Fair sale or rent price for a property you describe, with comparables. |
| `market_history` | Daily or weekly **market history** from 2026-09-27: median price per m², quartiles, active, new and removed listings, price cuts, days on market. Also a weekly trend for new listings. |
| `market_analytics` | Statistics across all active listings: overview KPIs, breakdowns by rooms, category, repair, deed, floor, agency or district, prices per location, rental yields, distribution. |
| `price_drops` | Listings whose price was cut or raised. Changes over 50% are hidden by default because they are usually typo fixes. |

## Setup

Requires [uv](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/beyrakIn/bina-az-mcp.git
cd bina-az-mcp
uv sync
claude mcp add bina-az --scope user -e BINA_AL_API_KEY=bi_live_... -- \
  uv run --directory "$(pwd)" python server.py
```

Leave out `-e BINA_AL_API_KEY=...` if you don't have a bina.al Pro key. The bina.az tools still work.

The key lives only in your Claude Code config (`~/.claude.json`), never in this repo.

## Notes

- bina.az returns at most 25 listings per page and bina.al at most 100. Paging is handled for you.
- bina.al market history is kept per category: new build, old build or house. `apartment` returns both building types. The daily history ignores the room filter; the weekly trend supports it.
- Fair values are statistical estimates, not appraisals.
