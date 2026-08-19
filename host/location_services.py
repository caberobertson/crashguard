"""
location_services.py — turn raw GPS into the things a dispatcher actually asks for.

Everything here uses FREE, no-API-key services (OpenStreetMap Nominatim +
Overpass). It is called ONCE per crash, at trigger time, and the result is
cached and injected into the AI's context. That matters for three reasons:

  1. Speed  — no network lookups happen mid-call, so answers come back fast.
  2. Credits— the AI never needs a second round trip to "go look something up";
              the facts are already in its context.
  3. Truth  — the AI reads real map data instead of guessing street names.

Provides: street address, city, cross streets, nearby hospitals (with distance
and direction), and highway/route context.
"""

from __future__ import annotations

import json
import math
import urllib.parse
import urllib.request

UA = {"User-Agent": "MYOSA-CrashGuard/1.0 (student demo project)",
      "Accept": "application/json"}
TIMEOUT = 8

# Overpass is free and heavily rate-limited: a single endpoint frequently
# returns 429 or times out, which is why cross streets and hospitals can go
# missing. We try several public mirrors in turn. All of this runs in a
# background thread while the phone rings, so the extra time costs nothing.
OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.osm.ch/api/interpreter",
)


def _overpass(query: str) -> dict | None:
    """Run an Overpass query against each mirror until one answers."""
    data = urllib.parse.urlencode({"data": query})
    last = None
    for url in OVERPASS_ENDPOINTS:
        try:
            return _post_json(url, data, timeout=TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            last = exc
            continue
    print(f"[maps] all Overpass mirrors failed: {last}")
    return None


def _get_json(url: str, timeout: int = TIMEOUT):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _post_json(url: str, data: str, timeout: int = TIMEOUT):
    req = urllib.request.Request(url, data=data.encode(), headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def haversine_m(lat1, lon1, lat2, lon2) -> float:
    """Great-circle distance in metres."""
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def compass(lat1, lon1, lat2, lon2) -> str:
    """Rough compass direction from point 1 to point 2."""
    dLon = math.radians(lon2 - lon1)
    y = math.sin(dLon) * math.cos(math.radians(lat2))
    x = (math.cos(math.radians(lat1)) * math.sin(math.radians(lat2))
         - math.sin(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.cos(dLon))
    brg = (math.degrees(math.atan2(y, x)) + 360) % 360
    dirs = ["north", "northeast", "east", "southeast",
            "south", "southwest", "west", "northwest"]
    return dirs[int((brg + 22.5) % 360 // 45)]


def _miles(m: float) -> str:
    mi = m / 1609.34
    if mi < 0.2:
        return f"{int(m * 3.28084)} feet"
    return f"{mi:.1f} miles"


# --------------------------------------------------------------------------
# Individual lookups
# --------------------------------------------------------------------------
def reverse_geocode(lat: float, lon: float) -> dict:
    """Street address, city, county, state, postcode."""
    q = urllib.parse.urlencode({"format": "jsonv2", "lat": lat, "lon": lon,
                                "zoom": "18", "addressdetails": "1"})
    try:
        d = _get_json(f"https://nominatim.openstreetmap.org/reverse?{q}")
    except Exception:
        try:  # fallback provider
            q2 = urllib.parse.urlencode({"latitude": lat, "longitude": lon,
                                         "localityLanguage": "en"})
            d2 = _get_json(
                "https://api.bigdatacloud.net/data/reverse-geocode-client?" + q2)
            return {
                "street": d2.get("locality") or "",
                "city": d2.get("city") or d2.get("locality") or "",
                "state": d2.get("principalSubdivision") or "",
                "postcode": d2.get("postcode") or "",
                "full": ", ".join(x for x in (d2.get("locality"),
                                              d2.get("city"),
                                              d2.get("principalSubdivision")) if x),
            }
        except Exception:
            return {}
    a = d.get("address", {})
    house = a.get("house_number", "")
    road = a.get("road") or a.get("pedestrian") or a.get("footway") or ""
    city = (a.get("city") or a.get("town") or a.get("village")
            or a.get("hamlet") or a.get("suburb") or "")
    street = f"{house} {road}".strip()
    return {
        "street": street or road,
        "road": road,
        "city": city,
        "county": a.get("county", ""),
        "state": a.get("state", ""),
        "postcode": a.get("postcode", ""),
        "full": ", ".join(x for x in (street, city, a.get("state", "")) if x),
    }


def scene_features(lat: float, lon: float) -> dict:
    """ONE Overpass query for everything nearby: named roads, hospitals, and
    notable landmarks. Combining them into a single request (instead of three)
    roughly thirds the lookup time and the rate-limit exposure, which is what
    kept cross streets and hospitals from arriving in time."""
    query = f"""
    [out:json][timeout:12];
    (
      way(around:250,{lat},{lon})["highway"]["name"];
      node(around:15000,{lat},{lon})["amenity"="hospital"];
      way(around:15000,{lat},{lon})["amenity"="hospital"];
      node(around:1200,{lat},{lon})["amenity"~"^(school|university|fire_station|police|fuel|pharmacy)$"];
      node(around:1200,{lat},{lon})["shop"]["name"];
      node(around:1200,{lat},{lon})["tourism"]["name"];
    );
    out tags center 120;
    """
    d = _overpass(query)
    roads: list[tuple[float, str]] = []
    hosp: list[dict] = []
    marks: list[dict] = []
    if not d:
        return {"cross_streets": [], "nearby_hospitals": [], "landmarks": []}

    seen_road: set[str] = set()
    for el in d.get("elements", []):
        t = el.get("tags") or {}
        name = t.get("name")
        if not name:
            continue
        plat = el.get("lat") or (el.get("center") or {}).get("lat")
        plon = el.get("lon") or (el.get("center") or {}).get("lon")
        dist = haversine_m(lat, lon, plat, plon) if plat is not None else 9e9

        if t.get("highway"):
            if name not in seen_road:
                seen_road.add(name)
                roads.append((dist, name))
        elif t.get("amenity") == "hospital":
            hosp.append({"name": name, "distance_m": round(dist),
                         "distance": _miles(dist),
                         "direction": compass(lat, lon, plat, plon),
                         "emergency": t.get("emergency", "")})
        else:
            kind = (t.get("amenity") or t.get("shop") or t.get("tourism") or "")
            marks.append({"name": name, "kind": kind.replace("_", " "),
                          "distance_m": round(dist),
                          "distance": _miles(dist),
                          "direction": compass(lat, lon, plat, plon)})

    roads.sort()
    hosp.sort(key=lambda h: h["distance_m"])
    marks.sort(key=lambda m: m["distance_m"])
    return {
        "cross_streets": [n for _, n in roads[:6]],
        "nearby_hospitals": hosp[:4],
        "landmarks": marks[:6],
    }


def cross_streets(lat: float, lon: float, radius_m: int = 250) -> list[str]:
    """Nearby named roads — what a dispatcher means by 'nearest cross street'."""
    query = f"""
    [out:json][timeout:5];
    way(around:{radius_m},{lat},{lon})["highway"]["name"];
    out tags center 40;
    """
    d = _overpass(query)
    if not d:
        return []
    seen, out = set(), []
    for el in d.get("elements", []):
        name = (el.get("tags") or {}).get("name")
        if not name or name in seen:
            continue
        seen.add(name)
        c = el.get("center") or {}
        if c:
            dist = haversine_m(lat, lon, c["lat"], c["lon"])
            out.append((dist, name))
        else:
            out.append((9e9, name))
    out.sort()
    return [n for _, n in out[:6]]


def nearby_hospitals(lat: float, lon: float, radius_m: int = 15000) -> list[dict]:
    """Hospitals / emergency rooms near the crash, nearest first."""
    query = f"""
    [out:json][timeout:6];
    (
      node(around:{radius_m},{lat},{lon})["amenity"="hospital"];
      way(around:{radius_m},{lat},{lon})["amenity"="hospital"];
    );
    out tags center 30;
    """
    d = _overpass(query)
    if not d:
        return []
    found = []
    for el in d.get("elements", []):
        t = el.get("tags") or {}
        name = t.get("name")
        if not name:
            continue
        plat = el.get("lat") or (el.get("center") or {}).get("lat")
        plon = el.get("lon") or (el.get("center") or {}).get("lon")
        if plat is None:
            continue
        dist = haversine_m(lat, lon, plat, plon)
        found.append({
            "name": name,
            "distance_m": round(dist),
            "distance": _miles(dist),
            "direction": compass(lat, lon, plat, plon),
            "emergency": t.get("emergency", ""),
        })
    found.sort(key=lambda h: h["distance_m"])
    return found[:4]


# --------------------------------------------------------------------------
# One-shot bundle — called once per crash, cached, injected into AI context
# --------------------------------------------------------------------------
def build_scene_context(lat: float, lon: float) -> dict:
    """Everything map-derived a dispatcher might ask for, fetched once.
    Two network calls total: one geocode, one combined feature query."""
    ctx: dict = {"lat": lat, "lon": lon}
    try:
        ctx["address"] = reverse_geocode(lat, lon)
    except Exception as exc:  # noqa: BLE001
        print(f"[maps] address lookup failed: {exc}")
        ctx["address"] = {}
    try:
        ctx.update(scene_features(lat, lon))
    except Exception as exc:  # noqa: BLE001
        print(f"[maps] feature lookup failed: {exc}")
        ctx.setdefault("cross_streets", [])
        ctx.setdefault("nearby_hospitals", [])
        ctx.setdefault("landmarks", [])
    for k in ("address", "cross_streets", "nearby_hospitals", "landmarks"):
        if not ctx.get(k):
            print(f"[maps] WARNING: no {k} returned for {lat},{lon}")
    return ctx


def context_summary(ctx: dict) -> str:
    """Compact plain-text form for the AI prompt (token-efficient — this is a
    fraction of the size of the raw JSON). Absent fields are stated explicitly
    so the agent never claims to hold data it does not have."""
    if not ctx:
        return ""
    lines = []
    a = ctx.get("address") or {}
    if a.get("full"):
        lines.append(f"Address: {a['full']}")
    if a.get("city"):
        loc = f"City: {a['city']}"
        if a.get("county"):
            loc += f", {a['county']}"
        if a.get("state"):
            loc += f", {a['state']}"
        if a.get("postcode"):
            loc += f"  ZIP: {a['postcode']}"
        lines.append(loc)
    elif a.get("postcode"):
        lines.append(f"ZIP: {a['postcode']}")

    cs = ctx.get("cross_streets") or []
    if cs:
        lines.append("Nearest cross streets (nearest first): " + ", ".join(cs))
    else:
        lines.append("Cross streets: NOT AVAILABLE — say you do not have them.")

    hs = ctx.get("nearby_hospitals") or []
    if hs:
        lines.append("Nearest hospitals: " + "; ".join(
            f"{h['name']} ({h['distance']} {h['direction']})" for h in hs))
    else:
        lines.append("Hospitals: NOT AVAILABLE — say you do not have them.")

    lm = ctx.get("landmarks") or []
    if lm:
        lines.append("Nearby landmarks: " + "; ".join(
            f"{m['name']} ({m['kind']}, {m['distance']} {m['direction']})"
            for m in lm))

    lines.append(f"Coordinates: {ctx.get('lat')}, {ctx.get('lon')}")
    return "\n".join(lines)


if __name__ == "__main__":  # quick manual test
    import sys
    la = float(sys.argv[1]) if len(sys.argv) > 2 else 32.2809
    lo = float(sys.argv[2]) if len(sys.argv) > 2 else -106.7469
    print(context_summary(build_scene_context(la, lo)))