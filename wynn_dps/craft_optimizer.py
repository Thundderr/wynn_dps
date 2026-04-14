"""Craft optimizer: pick the 6 best ingredients for a recipe.

Strategy: pre-filter by skill/level, Pareto-dominance filter on damage IDs,
multi-start hill-climbing on 1-swap moves, 2-opt refinement on the top-10.
Material tiers and atk-speed are enumerated in the outer loop. Powders on
the resulting crafted weapon are optimized per-candidate inside DPS eval.
"""
from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass
from typing import Sequence

from .constants import CLASS_TO_WEAPON, SKP_ELEMENTS
from .craft import build_crafted_item, null_ingredient
from .dps import Build, compute_melee_dps, requirements_met
from .models import CraftedItem, Ingredient, Recipe
from .pareto import pareto_filter
from .skillpoints import enumerate_assignments


def _skill_for_recipe(recipe: Recipe) -> str:
    return recipe.skill.upper() if recipe.skill else ""


def _ingredient_damage_vector(ing: Ingredient) -> tuple[float, ...]:
    m = ing.ids_max
    return (
        m.get("mdRaw", 0),
        m.get("mdPct", 0),
        m.get("str", 0), m.get("dex", 0),
        m.get("critDamPct", 0),
        m.get("eDamPct", 0) + m.get("tDamPct", 0) + m.get("wDamPct", 0)
        + m.get("fDamPct", 0) + m.get("aDamPct", 0),
        -m.get("atkTier", 0),
    )


def _ingredient_cost_vector(ing: Ingredient) -> tuple[float, ...]:
    r = ing.item_ids
    return (r.get("strReq", 0), r.get("dexReq", 0), r.get("intReq", 0),
            r.get("defReq", 0), r.get("agiReq", 0))


def _filter_pool(
    ingredients: Sequence[Ingredient], recipe: Recipe, char_level: int
) -> list[Ingredient]:
    skill = _skill_for_recipe(recipe)
    pool = [
        ing for ing in ingredients
        if ing.level <= char_level
        and (not skill or skill in ing.skills or not ing.skills)
    ]
    # Keep only ingredients with any damage-relevant contribution.
    def _has_dmg(ing: Ingredient) -> bool:
        v = _ingredient_damage_vector(ing)
        return any(abs(x) > 0 for x in v[:-1]) or v[-1] != 0
    pool = [ing for ing in pool if _has_dmg(ing)]
    # Pareto-dominance filter.
    if len(pool) > 8:
        pool = pareto_filter(pool, _ingredient_damage_vector, _ingredient_cost_vector)
    return pool


@dataclass
class CraftResult:
    dps: float
    crafted: CraftedItem
    ingredients: list[Ingredient]
    material_tiers: tuple[int, int]
    atk_speed: str


def _score_placement(
    placement: list[Ingredient],
    recipe: Recipe,
    wclass: str,
    char_level: int,
    atk_speed: str,
    material_tiers: tuple[int, int],
) -> tuple[float, CraftedItem]:
    crafted = build_crafted_item(recipe, placement, material_tiers, atk_speed)
    # Evaluate crafted item as a weapon-only "build" (no armor/accessories)
    # so the DPS calc is self-contained. We pass empty armor/acc so the
    # crafted weapon is graded on its own damage output.
    build = Build(weapon=crafted, armor=[], accessories=[],
                  powders=[], skillpoints={})
    # Assign skill points to meet the crafted item's own reqs.
    sps = enumerate_assignments([crafted], level=char_level)
    best_dps = 0.0
    best_build = build
    if not sps:
        return 0.0, crafted
    for sp in sps:
        if not sp.feasible:
            continue
        b = Build(weapon=crafted, armor=[], accessories=[],
                  powders=[], skillpoints=sp.assigned)
        # Optimize weapon powders (all-same-element tier-6).
        best_p: list = []
        best_p_dps = -1.0
        if crafted.powder_slots > 0:
            for e in SKP_ELEMENTS:
                trial = Build(weapon=crafted, armor=[], accessories=[],
                              powders=[(e, 6)] * crafted.powder_slots,
                              skillpoints=sp.assigned)
                d = compute_melee_dps(trial)
                if d > best_p_dps:
                    best_p_dps = d
                    best_p = [(e, 6)] * crafted.powder_slots
            b.powders = best_p
        if not requirements_met(b):
            continue
        dps = compute_melee_dps(b)
        if dps > best_dps:
            best_dps = dps
            best_build = b
    return best_dps, crafted


def optimize_craft(
    ingredients: Sequence[Ingredient],
    recipe: Recipe,
    wclass: str,
    char_level: int = 106,
    atk_speeds: tuple[str, ...] = ("slow", "normal", "fast"),
    material_tiers: tuple[tuple[int, int], ...] = ((3, 3),),
    n_restarts: int = 30,
    max_iters_per_start: int = 120,
    top_k: int = 5,
    seed: int = 0,
    verbose: bool = True,
) -> list[CraftResult]:
    t_start = time.monotonic()
    pool = _filter_pool(ingredients, recipe, char_level)
    if verbose:
        print(f"Pool after skill+level filter and Pareto: {len(pool)} ingredients",
              flush=True)
    if not pool:
        return []

    rng = random.Random(seed)
    all_results: list[CraftResult] = []

    atk_list = atk_speeds if recipe.is_weapon else ("normal",)
    if verbose:
        n_outer = len(atk_list) * len(material_tiers)
        per_start_evals = max_iters_per_start * 6 * len(pool)
        print(f"Configuration: {len(atk_list)} atk-speed × "
              f"{len(material_tiers)} mat-tier × {n_restarts} restarts = "
              f"{n_outer * n_restarts} hill-climbs (≤{per_start_evals:,} "
              f"DPS evals each).", flush=True)

    best_seen = 0.0
    total_starts = len(atk_list) * len(material_tiers) * n_restarts
    starts_done = 0

    for atk in atk_list:
        for mat in material_tiers:
            if verbose:
                print(f"\n-- atk_speed={atk}, mats={mat} --", flush=True)
            for start in range(n_restarts):
                placement: list[Ingredient] = [rng.choice(pool) for _ in range(6)]
                cur_dps, cur_crafted = _score_placement(
                    placement, recipe, wclass, char_level, atk, mat)

                improved = True
                iters = 0
                while improved and iters < max_iters_per_start:
                    improved = False
                    slot_order = list(range(6))
                    rng.shuffle(slot_order)
                    for slot in slot_order:
                        original = placement[slot]
                        best_cand = original
                        best_dps = cur_dps
                        best_crafted = cur_crafted
                        for cand in pool:
                            if cand is original:
                                continue
                            placement[slot] = cand
                            d, c = _score_placement(
                                placement, recipe, wclass, char_level, atk, mat)
                            if d > best_dps + 1e-6:
                                best_dps = d
                                best_cand = cand
                                best_crafted = c
                        placement[slot] = best_cand
                        if best_cand is not original:
                            cur_dps = best_dps
                            cur_crafted = best_crafted
                            improved = True
                            break
                    iters += 1

                all_results.append(CraftResult(
                    dps=cur_dps, crafted=cur_crafted,
                    ingredients=list(placement),
                    material_tiers=mat, atk_speed=atk,
                ))
                starts_done += 1
                if cur_dps > best_seen:
                    best_seen = cur_dps
                if verbose:
                    elapsed = time.monotonic() - t_start
                    eta = elapsed / starts_done * (total_starts - starts_done)
                    marker = " ★" if cur_dps == best_seen else ""
                    print(f"  start {starts_done:3d}/{total_starts}: "
                          f"DPS {cur_dps:7,.0f}  (best {best_seen:7,.0f}){marker}  "
                          f"[{elapsed:5.1f}s elapsed, ~{eta:5.1f}s left]",
                          flush=True)

    if verbose:
        print(f"\nHill-climb done. Top-10 going to 2-opt refinement...",
              flush=True)

    # 2-opt refinement on top-10.
    all_results.sort(key=lambda r: r.dps, reverse=True)
    refined: list[CraftResult] = []
    for ridx, r in enumerate(all_results[:10]):
        placement = list(r.ingredients)
        cur = r.dps
        before = cur
        improved = True
        while improved:
            improved = False
            for i in range(6):
                for j in range(i + 1, 6):
                    placement[i], placement[j] = placement[j], placement[i]
                    d, c = _score_placement(placement, recipe, wclass,
                                             char_level, r.atk_speed,
                                             r.material_tiers)
                    if d > cur + 1e-6:
                        cur = d
                        r = CraftResult(dps=d, crafted=c,
                                         ingredients=list(placement),
                                         material_tiers=r.material_tiers,
                                         atk_speed=r.atk_speed)
                        improved = True
                    else:
                        placement[i], placement[j] = placement[j], placement[i]
        refined.append(r)
        if verbose:
            delta = cur - before
            print(f"  refined #{ridx+1}: {before:,.0f} -> {cur:,.0f} "
                  f"(+{delta:,.0f})", flush=True)

    # Deduplicate and return top-K.
    seen: set[tuple[tuple[str, ...], str, tuple[int, int]]] = set()
    out: list[CraftResult] = []
    for r in sorted(refined, key=lambda x: x.dps, reverse=True):
        sig = (tuple(sorted(i.name for i in r.ingredients)),
               r.atk_speed, r.material_tiers)
        if sig in seen:
            continue
        seen.add(sig)
        out.append(r)
        if len(out) >= top_k:
            break
    if verbose:
        elapsed = time.monotonic() - t_start
        print(f"\nTotal time: {elapsed:.1f}s. Returning top-{len(out)}.",
              flush=True)
    return out
