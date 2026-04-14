"""Wynncraft v3 API client with per-dataset disk cache.

Uses the paginated /v3/item/search endpoint (20/page) with steady pacing and
429 back-off. First run caches each dataset to ~/.cache/wynn_dps/<name>/.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import requests

API_BASE = "https://api.wynncraft.com/v3"
# In-repo cache: wynn_dps/cache/  (sibling of the wynn_dps/ package).
# Override with WYNN_DPS_CACHE env var if you want a different location.
import os as _os
_DEFAULT_CACHE = Path(__file__).resolve().parent.parent / "cache"
CACHE_ROOT = Path(_os.environ.get("WYNN_DPS_CACHE", _DEFAULT_CACHE))
# A long TTL since item data updates rarely. Use --refresh to force.
DEFAULT_TTL = 30 * 24 * 3600
_UA = {"User-Agent": "wynn_dps/0.2"}


def _cache_paths(name: str) -> tuple[Path, Path]:
    d = CACHE_ROOT / name
    d.mkdir(parents=True, exist_ok=True)
    return d / "database.json", d / "database.meta.json"


def _search_page(types: list[str], page: int, max_attempts: int = 8) -> dict[str, Any]:
    delay = 2.0
    for _ in range(max_attempts):
        r = requests.post(
            f"{API_BASE}/item/search",
            params={"page": page},
            json={"type": types},
            headers={**_UA, "Content-Type": "application/json"},
            timeout=30,
        )
        if r.status_code == 429:
            wait = float(r.headers.get("Retry-After", delay))
            time.sleep(wait)
            delay = min(delay * 2, 60)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"page {page} failed after {max_attempts} attempts")


def _fetch_paged(types: list[str], req_interval_s: float = 0.5) -> dict[str, Any]:
    first = _search_page(types, 1)
    total_pages = first["controller"]["pages"]
    total_count = first["controller"]["count"]
    merged: dict[str, Any] = dict(first["results"])
    label = "/".join(types)
    eta_s = (total_pages - 1) * (req_interval_s + 0.2)
    print(f"  {label}: {total_pages} pages × 20 items = {total_count} total. "
          f"ETA ~{eta_s:.0f}s with {req_interval_s}s pacing.", flush=True)
    t0 = time.monotonic()
    for page in range(2, total_pages + 1):
        time.sleep(req_interval_s)
        merged.update(_search_page(types, page)["results"])
        if page % 10 == 0 or page == total_pages:
            elapsed = time.monotonic() - t0
            rate = (page - 1) / elapsed if elapsed > 0 else 0
            remaining = (total_pages - page) / rate if rate > 0 else 0
            print(f"  {label}: {page}/{total_pages} pages "
                  f"({len(merged)}/{total_count} items) "
                  f"[{elapsed:5.1f}s elapsed, ~{remaining:.0f}s left]",
                  flush=True)
    return merged


def _fetch_cached(
    name: str, types: list[str], ttl: int, force: bool
) -> dict[str, Any]:
    db_path, meta_path = _cache_paths(name)
    if not force and db_path.exists():
        age = time.time() - db_path.stat().st_mtime
        if age < ttl:
            with db_path.open() as f:
                return json.load(f)
    print(f"Fetching Wynncraft {name} database (one-time)...", flush=True)
    data = _fetch_paged(types)
    with db_path.open("w") as f:
        json.dump(data, f)
    with meta_path.open("w") as f:
        json.dump({
            "fetched_at": time.time(),
            "count": len(data),
            "types": types,
        }, f, indent=2)
    print(f"  cached {len(data)} entries to {db_path}", flush=True)
    return data


def fetch_items(ttl: int = DEFAULT_TTL, force: bool = False) -> dict[str, Any]:
    return _fetch_cached("items", ["weapon", "armour", "accessory"], ttl, force)


def fetch_ingredients(ttl: int = DEFAULT_TTL, force: bool = False) -> dict[str, Any]:
    return _fetch_cached("ingredients", ["ingredient"], ttl, force)


def fetch_materials(ttl: int = DEFAULT_TTL, force: bool = False) -> dict[str, Any]:
    return _fetch_cached("materials", ["material"], ttl, force)


# Backwards-compat alias used by the older code paths.
fetch_all_items = fetch_items
