"""bina.al tools: fair-value analytics, price history and market history.

Uses the official bina.al REST API (https://bina.al/api/v1, docs: https://bina.al/en/developers),
which needs a Pro-plan API key in the BINA_AL_API_KEY environment variable.
Listing ids are the same as bina.az ids.
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime, timezone
from typing import Annotated, Any, Callable, Literal

import httpx
from pydantic import Field

API = "https://bina.al/api/v1"
USER_AGENT = "bina-az-mcp/0.1"

CATEGORY_IDS = {
    "apartment": "2,3", "new_building": "2", "old_building": "3", "house": "5",
    "office": "7", "garage": "8", "land": "9", "commercial": "10",
}
DEALS = {"buy": "sale", "rent": "rent", "daily_rent": "daily"}

Category = Literal["apartment", "new_building", "old_building", "house", "office", "garage", "land", "commercial"]
Deal = Literal["buy", "rent", "daily_rent"]


P_Deal = Annotated[Deal, Field(description="buy = for sale, rent = monthly rent, daily_rent = daily rent.")]
P_Cat = Annotated[Category, Field(description="apartment = new + old buildings; new_building; old_building; house; office; garage; land; commercial.")]
P_City = Annotated[str | None, Field(description="City name or id. Default Bakı.")]
P_Dist = Annotated[list[str] | None, Field(description="Baku districts (rayon), e.g. ['Yasamal', 'Nərimanov', 'Nəsimi']. Diacritics optional.")]
P_Loc = Annotated[list[str] | None, Field(description="Metro stations, settlements or landmarks (names or ids), e.g. ['Elmlər Akademiyası', 'İnşaatçılar', '28 May'].")]
P_Rooms = Annotated[list[int] | None, Field(description="Room counts, e.g. [2, 3]. 5 means 5+.")]


class BinaAlError(Exception):
    pass


_client: httpx.AsyncClient | None = None
_meta: dict[str, Any] | None = None
_locs: dict[str, list[dict[str, Any]]] = {}


def _key() -> str:
    k = os.environ.get("BINA_AL_API_KEY", "").strip()
    if not k:
        raise BinaAlError(
            "BINA_AL_API_KEY is not set. bina.al tools need a Pro API key "
            "(https://bina.al/en/developers). Re-register the server with -e BINA_AL_API_KEY=..."
        )
    return k


async def get(path: str, params: dict[str, Any] | None = None) -> Any:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(base_url=API, timeout=httpx.Timeout(40.0),
                                    headers={"user-agent": USER_AGENT, "accept": "application/json"})
    q = {k: (str(v).lower() if isinstance(v, bool) else v) for k, v in (params or {}).items() if v is not None}
    try:
        r = await _client.get(path, params=q, headers={"X-API-Key": _key()})
    except httpx.TransportError as e:
        raise BinaAlError(f"bina.al unreachable: {e}") from e
    if r.status_code >= 400:
        try:
            err = r.json().get("error", {})
            msg = f"{err.get('code')}: {err.get('message')}"
        except Exception:
            msg = r.text[:200]
        raise BinaAlError(f"bina.al HTTP {r.status_code} — {msg}")
    return r.json()


# --------------------------------------------------------------------------- #
# Name resolution
# --------------------------------------------------------------------------- #

async def meta() -> dict[str, Any]:
    global _meta
    if _meta is None:
        _meta = await get("/meta")
    return _meta


def make_resolvers(norm: Callable[[str], str]):
    async def city_id(city: str | None) -> str | None:
        if not city:
            return None
        if city.isdigit():
            return city
        q = norm(city)
        q = "baki" if q == "baku" else q
        cities = (await meta()).get("cities", [])
        hit = [c for c in cities if norm(c["name"]) == q] or [c for c in cities if q in norm(c["name"])]
        if len(hit) != 1:
            raise BinaAlError(f"Could not resolve city '{city}'. Candidates: "
                              + (", ".join(c["name"] for c in hit[:10]) or "none"))
        return hit[0]["id"]

    async def districts(names: list[str] | None) -> str | None:
        if not names:
            return None
        known = [d["name"] for d in (await meta()).get("districts", [])]
        out = []
        for n in names:
            q = norm(n)
            hit = [d for d in known if norm(d) == q] or [d for d in known if norm(d).startswith(q)]
            if len(hit) != 1:
                raise BinaAlError(f"Unknown district '{n}'. Baku districts: {', '.join(known)}")
            out.append(hit[0])
        return ",".join(out)

    async def locations(names: list[str] | None, cid: str | None) -> tuple[str | None, list[str]]:
        if not names:
            return None, []
        key = cid or "1"
        if key not in _locs:
            _locs[key] = (await get("/locations", {"city": key})).get("data", [])
        locs = _locs[key]
        ids, used = [], []
        for raw in names:
            raw = str(raw).strip()
            if raw.isdigit():
                ids.append(raw)
                used.append(raw)
                continue
            q = norm(raw)
            hit = [l for l in locs if norm(l["name"]) == q]
            if not hit:
                part = [l for l in locs if q in norm(l["name"])]
                if len(part) == 1:
                    hit = part
                else:
                    raise BinaAlError(
                        f"Could not resolve location '{raw}'. Candidates: "
                        + (", ".join(f"{l['name']} ({l['kind']}, {l['district']}, id {l['id']})" for l in part[:12]) or "none")
                    )
            for l in hit:
                if l["id"] not in ids:
                    ids.append(l["id"])
                    used.append(f"{l['name']} ({l['kind']}, {l['district']})")
        return ",".join(ids), used

    return city_id, districts, locations


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #

def _r(x: Any, nd: int = 0) -> Any:
    if isinstance(x, (int, float)):
        return round(x, nd) if nd else int(round(x))
    return x


def _pct(x: Any) -> str | None:
    if isinstance(x, (int, float)):
        return f"{x * 100:+.0f}%"
    return None


def _days_since(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).days
    except ValueError:
        return None


def listing_row(x: dict[str, Any]) -> dict[str, Any]:
    fair = x.get("fair") or {}
    r: dict[str, Any] = {
        "id": x.get("id"),
        "url": x.get("url"),
        "price": _r(x.get("priceAzn") or x.get("price")),
        "currency": "AZN",
        "area": f"{_r(x.get('area'), 1) if x.get('area') else None} {x.get('areaUnits') or ''}".strip() if x.get("area") else None,
        "ppu": _r(x.get("ppu")),
        "rooms": x.get("rooms"),
        "floor": f"{x['floor']}/{x['floors']}" if x.get("floor") else None,
        "category": x.get("categoryEn") or x.get("category"),
        "district": x.get("district"),
        "location": x.get("location"),
        "complex": x.get("residenceName"),
        "repaired": x.get("hasRepair"),
        "deed": x.get("hasBillOfSale"),
        "mortgage": x.get("hasMortgage"),
        "seller": "agency" if x.get("isAgency") else "owner",
    }
    if fair:
        r["fair_price"] = _r(fair.get("price"))
        r["fair_range"] = [_r(fair.get("low")), _r(fair.get("high"))]
        r["vs_fair"] = _pct(fair.get("score"))
        r["fair_basis"] = f"{fair.get('basis')} ({fair.get('comps')} comps)"
        if fair.get("suspicious"):
            r["suspicious"] = "more than 40% below fair value: often a typo, a share, or bait"
    if x.get("priceChange"):
        r["price_change"] = _pct(x["priceChange"])
        r["first_price"] = _r(x.get("firstPriceAzn"))
        if abs(x["priceChange"]) >= 0.5:
            r["change_note"] = "change of 50%+ is often a typo correction, not a real cut"
    if x.get("firstSeen"):
        r["first_seen"] = x["firstSeen"][:10]
        r["days_listed"] = _days_since(x["firstSeen"])
    return {k: v for k, v in r.items() if v not in (None, "", [])}


def stats_row(s: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for src, dst in (("n", "n"), ("medianPpu", "median_ppu"), ("p25Ppu", "p25_ppu"), ("p75Ppu", "p75_ppu"),
                     ("medianPrice", "median_price"), ("medianArea", "median_area")):
        if s.get(src) is not None:
            out[dst] = _r(s[src])
    return out


def _week_key(day: str) -> tuple[int, int]:
    y, w, _ = date.fromisoformat(day).isocalendar()
    return y, w


# --------------------------------------------------------------------------- #
# Tool registration
# --------------------------------------------------------------------------- #

def register(mcp, norm: Callable[[str], str]) -> None:
    city_id, districts_of, locations_of = make_resolvers(norm)

    async def common(deal, category, city, districts, locations, rooms) -> tuple[dict[str, Any], dict[str, Any]]:
        cid = await city_id(city)
        dist = await districts_of(districts)
        loc, used = await locations_of(locations, cid)
        p = {"deal": DEALS[deal], "category": CATEGORY_IDS[category], "city": cid, "district": dist, "location": loc,
             "rooms": ",".join(sorted({"5+" if int(r) >= 5 else str(int(r)) for r in rooms})) if rooms else None}
        applied = {k: v for k, v in {"deal": deal, "category": category, "city": city, "districts": dist,
                                      "locations": used or None, "rooms": p["rooms"]}.items() if v}
        return p, applied

    def listing_filters(price_min, price_max, area_min, area_max, ppu_max, floor_min, floor_max,
                        exclude_first_floor, exclude_top_floor, repaired, has_deed, mortgage_ready,
                        owner_only, min_discount, hide_suspicious, price_dropped, text) -> dict[str, Any]:
        return {"priceMin": price_min, "priceMax": price_max, "areaMin": area_min, "areaMax": area_max,
                "ppuMax": ppu_max, "floorMin": floor_min, "floorMax": floor_max,
                "notFirst": True if exclude_first_floor else None, "notTop": True if exclude_top_floor else None,
                "repair": repaired, "deed": has_deed, "mortgage": mortgage_ready,
                "ownerOnly": True if owner_only else None, "minDiscount": min_discount,
                "hideSuspicious": hide_suspicious, "priceDropped": True if price_dropped else None, "q": text}

    # ---------------------------------------------------------------- #
    @mcp.tool()
    async def find_deals(
        deal: P_Deal = "buy",
        category: P_Cat = "apartment",
        city: P_City = None,
        districts: P_Dist = None,
        locations: P_Loc = None,
        rooms: P_Rooms = None,
        price_min: int | None = None,
        price_max: int | None = None,
        area_min: int | None = None,
        area_max: int | None = None,
        ppu_max: Annotated[int | None, Field(description="Max price per m² (AZN).")] = None,
        floor_min: int | None = None,
        floor_max: int | None = None,
        exclude_first_floor: bool = False,
        exclude_top_floor: bool = False,
        repaired: Annotated[bool | None, Field(description="true = renovated only, false = unrenovated only, omit = any.")] = None,
        has_deed: Annotated[bool | None, Field(description="true = has kupça (ownership deed).")] = None,
        mortgage_ready: Annotated[bool | None, Field(description="true = mortgage (ipoteka) eligible.")] = None,
        owner_only: Annotated[bool, Field(description="Exclude agency listings.")] = False,
        min_discount: Annotated[int | None, Field(description="Only listings at least this many % below their fair value.")] = None,
        hide_suspicious: Annotated[bool, Field(description="Hide listings >40% below fair value (usually typos/bait).")] = True,
        price_dropped: Annotated[bool, Field(description="Only listings whose price was cut.")] = False,
        text: Annotated[str | None, Field(description="Free-text search in location, district, title, address (e.g. a complex name).")] = None,
        sort: Annotated[Literal["deal", "newest", "price_asc", "price_desc", "ppu_asc", "area_desc", "drop"],
                        Field(description="deal = most below fair value first (default); ppu_asc = cheapest per m²; drop = biggest price cut.")] = "deal",
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        page: Annotated[int, Field(ge=1)] = 1,
    ) -> dict[str, Any]:
        """Smart search via bina.al: every listing comes with a comparable-based fair price, how far it is
        above/below fair value (vs_fair), price change since first seen and days on market.
        Default sort puts the best deals (most underpriced) first. Ids/urls are bina.az's."""
        try:
            p, applied = await common(deal, category, city, districts, locations, rooms)
            p |= listing_filters(price_min, price_max, area_min, area_max, ppu_max, floor_min, floor_max,
                                 exclude_first_floor, exclude_top_floor, repaired, has_deed, mortgage_ready,
                                 owner_only, min_discount, hide_suspicious, price_dropped, text)
            p |= {"sort": sort, "limit": limit, "page": page, "photos": 0}
            r = await get("/listings", p)
        except BinaAlError as e:
            return {"error": str(e)}
        return {"filters": applied, "total_matches": r.get("total"), "page": r.get("page"), "pages": r.get("pages"),
                "listings": [listing_row(x) for x in r.get("data", [])],
                "legend": "ppu = AZN per m²; vs_fair = price vs estimated fair price (negative = cheaper than market)."}

    # ---------------------------------------------------------------- #
    @mcp.tool()
    async def analyze_listing(
        id_or_url: Annotated[str, Field(description="bina.az / bina.al listing id or URL.")],
        include_similar: Annotated[bool, Field(description="Also return similar active listings nearby.")] = True,
        similar_count: Annotated[int, Field(ge=1, le=20)] = 6,
    ) -> dict[str, Any]:
        """Deep-dive one listing via bina.al: fair price and range, % vs fair value, full price history
        (every price change with dates), days on market, active/removed status, description and similar
        listings nearby for comparison."""
        m = re.search(r"(\d{4,})", id_or_url)
        if not m:
            return {"error": f"No listing id in '{id_or_url}'"}
        lid = m.group(1)
        try:
            x = await get(f"/listings/{lid}")
            sim = (await get(f"/listings/{lid}/similar")).get("data", []) if include_similar else []
        except BinaAlError as e:
            return {"error": str(e)}
        out = listing_row(x)
        out["bina_al"] = f"https://bina.al/en/listing/{lid}"
        out["active"] = x.get("active")
        out["last_seen"] = (x.get("lastSeen") or "")[:10] or None
        out["price_history"] = [{"date": (h.get("at") or "")[:10], "price": _r(h.get("priceAzn") or h.get("price"))}
                                for h in x.get("priceHistory") or []]
        d = x.get("detail") or {}
        if d.get("address"):
            out["address"] = d["address"]
        if d.get("description"):
            out["description"] = d["description"][:1500]
        if x.get("lat") and x.get("lon"):
            out["map"] = f"https://www.google.com/maps?q={x['lat']},{x['lon']}"
        if sim:
            out["similar"] = [listing_row(s) for s in sim[:similar_count]]
            ppus = sorted(s["ppu"] for s in sim if s.get("ppu"))
            if ppus and x.get("ppu"):
                med = ppus[len(ppus) // 2]
                out["vs_similar_median_ppu"] = f"{(x['ppu'] / med - 1) * 100:+.0f}% (similar median {round(med)} AZN/m², n={len(ppus)})"
        return {k: v for k, v in out.items() if v not in (None, "", [])}

    # ---------------------------------------------------------------- #
    @mcp.tool()
    async def estimate_price(
        area: Annotated[float, Field(description="Area in m².")],
        rooms: int,
        deal: P_Deal = "buy",
        category: Annotated[Literal["new_building", "old_building", "house"], Field()] = "new_building",
        district: Annotated[str | None, Field(description="Baku district, e.g. 'Yasamal'.")] = None,
        location: Annotated[str | None, Field(description="Metro/settlement/landmark name or id (more precise than district).")] = None,
        city: P_City = None,
        floor: int | None = None,
        floors: Annotated[int | None, Field(description="Total floors in the building.")] = None,
        repaired: bool = True,
        has_deed: bool = True,
        examples: Annotated[int, Field(ge=0, le=10, description="How many comparable listings to include.")] = 5,
    ) -> dict[str, Any]:
        """Estimate the fair market price (sale or rent) of a property from comparable bina.az listings,
        via bina.al. Returns fair price, low/high range, price per m², the comparison basis and examples."""
        try:
            cid = await city_id(city)
            dist = await districts_of([district]) if district else None
            loc, used = await locations_of([location] if location else None, cid)
            r = await get("/estimate", {"deal": DEALS[deal], "category": CATEGORY_IDS[category], "city": cid,
                                        "district": dist, "location": loc, "rooms": rooms, "area": area,
                                        "floor": floor, "floors": floors, "repair": repaired, "deed": has_deed})
        except BinaAlError as e:
            return {"error": str(e)}
        est = r.get("estimate") or {}
        if not est:
            return {"error": "No estimate: not enough comparable listings.", "input": r.get("input")}
        return {
            "input": {k: v for k, v in (r.get("input") or {}).items() if v is not None} | ({"location": used} if used else {}),
            "fair_price": _r(est.get("price")),
            "range": [_r(est.get("low")), _r(est.get("high"))],
            "ppu": _r(est.get("ppu")),
            "basis": f"{est.get('basis')} ({est.get('comps')} comparable listings)",
            "examples": [listing_row(x) for x in (r.get("examples") or [])[:examples]],
        }

    # ---------------------------------------------------------------- #
    @mcp.tool()
    async def market_history(
        deal: P_Deal = "buy",
        category: P_Cat = "apartment",
        city: P_City = None,
        district: Annotated[str | None, Field(description="One Baku district, e.g. 'Yasamal'. Omit for the whole city.")] = None,
        location: Annotated[str | None, Field(description="One metro/settlement/landmark (overrides district).")] = None,
        rooms: Annotated[list[int] | None, Field(description="Room counts; applies to the weekly new-listing trend only.")] = None,
        granularity: Annotated[Literal["daily", "weekly"], Field(description="weekly = one snapshot per week (compact).")] = "weekly",
    ) -> dict[str, Any]:
        """Price history of a market segment over time, via bina.al:
        (1) daily snapshots of all active listings: median / p25 / p75 price per m², median price, active count,
            new and removed listings, price cuts, average days on market, # below market;
        (2) weekly median price per m² of NEWLY listed properties (supports room filter).
        Includes the overall change from the first to the latest snapshot. Data starts 2026-09-27."""
        try:
            cid = await city_id(city)
            dist = await districts_of([district]) if district else None
            loc, used = await locations_of([location] if location else None, cid)
            cats = {"apartment": ["2", "3"]}.get(category, [CATEGORY_IDS[category]])
            base = {"deal": DEALS[deal], "city": cid, "district": None if loc else dist, "location": loc}
            series: dict[str, Any] = {}
            for c in cats:
                if c not in ("2", "3", "5"):
                    series[c] = {"note": "daily history is only kept for new_building, old_building and house"}
                    continue
                rows = (await get("/analytics/history", base | {"category": c})).get("data", [])
                if not rows:
                    continue
                if granularity == "weekly":
                    last: dict[tuple[int, int], dict] = {}
                    for x in rows:
                        last[_week_key(x["day"])] = x
                    picked = list(last.values())
                    window_new = {}
                    for x in rows[1:]:  # first day is the initial import, not real "new" listings
                        k = _week_key(x["day"])
                        w = window_new.setdefault(k, {"new": 0, "removed": 0, "cuts": 0})
                        w["new"] += x.get("newListings") or 0
                        w["removed"] += x.get("delisted") or 0
                        w["cuts"] += x.get("priceCuts") or 0
                else:
                    picked = rows
                    window_new = None
                pts = []
                for i, x in enumerate(picked):
                    pt = {"date": x["day"], "active": x.get("n"), "median_ppu": _r(x.get("medianPpu")),
                          "p25_ppu": _r(x.get("p25Ppu")), "p75_ppu": _r(x.get("p75Ppu")),
                          "median_price": _r(x.get("medianPrice")), "avg_days_on_market": _r(x.get("daysOnMarket"), 1),
                          "below_market": x.get("belowMarket")}
                    if window_new is not None:
                        w = window_new.get(_week_key(x["day"]), {})
                        pt |= {"new_in_week": w.get("new"), "removed_in_week": w.get("removed"), "price_cuts_in_week": w.get("cuts")}
                    elif i > 0:
                        pt |= {"new": x.get("newListings"), "removed": x.get("delisted"), "price_cuts": x.get("priceCuts")}
                    pts.append({k: v for k, v in pt.items() if v is not None})
                first, latest = rows[0], rows[-1]
                name = {"2": "new_building", "3": "old_building", "5": "house"}[c]
                series[name] = {
                    "scope": f"{latest.get('scope')}: {latest.get('name')}",
                    "change": {
                        "from": first["day"], "to": latest["day"],
                        "median_ppu": f"{_r(first['medianPpu'])} → {_r(latest['medianPpu'])} ({(latest['medianPpu'] / first['medianPpu'] - 1) * 100:+.1f}%)",
                        "median_price": f"{_r(first['medianPrice'])} → {_r(latest['medianPrice'])} ({(latest['medianPrice'] / first['medianPrice'] - 1) * 100:+.1f}%)",
                        "active_listings": f"{first['n']} → {latest['n']}",
                    },
                    "snapshots": pts,
                }
            trend_p = base | {"category": CATEGORY_IDS[category],
                              "rooms": ",".join(sorted({"5+" if int(r) >= 5 else str(int(r)) for r in rooms})) if rooms else None}
            trend = (await get("/analytics/trend", trend_p)).get("data", [])
        except BinaAlError as e:
            return {"error": str(e)}
        return {
            "filters": {k: v for k, v in {"deal": deal, "category": category, "district": dist, "location": used or None,
                                          "rooms_for_trend": trend_p.get("rooms")}.items() if v},
            "active_listings_history": series,
            "new_listings_weekly_trend": [{"week": t["week"][:10], "n": t["n"], "median_ppu": _r(t["medianPpu"])} for t in trend],
            "notes": "ppu = AZN per m². The first trend week includes the initial import of all existing listings.",
        }

    # ---------------------------------------------------------------- #
    @mcp.tool()
    async def market_analytics(
        view: Annotated[Literal["overview", "by_rooms", "by_category", "by_repair", "by_deed", "by_floor", "by_agency",
                                "by_mortgage", "by_district", "by_city", "locations", "yields", "yields_by_rooms",
                                "yields_by_location", "distribution"],
                        Field(description="overview = KPIs; by_* = price/m² stats per attribute; locations = price/m² per "
                                          "metro/settlement (priciest first); yields* = gross rental yield & payback years; "
                                          "distribution = histogram of price/m².")] = "overview",
        deal: P_Deal = "buy",
        category: P_Cat = "apartment",
        city: P_City = None,
        districts: P_Dist = None,
        locations: P_Loc = None,
        rooms: P_Rooms = None,
        price_min: int | None = None,
        price_max: int | None = None,
        area_min: int | None = None,
        area_max: int | None = None,
        repaired: bool | None = None,
        has_deed: bool | None = None,
        min_listings: Annotated[int, Field(ge=1, description="For view=locations: minimum listings per location.")] = 30,
    ) -> dict[str, Any]:
        """Current market statistics across ALL active listings (not a sample), via bina.al:
        median/quartile price per m², median price & area, share with deed/renovation/agency, number of
        below-market deals and price drops, breakdowns by attribute, per-location prices, rental yields."""
        try:
            p, applied = await common(deal, category, city, districts, locations, rooms)
            p |= {"priceMin": price_min, "priceMax": price_max, "areaMin": area_min, "areaMax": area_max,
                  "repair": repaired, "deed": has_deed}
            if view == "overview":
                d = (await get("/analytics/overview", p)).get("data", {})
                res: Any = stats_row(d) | {
                    "below_market_deals": d.get("deals"), "price_dropped": d.get("dropped"),
                    "share_with_deed": _pct(d.get("shareWithDeed")), "share_renovated": _pct(d.get("shareRenovated")),
                    "share_agency": _pct(d.get("shareAgency"))}
                for k in ("share_with_deed", "share_renovated", "share_agency"):
                    if res.get(k):
                        res[k] = res[k].lstrip("+")
            elif view.startswith("by_"):
                rows = (await get("/analytics/breakdown", p | {"by": view[3:]})).get("data", [])
                cats = {"2": "new_building", "3": "old_building", "5": "house", "7": "office", "8": "garage",
                        "9": "land", "10": "commercial"}
                res = []
                for x in rows:
                    k = x.get("key")
                    if isinstance(k, dict):
                        label = " · ".join(str(cats.get(vv, vv) if kk == "categoryId" else vv)
                                           for kk, vv in k.items() if vv is not None)
                    else:
                        label = k
                    res.append({"group": label} | stats_row(x))
                res.sort(key=lambda r: -(r.get("n") or 0))
            elif view == "locations":
                rows = (await get("/analytics/locations", p | {"minN": min_listings})).get("data", [])
                res = [{"location": x["key"].get("name"), "district": x["key"].get("district"), "id": x["key"].get("id")}
                       | stats_row(x) for x in rows]
            elif view.startswith("yields"):
                by = {"yields": "district", "yields_by_rooms": "rooms", "yields_by_location": "location"}[view]
                rows = (await get("/analytics/yields", p | {"by": by})).get("data", [])
                res = [{"group": x.get("key"), "sale_ppu": _r(x.get("salePpu")), "rent_ppu_month": _r(x.get("rentPpu"), 1),
                        "gross_yield": f"{x['grossYield'] * 100:.1f}%" if x.get("grossYield") else None,
                        "payback_years": _r(x.get("paybackYears"), 1), "n_sale": x.get("nSale"), "n_rent": x.get("nRent")}
                       for x in rows]
            else:  # distribution
                rows = (await get("/analytics/distribution", p)).get("data", [])
                res = [{"ppu": f"{x['from']}–{x['to']}", "n": x["n"]} for x in rows]
        except BinaAlError as e:
            return {"error": str(e)}
        return {"view": view, "filters": applied, "data": res, "legend": "ppu = AZN per m²; all active listings."}

    # ---------------------------------------------------------------- #
    @mcp.tool()
    async def price_drops(
        deal: P_Deal = "buy",
        category: P_Cat = "apartment",
        city: P_City = None,
        districts: P_Dist = None,
        locations: P_Loc = None,
        rooms: P_Rooms = None,
        price_min: int | None = None,
        price_max: int | None = None,
        area_min: int | None = None,
        area_max: int | None = None,
        repaired: bool | None = None,
        has_deed: bool | None = None,
        direction: Annotated[Literal["down", "up"], Field(description="down = price cuts, up = price rises.")] = "down",
        sort: Annotated[Literal["drop", "amount", "recent", "price_asc", "price_desc"],
                        Field(description="drop = biggest % change; amount = biggest AZN change; recent = latest changes.")] = "recent",
        max_change_pct: Annotated[int | None, Field(description="Hide changes larger than this % (filters typo corrections). Default 50.")] = 50,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        page: Annotated[int, Field(ge=1)] = 1,
    ) -> dict[str, Any]:
        """Listings whose price was cut (or raised) since bina.al first saw them — motivated sellers,
        good for negotiation. Shows first price, current price, % change, fair value and days listed."""
        try:
            p, applied = await common(deal, category, city, districts, locations, rooms)
            p |= {"priceMin": price_min, "priceMax": price_max, "areaMin": area_min, "areaMax": area_max,
                  "repair": repaired, "deed": has_deed, "direction": direction, "sort": sort,
                  "limit": limit, "page": page, "photos": 0}
            r = await get("/price-drops", p)
        except BinaAlError as e:
            return {"error": str(e)}
        rows = [listing_row(x) for x in r.get("data", [])]
        hidden = 0
        if max_change_pct is not None:
            keep = []
            for row_, x in zip(rows, r.get("data", [])):
                if abs(x.get("priceChange") or 0) * 100 > max_change_pct:
                    hidden += 1
                else:
                    keep.append(row_)
            rows = keep
        out = {k: v for k, v in {"filters": applied, "total_matches": r.get("total"), "page": r.get("page"),
                                 "pages": r.get("pages"), "listings": rows}.items() if v is not None}
        if hidden:
            out["hidden"] = f"{hidden} listings on this page with >{max_change_pct}% change hidden (likely typo fixes); set max_change_pct=null to see them."
        return out
