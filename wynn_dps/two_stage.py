"""Two-stage optimizer: Stage A finds the best 8-piece normal build, then
Stage B walks each gear slot and asks "does any craftable recipe beat this
slot enough to improve total DPS while still meeting constraints?".

Stage B is bounded by `craft_budget_s` so it can't run forever. If no
crafted improvement is found for a slot, that slot keeps its Stage-A item.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Sequence

from .constraints import BuildConstraints, meets_constraints
from .craft import build_crafted_item
from .craft_optimizer import optimize_craft
from .dps import Build, compute_melee_dps
from .models import (
    Item, Recipe, load_recipes, parse_all_ingredients, parse_ingredient,
    parse_recipe,
)
from .optimizer import Result, optimize


# Slot name → recipe item_type
_SLOT_TO_RECIPE_TYPE = {
    "helmet": "HELMET", "chestplate": "CHESTPLATE",
    "leggings": "LEGGINGS", "boots": "BOOTS",
    "ring1": "RING", "ring2": "RING",
    "bracelet": "BRACELET", "necklace": "NECKLACE",
}


@dataclass
class TwoStageResult:
    base_results: list[Result]                  # Stage A
    final_results: list[Result]                 # Stage B (or A if B made no improvement)
    swaps: list[dict] = field(default_factory=list)  # log of slot swaps performed
    elapsed_s: float = 0.0


def optimize_two_stage(
    items: list[Item],
    ingredients_raw: dict | None,
    wclass: str,
    weapon: Item,
    level: int = 106,
    constraints: BuildConstraints | None = None,
    locked_items: dict[str, Item] | None = None,
    top_k: int = 5,
    pool: int = 6,
    use_tomes: bool = True,
    craft_budget_s: float = 60.0,
    min_dps_improvement_pct: float = 0.1,
    craft_restarts: int = 20,
    craft_iters_per_start: int = 120,
    recipes_per_slot: int = 5,
    verbose: bool = True,
) -> TwoStageResult:
    """Two-stage build optimizer. See module docstring for details.

    `ingredients_raw` is the raw v3 API response (dict). If None, Stage B
    is skipped and only normal items are considered.
    """
    t_start = time.monotonic()

    # -------- Stage A: normal-only baseline --------
    if verbose:
        print("[stage A] running normal-item optimizer...", flush=True)
    base_results = optimize(
        items, wclass=wclass, level=level, top_k=top_k,
        max_pool_per_slot=pool, weapon=weapon, use_tomes=use_tomes,
        constraints=constraints, locked_items=locked_items, verbose=verbose,
    )
    if not base_results or ingredients_raw is None:
        return TwoStageResult(base_results=base_results,
                              final_results=base_results,
                              elapsed_s=time.monotonic() - t_start)

    # -------- Stage B: per-slot crafted refinement --------
    if verbose:
        print(f"\n[stage B] looking for crafted upgrades "
              f"(budget {craft_budget_s:.0f}s)...", flush=True)
    ingredients = parse_all_ingredients(ingredients_raw)
    recipes = load_recipes()

    final_results: list[Result] = []
    swap_log: list[dict] = []
    deadline = t_start + craft_budget_s + (time.monotonic() - t_start)
    locked = locked_items or {}

    armor_slot_order = ["helmet", "chestplate", "leggings", "boots"]
    acc_slot_order = ["ring1", "ring2", "bracelet", "necklace"]
    slots_to_try = armor_slot_order + acc_slot_order
    stats = {"recipes_tried": 0, "swaps": 0, "slots_skipped_time": 0}

    for r_idx, baseline in enumerate(base_results):
        build = baseline.build
        baseline_dps = baseline.dps
        improved_build = build
        improved_dps = baseline_dps
        if verbose:
            print(f"\n  result #{r_idx + 1} baseline DPS: {baseline_dps:,.0f}",
                  flush=True)

        for slot in slots_to_try:
            if slot in locked:
                continue
            if time.monotonic() > deadline:
                stats["slots_skipped_time"] += 1
                if verbose:
                    print(f"    [{slot}] skipped — craft budget exhausted",
                          flush=True)
                continue
            recipe_type = _SLOT_TO_RECIPE_TYPE.get(slot)
            if not recipe_type:
                continue
            # All recipes within the level band (not just the top one).
            slot_recipes = [r for r in recipes
                            if r.item_type == recipe_type
                            and r.level_max <= level
                            and r.level_min >= max(1, level - 40)]
            slot_recipes.sort(key=lambda r: -r.level_max)
            slot_recipes = slot_recipes[:recipes_per_slot]
            if not slot_recipes:
                continue
            if verbose:
                print(f"    [{slot}] trying {len(slot_recipes)} recipes…",
                      flush=True)

            best_for_slot = None
            best_for_slot_dps = improved_dps
            best_recipe_name = None

            for recipe in slot_recipes:
                if time.monotonic() > deadline:
                    break
                try:
                    craft_res = optimize_craft(
                        ingredients, recipe, wclass=wclass, char_level=level,
                        n_restarts=craft_restarts,
                        max_iters_per_start=craft_iters_per_start,
                        top_k=1, verbose=False,
                        context_build=improved_build, context_slot=slot,
                    )
                except Exception as e:
                    if verbose:
                        print(f"      {recipe.name}: FAILED ({e})", flush=True)
                    continue
                stats["recipes_tried"] += 1
                if not craft_res:
                    continue
                best_crafted = craft_res[0].crafted

                # Swap into trial build
                new_armor = list(improved_build.armor)
                new_acc = list(improved_build.accessories)
                if slot in armor_slot_order:
                    idx = armor_slot_order.index(slot)
                    if idx < len(new_armor):
                        new_armor[idx] = best_crafted
                    else:
                        new_armor.append(best_crafted)
                else:
                    idx = acc_slot_order.index(slot)
                    if idx < len(new_acc):
                        new_acc[idx] = best_crafted
                    else:
                        new_acc.append(best_crafted)

                trial = Build(
                    weapon=improved_build.weapon, armor=new_armor,
                    accessories=new_acc, powders=improved_build.powders,
                    skillpoints=improved_build.skillpoints,
                    atree_bonuses=improved_build.atree_bonuses,
                    atree_short_bonuses=improved_build.atree_short_bonuses,
                    atree_spells=improved_build.atree_spells,
                    atree_spell_cost_delta=improved_build.atree_spell_cost_delta,
                    atree_damage_mults=improved_build.atree_damage_mults,
                )
                if not meets_constraints(trial, constraints):
                    continue
                trial_dps = compute_melee_dps(trial)
                if trial_dps > best_for_slot_dps:
                    best_for_slot_dps = trial_dps
                    best_for_slot = trial
                    best_recipe_name = recipe.name

            if best_for_slot is not None and best_for_slot_dps > improved_dps * (
                    1 + min_dps_improvement_pct / 100):
                stats["swaps"] += 1
                if verbose:
                    print(f"      ✓ swap {slot}: "
                          f"{improved_dps:,.0f} → {best_for_slot_dps:,.0f} "
                          f"(+{(best_for_slot_dps/improved_dps-1)*100:.1f}%) "
                          f"via {best_recipe_name}", flush=True)
                swap_log.append({
                    "result_idx": r_idx, "slot": slot,
                    "recipe": best_recipe_name,
                    "old_dps": improved_dps,
                    "new_dps": best_for_slot_dps,
                })
                improved_build = best_for_slot
                improved_dps = best_for_slot_dps

        final_results.append(Result(dps=improved_dps, build=improved_build,
                                     tomes=baseline.tomes))
    if verbose:
        print(f"\n  Stage B stats: {stats['recipes_tried']} recipes tried, "
              f"{stats['swaps']} swaps made, "
              f"{stats['slots_skipped_time']} slot(s) skipped due to time.",
              flush=True)

    final_results.sort(key=lambda r: r.dps, reverse=True)
    return TwoStageResult(
        base_results=base_results,
        final_results=final_results,
        swaps=swap_log,
        elapsed_s=time.monotonic() - t_start,
    )
