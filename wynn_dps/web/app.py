"""FastAPI backend for the wynn_dps web UI.

Routes:
- GET  /api/classes
- GET  /api/items?class=archer&type=weapon&tier=mythic
- GET  /api/atree/{cls}
- GET  /api/recipes?slot=helmet&level=106
- GET  /api/backend
- POST /api/decode-url
- POST /api/encode-url
- POST /api/build-stats
- POST /api/optimize
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..api import fetch_items
from ..atree import apply_atree, list_class_nodes, load_atree
from ..compute_backend import backend as backend_name
from ..constants import CLASS_TO_WEAPON
from ..constraints import BuildConstraints, evaluate_build_summary
from ..cycle import melee_hits_per_second, optimal_cycle
from ..dps import (
    Build, compute_melee_dps, compute_poison_dps, compute_spell_cost,
    evaluate_spell,
)
from ..models import (
    Item, parse_all, parse_all_ingredients, parse_recipe, load_recipes,
)
from ..optimizer import list_mythic_weapons, optimize
from ..spells import load_spells
from ..two_stage import optimize_two_stage
from ..url_decoder import decode_build_url
from ..url_encoder import encode_build_url


_ROOT = Path(__file__).resolve().parent
_STATIC = _ROOT / "static"
_CACHE_OPTIMIZED = Path(__file__).resolve().parent.parent.parent / "cache" / "optimized"
_CACHE_OPTIMIZED.mkdir(parents=True, exist_ok=True)

# Path overrides for URL decoder/encoder (default: bundled wynnbuilder data
# in wynn_dps/data/, falls back to ref clone).
_WB_REF = Path("/Users/aidensmith/test-projects/wynnbuilder_ref")


app = FastAPI(title="wynn_dps")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


# Mount static files at /
if _STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ITEMS: list[Item] | None = None


def _items() -> list[Item]:
    global _ITEMS
    if _ITEMS is None:
        _ITEMS = parse_all(fetch_items())
    return _ITEMS


def _item_by_name(name: str) -> Item | None:
    for it in _items():
        if it.name == name:
            return it
    return None


def _item_to_dict(it: Item) -> dict[str, Any]:
    return {
        "name": it.name,
        "type": it.type,
        "sub_type": it.sub_type,
        "tier": it.tier,
        "level": it.level,
        "class_req": it.class_req,
        "skill_reqs": it.skill_reqs,
        "attack_speed": it.attack_speed,
        "powder_slots": it.powder_slots,
        "base_damage": it.base_damage,
        "ids": it.ids,
    }


def _build_to_dict(build: Build, with_spells: bool = True) -> dict[str, Any]:
    spells_by_class = load_spells()
    summary = evaluate_build_summary(build)
    out: dict[str, Any] = {
        "weapon": _item_to_dict(build.weapon),
        "armor": [_item_to_dict(a) for a in build.armor],
        "accessories": [_item_to_dict(a) for a in build.accessories],
        "powders": build.powders,
        "skillpoints": build.skillpoints,
        "summary": summary,
        "melee_dps": compute_melee_dps(build),
    }
    if with_spells:
        wclass = build.weapon.class_req or ""
        cls_spells = spells_by_class.get(wclass, {})
        spell_table: list[dict[str, Any]] = []
        for slot, sp in sorted(cls_spells.items()):
            results = evaluate_spell(build, sp)
            display = sp.display_part or (sp.parts[-1].name if sp.parts else "")
            spell_table.append({
                "slot": slot,
                "name": sp.name,
                "cost": compute_spell_cost(build, slot, sp.base_cost) if slot > 0 else 0,
                "display_part": display,
                "display_value": results.get(display, 0),
                "parts": results,
            })
        out["spells"] = spell_table
        out["poison_dps"] = compute_poison_dps(build)
        cyc = optimal_cycle(build, cls_spells, damage_slots=(1, 3))
        out["cycle"] = {
            "rotation": list(cyc.rotation),
            "duration_s": cyc.duration_s,
            "dps": cyc.dps,
            "mana_per_sec": cyc.mana_per_sec,
            "sustainable": cyc.sustainable,
            "mana_deficit_per_sec": cyc.mana_deficit_per_sec,
        }
    return out


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------

class DecodeReq(BaseModel):
    hash: str
    cls: str = Field(..., description="warrior/mage/archer/assassin/shaman")


class EncodeReq(BaseModel):
    cls: str
    equipment: list[str | None]
    powders: list[list[str]] = Field(default_factory=list)
    tomes: list[str | None] | None = None
    skill_points: list[int | None] | None = None
    level: int = 106
    atree_nodes: list[str] | None = None


class BuildSpec(BaseModel):
    cls: str
    weapon: str
    level: int = 106
    armor: list[str | None] = Field(default_factory=list)        # 4 slots
    accessories: list[str | None] = Field(default_factory=list)  # 4 slots
    powders: list[list[str]] = Field(default_factory=list)       # weapon's powders
    skillpoints: dict[str, int] = Field(default_factory=dict)
    atree_nodes: list[str] = Field(default_factory=list)
    toggles: list[str] = Field(default_factory=list)
    sliders: dict[str, float] = Field(default_factory=dict)


class OptimizeReq(BuildSpec):
    locked_items: dict[str, str] = Field(default_factory=dict)  # slot -> name
    constraints: dict[str, Any] = Field(default_factory=dict)
    allow_crafted: bool = True
    craft_budget_s: float = 30.0
    top_k: int = 5
    pool: int = 6


def _make_build_from_spec(spec: BuildSpec) -> Build:
    weapon = _item_by_name(spec.weapon)
    if weapon is None:
        raise HTTPException(404, f"weapon {spec.weapon!r} not found")
    armor = [it for it in (_item_by_name(n) if n else None for n in spec.armor) if it]
    accs = [it for it in (_item_by_name(n) if n else None for n in spec.accessories) if it]

    spells = load_spells().get(spec.cls, {})
    atree = apply_atree(spec.cls, spec.atree_nodes, spells,
                         active_toggles=spec.toggles, sliders=spec.sliders)

    weapon_powders: list[tuple[str, int]] = []
    if spec.powders and spec.powders[0]:
        for p in spec.powders[0]:
            weapon_powders.append((_powder_str_to_pair(p)))

    return Build(
        weapon=weapon, armor=armor, accessories=accs,
        powders=weapon_powders, skillpoints=spec.skillpoints,
        atree_bonuses=atree.stat_bonuses,
        atree_short_bonuses=atree.raw_short_bonuses,
        atree_spells=atree.spells,
        atree_spell_cost_delta=atree.spell_cost_delta,
        atree_damage_mults=atree.damage_mults,
    )


_POWDER_LETTER_TO_ELEM = {"E": "earth", "T": "thunder", "W": "water",
                           "F": "fire", "A": "air"}


def _powder_str_to_pair(s: str) -> tuple[str, int]:
    return _POWDER_LETTER_TO_ELEM[s[0].upper()], int(s[1:])


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
async def index():
    f = _STATIC / "index.html"
    if f.exists():
        return FileResponse(f)
    return {"info": "wynn_dps API", "ui": "static UI not built"}


@app.get("/api/classes")
async def api_classes():
    return list(CLASS_TO_WEAPON)


@app.get("/api/backend")
async def api_backend():
    return {"accelerator": backend_name()}


@app.get("/api/items")
async def api_items(
    cls: str | None = Query(None, alias="class"),
    type: str | None = None,
    sub_type: str | None = None,
    tier: str | None = None,
    max_level: int | None = None,
):
    items = _items()
    out = []
    for it in items:
        if cls and it.class_req and it.class_req != cls:
            continue
        if type and it.type != type:
            continue
        if sub_type and it.sub_type != sub_type:
            continue
        if tier and it.tier != tier:
            continue
        if max_level is not None and it.level > max_level:
            continue
        out.append({
            "name": it.name, "type": it.type, "sub_type": it.sub_type,
            "tier": it.tier, "level": it.level,
            "class_req": it.class_req, "powder_slots": it.powder_slots,
            "attack_speed": it.attack_speed,
        })
    return out


@app.get("/api/mythics/{cls}")
async def api_mythics(cls: str):
    if cls not in CLASS_TO_WEAPON:
        raise HTTPException(404, f"unknown class {cls}")
    mythics = list_mythic_weapons(_items(), cls)
    return [
        {
            "name": w.name, "level": w.level, "attack_speed": w.attack_speed,
            "powder_slots": w.powder_slots,
            "base_damage": {e: list(v) for e, v in w.base_damage.items()},
        }
        for w in mythics
    ]


@app.get("/api/atree/{cls}")
async def api_atree(cls: str):
    nodes = list_class_nodes(cls)
    return [
        {
            "name": n.name, "cost": n.cost, "archetype": n.archetype,
            "base_abil": n.base_abil, "parents": n.parents,
            "blockers": n.blockers,
        }
        for n in nodes
    ]


@app.get("/api/recipes")
async def api_recipes(
    slot: str | None = None, max_level: int | None = None,
    min_level: int = 0,
):
    out = []
    slot_map = {
        "helmet": "HELMET", "chestplate": "CHESTPLATE",
        "leggings": "LEGGINGS", "boots": "BOOTS",
        "ring": "RING", "bracelet": "BRACELET", "necklace": "NECKLACE",
    }
    needed_type = slot_map.get(slot) if slot else None
    for r in load_recipes():
        if needed_type and r.item_type != needed_type:
            continue
        if max_level is not None and r.level_max > max_level:
            continue
        if r.level_min < min_level:
            continue
        out.append({
            "name": r.name, "type": r.item_type, "skill": r.skill,
            "level_min": r.level_min, "level_max": r.level_max,
            "base_low": r.base_low, "base_high": r.base_high,
        })
    return out


@app.post("/api/decode-url")
async def api_decode(req: DecodeReq):
    try:
        out = decode_build_url(
            req.hash, cls=req.cls,
            compress_json_path=str(_WB_REF / "clean.json"),
            tomes_json_path=str(_WB_REF / "data/2.1.6.0/tomes.json"),
        )
    except Exception as e:
        raise HTTPException(400, f"decode failed: {e}")
    return {
        "level": out.level, "equipment": out.equipment,
        "powders": out.powders, "tomes": out.tomes,
        "skill_points": out.skill_points,
        "atree_nodes": out.atree_nodes,
    }


@app.post("/api/encode-url")
async def api_encode(req: EncodeReq):
    try:
        h = encode_build_url(
            cls=req.cls, equipment=req.equipment, powders=req.powders,
            tomes=req.tomes, skill_points=req.skill_points,
            level=req.level, atree_nodes=req.atree_nodes,
            items_json_path=str(_WB_REF / "clean.json"),
            tomes_json_path=str(_WB_REF / "data/2.1.6.0/tomes.json"),
        )
    except Exception as e:
        raise HTTPException(400, f"encode failed: {e}")
    return {"hash": h, "url": f"https://wynnbuilder.github.io/builder/#{h}"}


@app.post("/api/build-stats")
async def api_build_stats(spec: BuildSpec):
    build = await asyncio.to_thread(_make_build_from_spec, spec)
    return _build_to_dict(build)


@app.post("/api/optimize")
async def api_optimize(req: OptimizeReq):
    # Cache key
    key_blob = req.model_dump_json()
    key = hashlib.sha1(key_blob.encode()).hexdigest()
    cache_path = _CACHE_OPTIMIZED / f"{key}.json"
    if cache_path.exists():
        return json.loads(cache_path.read_text())

    items = _items()
    weapon = _item_by_name(req.weapon)
    if weapon is None:
        raise HTTPException(404, f"weapon {req.weapon!r} not found")

    locked = {}
    for slot, name in req.locked_items.items():
        it = _item_by_name(name)
        if it is None:
            raise HTTPException(404, f"locked item {name!r} not found")
        locked[slot] = it

    c = BuildConstraints(**req.constraints) if req.constraints else None

    def _run() -> dict[str, Any]:
        if req.allow_crafted:
            from ..api import fetch_ingredients
            ingreds_raw = fetch_ingredients()
            ts = optimize_two_stage(
                items, ingreds_raw, wclass=req.cls, weapon=weapon,
                level=req.level, constraints=c, locked_items=locked,
                top_k=req.top_k, pool=req.pool,
                craft_budget_s=req.craft_budget_s, verbose=False,
            )
            results = ts.final_results
            stage = "B" if ts.swaps else "A"
        else:
            results = optimize(
                items, wclass=req.cls, weapon=weapon, level=req.level,
                top_k=req.top_k, max_pool_per_slot=req.pool,
                use_tomes=True, constraints=c, locked_items=locked,
                verbose=False,
            )
            stage = "A"

        # Decorate with atree for spell display.
        if req.atree_nodes:
            spells = load_spells().get(req.cls, {})
            atree = apply_atree(req.cls, req.atree_nodes, spells,
                                 active_toggles=req.toggles, sliders=req.sliders)
            for r in results:
                r.build.atree_bonuses = atree.stat_bonuses
                r.build.atree_short_bonuses = atree.raw_short_bonuses
                r.build.atree_spells = atree.spells
                r.build.atree_spell_cost_delta = atree.spell_cost_delta
                r.build.atree_damage_mults = atree.damage_mults

        return {
            "stage": stage,
            "results": [
                {
                    "dps": r.dps,
                    "build": _build_to_dict(r.build),
                    "tomes": [t.name for t in (r.tomes or [])],
                }
                for r in results
            ],
        }

    out = await asyncio.to_thread(_run)
    cache_path.write_text(json.dumps(out))
    return out
