"""bina.az MCP server.

Search real-estate listings on bina.az (Azerbaijan) from Claude Code.
Talks to the GraphQL endpoint the bina.az website itself uses
(https://bina.az/graphql). That API is unofficial and may change.
"""

from __future__ import annotations

import asyncio
import logging
import re
import statistics
import sys
import unicodedata
from typing import Annotated, Any, Literal

import httpx
from mcp.server.fastmcp import FastMCP
from pydantic import Field

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("bina-az-mcp")

API_URL = "https://bina.az/graphql"
SITE = "https://bina.az"
USER_AGENT = "bina-az-mcp/0.1"
PAGE_SIZE = 25  # the API caps every page at 25 items
BAKU_ID = "1"

mcp = FastMCP("bina-az")

# --------------------------------------------------------------------------- #
# Static vocabularies (from bina.az's own front-end)
# --------------------------------------------------------------------------- #

CATEGORIES: dict[str, str] = {
    "apartment": "1",       # Mənzil (new + old buildings)
    "new_building": "2",    # Yeni tikili
    "old_building": "3",    # Köhnə tikili
    "house": "5",           # Həyət evi / Bağ evi
    "office": "7",          # Ofis
    "garage": "8",          # Qaraj
    "land": "9",            # Torpaq (area in sot)
    "commercial": "10",     # Obyekt
}

SORTS: dict[str, str] = {
    "newest": "BUMPED_AT_DESC",
    "price_asc": "PRICE_ASC",
    "price_desc": "PRICE_DESC",
    "area_asc": "AREA_ASC",
    "area_desc": "AREA_DESC",
    "price_per_m2_asc": "PRICE_PER_ARE_ASC",
    "price_per_m2_desc": "PRICE_PER_ARE_DESC",
}

LOCATION_GROUPS = {"1": "metro", "2": "district", "3": "settlement", "4": "landmark"}

Category = Literal["apartment", "new_building", "old_building", "house", "office", "garage", "land", "commercial"]
Deal = Literal["buy", "rent", "daily_rent"]
Sort = Literal["newest", "price_asc", "price_desc", "area_asc", "area_desc", "price_per_m2_asc", "price_per_m2_desc"]

# --------------------------------------------------------------------------- #
# GraphQL
# --------------------------------------------------------------------------- #

ITEM_ROW_FIELDS = """
  id path updatedAt rooms floor floors isVipped isFeatured
  area { value units }
  price { total currency }
  location { id name fullName }
  city { id name }
  company { name targetType }
  hasMortgage hasBillOfSale hasRepair isLeased isPaidDaily
"""

SEARCH_QUERY = f"""
query SearchItems($first: Int, $filter: ItemFilter, $sort: ItemConnectionSort!, $cursor: String) {{
  itemsConnection(first: $first, after: $cursor, filter: $filter, sort: $sort) {{
    totalCount
    pageInfo {{ hasNextPage endCursor }}
    edges {{ node {{ {ITEM_ROW_FIELDS} }} }}
  }}
}}
"""

COUNT_QUERY = """
query SearchTotalCount($filter: ItemFilter) { itemsConnection(filter: $filter) { totalCount } }
"""

ITEM_QUERY = """
query CurrentItem($id: ID!) {
  item(id: $id) {
    id path description address rooms floor floors
    area { value units } landArea { value units }
    price { total currency perAre }
    latitude longitude buildingTypeName contactName contactTypeName
    hasBillOfSale hasMortgage hasRepair isLeased isPaidDaily
    views updatedAt expiresAt isVipped isFeatured state
    category { name }
    city { name }
    location { fullName }
    company { name targetType }
    nearestLocations { ... on Location { fullName } ... on City { name } }
    additionalParameters { name }
    photos { full }
    phones { ... on MaskedPhone { value } ... on Phone { value } }
    business {
      ... on Agency { name path itemsCount }
      ... on Residence { companyName path deadline website }
    }
  }
}
"""

LOCATIONS_QUERY = """
query CityAndLocations($cityId: ID!) {
  locations(filter: { scope: ALL, cityId: $cityId }, limit: 1000) {
    id name fullName locationGroupId slug
  }
}
"""

CITIES_QUERY = "query { cities(filter: { scope: ALL }) { id name slug } }"


class BinaError(Exception):
    pass


_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0),
            headers={"user-agent": USER_AGENT, "content-type": "application/json", "accept": "application/json"},
        )
    return _client


async def gql(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {"query": query, "variables": variables or {}}
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            resp = await client().post(API_URL, json=payload)
            if resp.status_code >= 500:
                raise BinaError(f"bina.az returned HTTP {resp.status_code}")
            resp.raise_for_status()
            body = resp.json()
            if body.get("errors"):
                raise BinaError("; ".join(e.get("message", "unknown error") for e in body["errors"]))
            return body.get("data") or {}
        except (httpx.TransportError, BinaError) as exc:
            last_exc = exc
            if isinstance(exc, BinaError) and "HTTP 5" not in str(exc):
                break  # GraphQL validation errors won't fix themselves
            await asyncio.sleep(0.8)
        except httpx.HTTPStatusError as exc:
            raise BinaError(f"bina.az returned HTTP {exc.response.status_code}") from exc
    raise BinaError(str(last_exc))


# --------------------------------------------------------------------------- #
# Name resolution (cities & locations), diacritic-insensitive
# --------------------------------------------------------------------------- #

_TRANSLIT = str.maketrans({"ə": "e", "Ə": "e", "ı": "i", "I": "i", "İ": "i", "ş": "s", "Ş": "s",
                           "ç": "c", "Ç": "c", "ğ": "g", "Ğ": "g", "ö": "o", "Ö": "o", "ü": "u", "Ü": "u"})
_SUFFIX = re.compile(r"\s+(m|r|q|ş|s|k)\.?$")


def norm(s: str) -> str:
    s = s.strip().translate(_TRANSLIT).lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = _SUFFIX.sub("", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    # common English/Russian-style spellings
    s = s.replace("narimanov", "nerimanov").replace("kh", "x")
    return s.strip()


_cities: list[dict[str, Any]] | None = None
_locations: dict[str, list[dict[str, Any]]] = {}


async def get_cities() -> list[dict[str, Any]]:
    global _cities
    if _cities is None:
        _cities = (await gql(CITIES_QUERY))["cities"]
    return _cities


async def get_locations(city_id: str) -> list[dict[str, Any]]:
    if city_id not in _locations:
        _locations[city_id] = (await gql(LOCATIONS_QUERY, {"cityId": city_id}))["locations"] or []
    return _locations[city_id]


async def resolve_city(city: str | None) -> dict[str, Any]:
    if not city:
        city = BAKU_ID
    cities = await get_cities()
    if city.isdigit():
        for c in cities:
            if c["id"] == city:
                return c
        raise BinaError(f"Unknown city id {city}")
    q = norm(city)
    if q in ("baku", "baki"):
        q = "baki"
    exact = [c for c in cities if norm(c["name"]) == q or c["slug"] == q]
    if exact:
        return exact[0]
    partial = [c for c in cities if q in norm(c["name"])]
    if len(partial) == 1:
        return partial[0]
    hint = ", ".join(c["name"] for c in partial[:10]) or "none"
    raise BinaError(f"Could not resolve city '{city}'. Candidates: {hint}")


def _loc_out(loc: dict[str, Any]) -> dict[str, Any]:
    return {"id": loc["id"], "name": loc["fullName"], "type": LOCATION_GROUPS.get(str(loc["locationGroupId"]), "other")}


async def resolve_locations(names: list[str], city_id: str) -> tuple[list[str], list[dict[str, Any]]]:
    locs = await get_locations(city_id)
    ids: list[str] = []
    used: list[dict[str, Any]] = []
    for raw in names:
        raw = str(raw).strip()
        if raw.isdigit():
            hit = [l for l in locs if l["id"] == raw]
            if not hit:
                raise BinaError(f"Unknown location id {raw} for this city")
        else:
            q = norm(raw)
            hit = [l for l in locs if norm(l["name"]) == q or norm(l["fullName"]) == q]
            if not hit:
                partial = [l for l in locs if q in norm(l["name"])]
                if len(partial) == 1:
                    hit = partial
                else:
                    cands = ", ".join(f"{l['fullName']} (id {l['id']})" for l in partial[:12]) or "none"
                    raise BinaError(
                        f"Could not resolve location '{raw}'. Candidates: {cands}. "
                        "Use find_locations to look up the right name or id."
                    )
        for l in hit:
            if l["id"] not in ids:
                ids.append(l["id"])
                used.append(_loc_out(l))
    return ids, used


# --------------------------------------------------------------------------- #
# Filter building & formatting
# --------------------------------------------------------------------------- #

async def build_filter(
    *, city, deal, category, rooms, price_min, price_max, area_min, area_max,
    land_area_min, land_area_max, floor_min, floor_max, first_floor, top_floor,
    locations, repaired, has_bill_of_sale, mortgage_ready, only_residential_complexes,
) -> tuple[dict[str, Any], dict[str, Any]]:
    c = await resolve_city(city)
    f: dict[str, Any] = {"cityId": c["id"], "categoryId": CATEGORIES[category], "leased": deal != "buy"}
    if deal == "daily_rent":
        f["paidDaily"] = True
    if rooms:
        f["roomIds"] = sorted({("5+" if int(r) >= 5 else str(int(r))) for r in rooms})
    for key, val in (("priceFrom", price_min), ("priceTo", price_max), ("areaFrom", area_min), ("areaTo", area_max),
                     ("landAreaFrom", land_area_min), ("landAreaTo", land_area_max),
                     ("floorFrom", floor_min), ("floorTo", floor_max)):
        if val is not None:
            f[key] = int(val)
    # Tri-state flags: None = don't care. True = only. False = exclude.
    for key, val in (("floorFirst", first_floor), ("floorLast", top_floor), ("hasRepair", repaired),
                     ("hasBillOfSale", has_bill_of_sale), ("hasMortgage", mortgage_ready)):
        if val is not None:
            f[key] = bool(val)
    if only_residential_complexes:
        f["onlyResidences"] = True
    applied: dict[str, Any] = {"city": c["name"], "deal": deal, "category": category}
    if locations:
        ids, used = await resolve_locations(locations, c["id"])
        f["locationIds"] = ids
        applied["locations"] = used
    return f, applied


def _num(v: Any) -> Any:
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def price_per_unit(price: Any, area: Any) -> int | None:
    try:
        if price and area and float(area) > 0:
            return round(float(price) / float(area))
    except (TypeError, ValueError):
        pass
    return None


def seller_of(node: dict[str, Any]) -> str:
    comp = node.get("company") or {}
    if comp.get("targetType") == "AGENCY":
        return f"agency: {comp.get('name')}" if comp.get("name") else "agency"
    return "owner"


def row(node: dict[str, Any]) -> dict[str, Any]:
    price = (node.get("price") or {}).get("total")
    area = (node.get("area") or {}).get("value")
    units = (node.get("area") or {}).get("units") or ""
    r: dict[str, Any] = {
        "id": node["id"],
        "url": SITE + (node.get("path") or f"/items/{node['id']}"),
        "price": _num(price),
        "currency": (node.get("price") or {}).get("currency"),
        "area": f"{_num(area)} {units}".strip() if area is not None else None,
        f"price_per_{'sot' if units == 'sot' else 'm2'}": price_per_unit(price, area),
        "rooms": node.get("rooms"),
        "floor": f"{node['floor']}/{node['floors']}" if node.get("floor") else None,
        "location": (node.get("location") or {}).get("fullName"),
        "repaired": node.get("hasRepair"),
        "bill_of_sale": node.get("hasBillOfSale"),
        "mortgage": node.get("hasMortgage"),
        "seller": seller_of(node),
        "updated": (node.get("updatedAt") or "")[:10],
    }
    if node.get("isPaidDaily"):
        r["daily"] = True
    return {k: v for k, v in r.items() if v is not None}


# Shared, annotated parameter types so both search tools have the same schema.
P_City = Annotated[str | None, Field(description="City name or id, e.g. 'Bakı', 'Baku', 'Sumqayıt'. Default: Bakı.")]
P_Deal = Annotated[Deal, Field(description="buy = for sale (alqı-satqı), rent = monthly rent, daily_rent = daily rent.")]
P_Category = Annotated[Category, Field(description="apartment = all flats (new+old); new_building = yeni tikili; old_building = köhnə tikili; house = həyət evi/bağ evi; office; garage; land (area in sot); commercial = obyekt.")]
P_Rooms = Annotated[list[int] | None, Field(description="Room counts to include, e.g. [2, 3]. 5 means 5+.")]
P_Int = Annotated[int | None, Field(description="Optional bound.")]
P_Locations = Annotated[list[str] | None, Field(description="Districts, settlements, metro stations or landmarks (names or ids), OR-combined. E.g. ['Yasamal', 'Nərimanov m.', '28 May']. Diacritics optional. Use find_locations if unsure.")]
P_Tri = Annotated[bool | None, Field(description="true = only these, false = exclude these, omit = don't care.")]


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #

@mcp.tool()
async def find_locations(
    query: Annotated[str | None, Field(description="Part of a name, e.g. 'yasamal', 'gənclik', 'nizami'. Omit to list all.")] = None,
    city: P_City = None,
    type: Annotated[Literal["metro", "district", "settlement", "landmark"] | None, Field(description="Restrict to one location type.")] = None,
) -> dict[str, Any]:
    """Look up bina.az location names/ids (districts, settlements, metro stations, landmarks) for a city.
    Use the returned names or ids in the `locations` parameter of search_listings / market_stats."""
    try:
        c = await resolve_city(city)
        locs = [_loc_out(l) for l in await get_locations(c["id"])]
        if query:
            q = norm(query)
            locs = [l for l in locs if q in norm(l["name"])]
        if type:
            locs = [l for l in locs if l["type"] == type]
        return {"city": c["name"], "count": len(locs), "locations": locs[:300]}
    except BinaError as e:
        return {"error": str(e)}


@mcp.tool()
async def search_listings(
    deal: P_Deal = "buy",
    category: P_Category = "apartment",
    city: P_City = None,
    locations: P_Locations = None,
    rooms: P_Rooms = None,
    price_min: Annotated[int | None, Field(description="Min total price (AZN).")] = None,
    price_max: Annotated[int | None, Field(description="Max total price (AZN).")] = None,
    area_min: Annotated[int | None, Field(description="Min area, m² (sot for land).")] = None,
    area_max: Annotated[int | None, Field(description="Max area, m² (sot for land).")] = None,
    land_area_min: Annotated[int | None, Field(description="Min plot size in sot (houses).")] = None,
    land_area_max: Annotated[int | None, Field(description="Max plot size in sot (houses).")] = None,
    floor_min: P_Int = None,
    floor_max: P_Int = None,
    first_floor: Annotated[bool | None, Field(description="false = exclude ground/1st floor, true = only 1st floor.")] = None,
    top_floor: Annotated[bool | None, Field(description="false = exclude top floor, true = only top floor.")] = None,
    repaired: Annotated[bool | None, Field(description="true = renovated (təmirli) only, false = unrenovated only.")] = None,
    has_bill_of_sale: Annotated[bool | None, Field(description="true = has kupça/çıxarış (ownership document) only.")] = None,
    mortgage_ready: Annotated[bool | None, Field(description="true = eligible for mortgage (ipoteka) only.")] = None,
    only_residential_complexes: Annotated[bool, Field(description="Only listings from residential complexes (yaşayış kompleksi).")] = False,
    sort: Annotated[Sort, Field(description="Result order.")] = "newest",
    limit: Annotated[int, Field(ge=1, le=100, description="How many listings to return (1-100).")] = 25,
    cursor: Annotated[str | None, Field(description="next_cursor from a previous call to get the next page.")] = None,
) -> dict[str, Any]:
    """Search bina.az listings with filters. Returns total match count, compact listing rows
    (price, area, price per m², rooms, floor, location, renovation, kupça, mortgage, owner/agency, link)
    and a next_cursor for more results. Use get_listing for full details of a listing."""
    try:
        f, applied = await build_filter(
            city=city, deal=deal, category=category, rooms=rooms, price_min=price_min, price_max=price_max,
            area_min=area_min, area_max=area_max, land_area_min=land_area_min, land_area_max=land_area_max,
            floor_min=floor_min, floor_max=floor_max, first_floor=first_floor, top_floor=top_floor,
            locations=locations, repaired=repaired, has_bill_of_sale=has_bill_of_sale,
            mortgage_ready=mortgage_ready, only_residential_complexes=only_residential_complexes,
        )
        # bina.az's server-side PRICE_PER_ARE sort only works for land (perAre is null for flats),
        # so for price-per-m² sorting we fetch a pool of the newest matches and sort locally.
        local_sort = sort.startswith("price_per_m2") and category != "land"
        if local_sort:
            pool_size = max(limit, LOCAL_SORT_POOL)
            rows, total, _, _ = await _fetch(f, "BUMPED_AT_DESC", pool_size, None)
            key = next(k for k in ("price_per_m2", "price_per_sot") if any(k in r for r in rows)) if rows else "price_per_m2"
            ranked = [r for r in rows if key in r]
            ranked.sort(key=lambda r: r[key], reverse=sort.endswith("desc"))
            out = {"filters": applied, "total_matches": total, "returned": min(limit, len(ranked)),
                   "listings": ranked[:limit]}
            if total > len(rows):
                out["note"] = (f"Sorted by price per m² among the {len(rows)} newest of {total} matches "
                               "(bina.az can't sort flats by price per m² server-side). Narrow filters for full coverage.")
            return out

        rows, total, has_next, end = await _fetch(f, SORTS[sort], limit, cursor)
        out: dict[str, Any] = {"filters": applied, "total_matches": total, "returned": len(rows), "listings": rows}
        if has_next:
            out["next_cursor"] = end
        return out
    except BinaError as e:
        return {"error": str(e)}


LOCAL_SORT_POOL = 250


async def _fetch(f: dict[str, Any], sort: str, want: int, cursor: str | None):
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    total = 0
    has_next, end = False, cursor
    while len(rows) < want:
        first = min(PAGE_SIZE, want - len(rows))
        data = await gql(SEARCH_QUERY, {"first": first, "filter": f, "sort": sort, "cursor": end})
        conn = data["itemsConnection"]
        total = conn["totalCount"]
        for e in conn["edges"]:
            if e["node"]["id"] not in seen:
                seen.add(e["node"]["id"])
                rows.append(row(e["node"]))
        has_next = conn["pageInfo"]["hasNextPage"]
        end = conn["pageInfo"]["endCursor"]
        if not has_next or not conn["edges"]:
            break
        await asyncio.sleep(0.15)
    return rows, total, has_next, end


@mcp.tool()
async def get_listing(
    id_or_url: Annotated[str, Field(description="Listing id (e.g. '6139038') or a bina.az listing URL.")],
    max_photos: Annotated[int, Field(ge=0, le=50, description="How many photo URLs to include.")] = 5,
) -> dict[str, Any]:
    """Get full details of one bina.az listing: description, address, coordinates, map link, photos,
    contact name/type, masked phone, building/complex info, views and dates."""
    m = re.search(r"(\d{4,})", id_or_url)
    if not m:
        return {"error": f"No listing id found in '{id_or_url}'"}
    try:
        it = (await gql(ITEM_QUERY, {"id": m.group(1)})).get("item")
    except BinaError as e:
        return {"error": str(e)}
    if not it:
        return {"error": f"Listing {m.group(1)} not found (it may have been removed)."}
    price = (it.get("price") or {}).get("total")
    area = (it.get("area") or {}).get("value")
    units = (it.get("area") or {}).get("units") or ""
    lat, lng = it.get("latitude"), it.get("longitude")
    biz = it.get("business") or {}
    out: dict[str, Any] = {
        "id": it["id"],
        "url": SITE + (it.get("path") or f"/items/{it['id']}"),
        "state": it.get("state"),
        "category": (it.get("category") or {}).get("name"),
        "deal": ("daily rent" if it.get("isPaidDaily") else "rent") if it.get("isLeased") else "sale",
        "price": _num(price),
        "currency": (it.get("price") or {}).get("currency"),
        "area": f"{_num(area)} {units}".strip() if area is not None else None,
        f"price_per_{'sot' if units == 'sot' else 'm2'}": price_per_unit(price, area),
        "land_area": (f"{_num(it['landArea']['value'])} {it['landArea']['units']}" if it.get("landArea") else None),
        "rooms": it.get("rooms"),
        "floor": f"{it['floor']}/{it['floors']}" if it.get("floor") else None,
        "building_type": it.get("buildingTypeName"),
        "city": (it.get("city") or {}).get("name"),
        "location": (it.get("location") or {}).get("fullName"),
        "nearby": [n.get("fullName") or n.get("name") for n in it.get("nearestLocations") or []],
        "address": it.get("address"),
        "map": f"https://www.google.com/maps?q={lat},{lng}" if lat and lng else None,
        "repaired": it.get("hasRepair"),
        "bill_of_sale": it.get("hasBillOfSale"),
        "mortgage": it.get("hasMortgage"),
        "features": [p["name"] for p in it.get("additionalParameters") or []],
        "description": it.get("description"),
        "contact": it.get("contactName"),
        "contact_type": it.get("contactTypeName"),
        "phones_masked": [p.get("value") for p in it.get("phones") or []],
        "agency_or_complex": biz.get("name") or biz.get("companyName"),
        "complex_deadline": biz.get("deadline"),
        "views": it.get("views"),
        "updated": it.get("updatedAt"),
        "expires": it.get("expiresAt"),
        "photos": [p["full"] for p in (it.get("photos") or [])[:max_photos]],
        "photo_count": len(it.get("photos") or []),
        "note": "Phone numbers are masked by bina.az; open the url to reveal them.",
    }
    return {k: v for k, v in out.items() if v not in (None, [], "")}


def _q(values: list[float]) -> dict[str, int]:
    values = sorted(values)
    if not values:
        return {}
    if len(values) == 1:
        v = round(values[0])
        return {"min": v, "p25": v, "median": v, "p75": v, "max": v}
    qs = statistics.quantiles(values, n=4)
    return {"min": round(values[0]), "p25": round(qs[0]), "median": round(qs[1]),
            "p75": round(qs[2]), "max": round(values[-1])}


@mcp.tool()
async def market_stats(
    deal: P_Deal = "buy",
    category: P_Category = "apartment",
    city: P_City = None,
    locations: P_Locations = None,
    rooms: P_Rooms = None,
    price_min: P_Int = None,
    price_max: P_Int = None,
    area_min: P_Int = None,
    area_max: P_Int = None,
    land_area_min: P_Int = None,
    land_area_max: P_Int = None,
    floor_min: P_Int = None,
    floor_max: P_Int = None,
    first_floor: P_Tri = None,
    top_floor: P_Tri = None,
    repaired: P_Tri = None,
    has_bill_of_sale: P_Tri = None,
    mortgage_ready: P_Tri = None,
    only_residential_complexes: bool = False,
    sample_size: Annotated[int, Field(ge=25, le=500, description="How many of the newest matching listings to analyse.")] = 200,
    group_by_location: Annotated[bool, Field(description="Also break down price per m² by location.")] = True,
) -> dict[str, Any]:
    """Price statistics for a segment of the bina.az market: total listings, and min / quartiles / median / max
    of price, area and price per m² over a sample of the newest matching listings, optionally per location.
    Useful to judge whether a listing is fairly priced or to compare neighbourhoods.
    Works without a bina.al key; when available, market_analytics covers ALL listings instead of a sample."""
    try:
        f, applied = await build_filter(
            city=city, deal=deal, category=category, rooms=rooms, price_min=price_min, price_max=price_max,
            area_min=area_min, area_max=area_max, land_area_min=land_area_min, land_area_max=land_area_max,
            floor_min=floor_min, floor_max=floor_max, first_floor=first_floor, top_floor=top_floor,
            locations=locations, repaired=repaired, has_bill_of_sale=has_bill_of_sale,
            mortgage_ready=mortgage_ready, only_residential_complexes=only_residential_complexes,
        )
        nodes: list[dict[str, Any]] = []
        total, end = 0, None
        while len(nodes) < sample_size:
            data = await gql(SEARCH_QUERY, {"first": PAGE_SIZE, "filter": f, "sort": "BUMPED_AT_DESC", "cursor": end})
            conn = data["itemsConnection"]
            total = conn["totalCount"]
            nodes.extend(e["node"] for e in conn["edges"])
            end = conn["pageInfo"]["endCursor"]
            if not conn["pageInfo"]["hasNextPage"] or not conn["edges"]:
                break
            await asyncio.sleep(0.2)
        nodes = nodes[:sample_size]
    except BinaError as e:
        return {"error": str(e)}

    seen: set[str] = set()
    prices, areas, ppm = [], [], []
    by_loc: dict[str, list[float]] = {}
    owners = 0
    for n in nodes:
        if n["id"] in seen:
            continue
        seen.add(n["id"])
        p = (n.get("price") or {}).get("total")
        a = (n.get("area") or {}).get("value")
        if p:
            prices.append(float(p))
        if a:
            areas.append(float(a))
        u = price_per_unit(p, a)
        if u:
            ppm.append(u)
            loc = (n.get("location") or {}).get("fullName") or "unknown"
            by_loc.setdefault(loc, []).append(u)
        if seller_of(n) == "owner":
            owners += 1

    unit = "sot" if any((n.get("area") or {}).get("units") == "sot" for n in nodes[:5]) else "m2"
    out: dict[str, Any] = {
        "filters": applied,
        "total_matches": total,
        "sampled": len(seen),
        "sample_note": "Sample = newest listings matching the filters." if len(seen) < total else "All matching listings analysed.",
        "price_AZN": _q(prices),
        f"area_{unit}": _q(areas),
        f"price_per_{unit}_AZN": _q(ppm),
        "owner_share": f"{round(100 * owners / len(seen))}%" if seen else None,
    }
    if group_by_location and by_loc:
        rows = [{"location": k, "n": len(v), f"median_price_per_{unit}": round(statistics.median(v))}
                for k, v in by_loc.items() if len(v) >= 3]
        rows.sort(key=lambda r: -r["n"])
        out["by_location"] = rows[:25]
    return out


import binaal  # noqa: E402  (bina.al analytics tools; need BINA_AL_API_KEY)

binaal.register(mcp, norm)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
