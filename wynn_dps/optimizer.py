"""Build optimizer: branch-and-bound over slot assignments.

Per-slot Pareto filtering + cumulative SP-feasibility pruning + upper-bound
DPS pruning + slot ordering + two-phase skill-point solver + greedy weapon
powders.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Sequence

from .constants import (
    ARMOR_TYPES, ATTACK_SPEED_MULT, ATTACK_SPEEDS, CLASS_TO_WEAPON,
    SKP_ELEMENTS, SKP_ORDER, level_to_skill_points,
)
from .constraints import (
    BuildConstraints, meets_constraints, slot_max_summary, upper_bound_meets,
)
from .dps import Build, compute_melee_dps, requirements_met
from .models import Item
from .pareto import pareto_filter
from .powders_smart import pick_powders as smart_pick_powders
from .skillpoints import (
    SPAssignment, enumerate_assignments, items_sp_gains,
    minimum_required_assignment,
)
from .tomes import GuildTome, MAX_GUILD_TOMES, pick_tomes_for_shortfall

SLOTS = ["weapon", "chestplate", "leggings", "helmet", "boots",
         "ring1", "ring2", "bracelet", "necklace"]


def _raw_skill_key(stat: str) -> str:
    return "raw" + stat[0].upper() + stat[1:]


def _dmg_vector(it: Item) -> tuple[float, ...]:
    ids = it.ids
    return (
        ids.get("rawMainAttackDamage", 0),
        ids.get("mainAttackDamage", 0),
        ids.get("rawStrength", 0),
        ids.get("rawDexterity", 0),
        ids.get("criticalDamageBonus", 0),
        sum(ids.get(f"{e}Damage", 0) for e in SKP_ELEMENTS),
        sum(ids.get(f"{e}MainAttackDamage", 0) for e in SKP_ELEMENTS),
        -ids.get("rawAttackSpeed", 0),
    )


def _cost_vector(it: Item) -> tuple[float, ...]:
    return tuple(it.skill_reqs.get(s, 0) for s in SKP_ORDER)


def _build_pools(
    items: list[Item], wclass: str, level: int, pareto: bool
) -> dict[str, list[Item]]:
    weapon_type = CLASS_TO_WEAPON[wclass]
    pools: dict[str, list[Item]] = {s: [] for s in SLOTS}
    for it in items:
        if it.level > level:
            continue
        if it.class_req and it.class_req != wclass:
            continue
        if it.is_weapon:
            if it.sub_type == weapon_type:
                pools["weapon"].append(it)
            continue
        if it.type == "armour" and it.sub_type in ARMOR_TYPES:
            pools[it.sub_type].append(it)
        elif it.type == "accessory":
            if it.sub_type == "ring":
                pools["ring1"].append(it)
                pools["ring2"].append(it)
            elif it.sub_type in ("bracelet", "necklace"):
                pools[it.sub_type].append(it)

    if pareto:
        for k in pools:
            if len(pools[k]) > 4:
                pools[k] = pareto_filter(pools[k], _dmg_vector, _cost_vector)

    return pools


# ---------------------------------------------------------------------------
# Per-slot maxima (used by both UB and SP-feasibility pruning)
# ---------------------------------------------------------------------------

def _slot_maxima(pools: dict[str, list[Item]]) -> dict[str, dict[str, float]]:
    """For each slot, max value of each metric we care about."""
    fields = (
        ["rawMainAttackDamage", "mainAttackDamage", "criticalDamageBonus"]
        + [f"{e}Damage" for e in SKP_ELEMENTS]
        + [f"{e}MainAttackDamage" for e in SKP_ELEMENTS]
        + [_raw_skill_key(s) for s in SKP_ORDER]
    )
    out: dict[str, dict[str, float]] = {}
    for s, pool in pools.items():
        m = {f: 0.0 for f in fields}
        for it in pool:
            for f in fields:
                v = it.ids.get(f, 0)
                if v > m[f]:
                    m[f] = v
        out[s] = m
    return out


def _slot_min_skill_req(pools: dict[str, list[Item]]) -> dict[str, dict[str, int]]:
    """For each slot, minimum requirement per stat across that slot's pool.

    Used by SP feasibility: the slot will demand AT LEAST this much because
    every candidate item in the pool has it.
    """
    out: dict[str, dict[str, int]] = {}
    for s, pool in pools.items():
        if not pool:
            out[s] = {st: 0 for st in SKP_ORDER}
            continue
        m = {st: min(it.skill_reqs.get(st, 0) for it in pool) for st in SKP_ORDER}
        out[s] = m
    return out


# ---------------------------------------------------------------------------
# Upper bound on DPS
# ---------------------------------------------------------------------------

def _max_powder_flat(slots: int) -> float:
    """Tier-6 thunder gives 5-20 → max 20 per slot."""
    return slots * 20.0


def _upper_bound(
    weapon: Item,
    selected: list[Item],
    remaining_slots: list[str],
    slot_max: dict[str, dict[str, float]],
) -> float:
    """Tighter UB: sum partial item bonuses + per-remaining-slot maxima."""
    raw_main = sum(it.ids.get("rawMainAttackDamage", 0) for it in selected)
    pct_main = sum(it.ids.get("mainAttackDamage", 0) for it in selected)
    elem_pct = sum(
        sum(it.ids.get(f"{e}Damage", 0) + it.ids.get(f"{e}MainAttackDamage", 0)
            for e in SKP_ELEMENTS)
        for it in selected
    )
    crit_dmg = sum(it.ids.get("criticalDamageBonus", 0) for it in selected)

    for s in remaining_slots:
        m = slot_max[s]
        raw_main += m["rawMainAttackDamage"]
        pct_main += m["mainAttackDamage"]
        elem_pct += sum(m[f"{e}Damage"] + m[f"{e}MainAttackDamage"]
                        for e in SKP_ELEMENTS)
        crit_dmg += m["criticalDamageBonus"]

    w_max = sum(v[1] for v in weapon.base_damage.values()) \
            + _max_powder_flat(weapon.powder_slots)
    base_hit = w_max + raw_main
    boost = 1.0 + pct_main / 100.0 + elem_pct / 100.0 + 0.83  # str-skill cap
    # Crit ceiling: at 150 dex, crit chance is ~0.83. Crit hit = 1 + critDmgBonus.
    crit_factor = 1 + 0.83 * (1 + crit_dmg / 100.0)
    attack_idx = ATTACK_SPEEDS.index(weapon.attack_speed or "normal")
    aps = ATTACK_SPEED_MULT[ATTACK_SPEEDS[attack_idx]]
    return base_hit * boost * aps * crit_factor


# ---------------------------------------------------------------------------
# Skill-point feasibility pruning
# ---------------------------------------------------------------------------

_sp_feasible_last_breakdown: dict | None = None


def _sp_feasible(
    weapon: Item, selected: list[Item], remaining_slots: list[str],
    slot_max: dict[str, dict[str, float]],
    slot_min_req: dict[str, dict[str, int]],
    sp_budget: int,
    tome_budget: int = 0,
) -> bool:
    """Optimistic feasibility: assume future slots grant their max raw<Stat>
    and require at least slot_min_req[stat]. If even that cannot fit in
    sp_budget (+ tome rescue headroom), prune.
    """
    global _sp_feasible_last_breakdown
    # Selected items contribute fixed reqs and gains.
    cur_max_req = {s: 0 for s in SKP_ORDER}
    cur_gains = {s: 0 for s in SKP_ORDER}
    for it in [weapon] + selected:
        for s in SKP_ORDER:
            r = it.skill_reqs.get(s, 0)
            if r > cur_max_req[s]:
                cur_max_req[s] = r
            cur_gains[s] += int(it.ids.get(_raw_skill_key(s), 0))

    # For remaining slots: forced floor on req from the slot's pool min, and
    # optimistic max gain we can pull from that slot.
    future_floor_req = {s: 0 for s in SKP_ORDER}
    future_max_gain = {s: 0 for s in SKP_ORDER}
    for slot in remaining_slots:
        for st in SKP_ORDER:
            future_floor_req[st] = max(future_floor_req[st], slot_min_req[slot][st])
            future_max_gain[st] += int(slot_max[slot][_raw_skill_key(st)])

    total_assigned = 0
    breakdown: dict[str, dict[str, float]] = {}
    for st in SKP_ORDER:
        req = max(cur_max_req[st], future_floor_req[st])
        gain = cur_gains[st] + future_max_gain[st]
        if req <= 0:
            need = 0
        else:
            need = max(0, req - gain)
        breakdown[st] = {"req": req, "gain": gain, "need": need}
        total_assigned += need
    _sp_feasible_last_breakdown = {
        "breakdown": breakdown, "total": total_assigned,
        "budget": sp_budget + tome_budget,
        "n_selected": len(selected), "n_remaining": len(remaining_slots),
    }
    return total_assigned <= sp_budget + tome_budget


def _optimize_powders(build: Build, constraints: BuildConstraints | None = None) -> list[tuple[str, int]]:
    """Element-aligned powder picker (smart). Falls back to brute 5-element if
    smart returns empty (e.g., constraint-incompatible)."""
    powders = smart_pick_powders(build.weapon, build, constraints)
    if powders or build.weapon.powder_slots <= 0:
        return powders
    # Fallback: try every element if smart returned nothing
    best: list[tuple[str, int]] = []
    best_dps = -1.0
    for e in SKP_ELEMENTS:
        ps = [(e, 6)] * build.weapon.powder_slots
        trial = Build(weapon=build.weapon, armor=build.armor,
                      accessories=build.accessories, powders=ps,
                      skillpoints=build.skillpoints)
        dps = compute_melee_dps(trial)
        if dps > best_dps:
            best_dps = dps
            best = ps
    return best


@dataclass
class Result:
    dps: float
    build: Build
    tomes: list[GuildTome] = None  # type: ignore[assignment]


def _eval_full_build(
    weapon: Item, chosen: dict[str, Item], level: int,
    use_tomes: bool = True,
    constraints: BuildConstraints | None = None,
) -> tuple[float, Build, list[GuildTome]] | None:
    armor_slots = [chosen[s] for s in ("helmet", "chestplate", "leggings", "boots") if s in chosen]
    acc_slots = [chosen[s] for s in ("ring1", "ring2", "bracelet", "necklace") if s in chosen]
    items_now = [weapon, *armor_slots, *acc_slots]

    # Try without tomes first.
    tomes: list[GuildTome] = []
    candidates = enumerate_assignments(items_now, level=level)
    feasible = [sp for sp in candidates if sp.feasible]

    if not feasible and use_tomes:
        # Compute SP shortfall and pick guild tomes to cover it.
        shortfall = minimum_required_assignment(items_now)
        # The "shortfall" at this point is how many SP a player must assign.
        # We have 200 SP; tomes can add up to MAX_GUILD_TOMES * 4 = 16 to a stat.
        sp_total_needed = sum(shortfall.values())
        sp_budget = 200
        if sp_total_needed > sp_budget:
            extra_needed = sp_total_needed - sp_budget
            # Try to cover extra_needed by tomes — heuristic: just dump it on
            # the largest single shortfall stat first.
            stat_shortfall = dict(sorted(shortfall.items(), key=lambda kv: -kv[1]))
            cover: dict[str, int] = {}
            remaining = extra_needed
            for stat, _ in stat_shortfall.items():
                if remaining <= 0:
                    break
                give = min(MAX_GUILD_TOMES * 4, remaining)
                cover[stat] = give
                remaining -= give
            tomes = pick_tomes_for_shortfall(cover)
            if tomes:
                # Add tome SP into a synthetic item-like contribution by
                # bumping the assignment phase. Simpler: bump items_now's
                # contribution using a fake item — but rerunning enumerate
                # with a higher virtual budget is cleaner.
                tome_sp = {s: 0 for s in items_sp_gains([]).keys()}
                for t in tomes:
                    for stat, n in t.grants:
                        tome_sp[stat] = tome_sp.get(stat, 0) + n
                candidates = enumerate_assignments(items_now, level=level)
                # Manually grant tome SP by topping up assigned dicts post-hoc
                # and re-checking feasibility.
                from .skillpoints import total_reqs_met
                sp_gains = items_sp_gains(items_now)
                augmented: list = []
                for sp in candidates:
                    new_assigned = dict(sp.assigned)
                    for stat, n in tome_sp.items():
                        new_assigned[stat] = new_assigned.get(stat, 0) + n
                    if total_reqs_met(items_now, new_assigned, sp_gains):
                        sp.feasible = True
                        sp.assigned = new_assigned
                        augmented.append(sp)
                feasible = augmented

    if not feasible:
        return None

    best_dps = -1.0
    best_build: Build | None = None
    for sp in feasible:
        build = Build(weapon=weapon, armor=armor_slots, accessories=acc_slots,
                      powders=[], skillpoints=sp.assigned)
        build.powders = _optimize_powders(build, constraints)
        if not requirements_met(build):
            continue
        # Hard-constraint filter (post-eval).
        if constraints is not None and not meets_constraints(build, constraints):
            from .constraints import evaluate_build_summary as _ebs
            s = _ebs(build)
            _eval_full_build._last_fail = {
                k: s.get(k) for k in ("mana_regen", "mana_steal", "life_steal",
                                       "walk_speed", "hp", "ehp", "poison",
                                       "health_regen_raw")
            }
            continue
        dps = compute_melee_dps(build)
        if dps > best_dps:
            best_dps = dps
            best_build = build
    if best_build is None:
        return None
    return best_dps, best_build, tomes


def list_mythic_weapons(items: list[Item], wclass: str) -> list[Item]:
    weapon_type = CLASS_TO_WEAPON[wclass]
    return sorted(
        [it for it in items
         if it.is_weapon and it.sub_type == weapon_type and it.tier == "mythic"],
        key=lambda it: it.name,
    )


def optimize(
    items: list[Item],
    wclass: str,
    level: int = 106,
    top_k: int = 5,
    pareto: bool = True,
    max_pool_per_slot: int = 6,
    verbose: bool = True,
    weapon: Item | None = None,
    use_tomes: bool = True,
    constraints: BuildConstraints | None = None,
    locked_items: dict[str, Item] | None = None,
) -> list[Result]:
    pools = _build_pools(items, wclass, level, pareto=pareto)

    # Lock to a specific weapon if provided.
    if weapon is not None:
        pools["weapon"] = [weapon]
    if not pools["weapon"]:
        return []

    # Lock specific gear slots to a single item.
    if locked_items:
        for slot, it in locked_items.items():
            if slot in pools and it is not None:
                pools[slot] = [it]

    def _heur(it: Item) -> float:
        v = _dmg_vector(it)
        # Penalize high-SP-req items so the top-N retains SP-cheap options.
        sp_req = sum(it.skill_reqs.get(s, 0) for s in SKP_ORDER)
        return v[0] + 10 * v[1] + 3 * (v[2] + v[3]) + 8 * (v[5] + v[6]) - 2 * sp_req

    for k in pools:
        pools[k].sort(key=_heur, reverse=True)
        pools[k] = pools[k][:max_pool_per_slot]

    slot_max = _slot_maxima(pools)
    slot_min_req = _slot_min_skill_req(pools)
    # Pre-compute per-slot constraint maxima for upper-bound pruning.
    slot_constraint_caps = slot_max_summary(pools, pools["weapon"][0]) if constraints else {}

    # Slot ordering: biggest contribution first.
    gear_slots = ["chestplate", "leggings", "helmet", "boots",
                  "ring1", "ring2", "bracelet", "necklace"]
    gear_slots.sort(
        key=lambda s: -(slot_max[s]["rawMainAttackDamage"]
                        + 10 * slot_max[s]["mainAttackDamage"])
    )

    sp_budget = level_to_skill_points(level)

    if verbose:
        pool_sizes = {s: len(p) for s, p in pools.items()}
        print(f"Pool sizes after Pareto+trim: {pool_sizes}", flush=True)
        worst = len(pools["weapon"])
        for s in gear_slots:
            worst *= max(1, len(pools[s]))
        print(f"Worst-case search space: {worst:,} combos. "
              f"SP budget: {sp_budget}. Slot order: {gear_slots}", flush=True)

    results: list[Result] = []
    best_dps_overall = 0.0
    t_start = time.monotonic()
    stats = {"evaluated": 0, "pruned_dps": 0, "pruned_sp": 0, "infeasible": 0}
    last_log = [t_start]

    for w_idx, weapon in enumerate(pools["weapon"]):
        if verbose:
            elapsed = time.monotonic() - t_start
            print(f"[{elapsed:6.1f}s] weapon {w_idx+1}/{len(pools['weapon'])}: "
                  f"{weapon.name}  (best so far: {best_dps_overall:,.0f})",
                  flush=True)

        chosen: dict[str, Item] = {}
        chosen_list: list[Item] = []  # preserves insertion order

        def dfs(depth: int) -> None:
            nonlocal best_dps_overall
            if verbose:
                now = time.monotonic()
                if now - last_log[0] > 2.0:
                    last_log[0] = now
                    print(f"  ...{stats['evaluated']:,} evals, "
                          f"{stats['pruned_sp']:,} sp-prune, "
                          f"{stats['pruned_dps']:,} dps-prune, "
                          f"{stats['infeasible']:,} infeasible "
                          f"(best {best_dps_overall:,.0f})", flush=True)

            if depth == len(gear_slots):
                stats["evaluated"] += 1
                out = _eval_full_build(weapon, chosen, level,
                                        use_tomes=use_tomes,
                                        constraints=constraints)
                if out is None:
                    stats["infeasible"] += 1
                    return
                dps, build, tomes = out
                if dps > best_dps_overall:
                    best_dps_overall = dps
                    if verbose:
                        names = ", ".join(it.name for it in build.armor + build.accessories)
                        if tomes:
                            tome_str = " + tomes:" + str(
                                [t.name.split("'")[0] for t in tomes])
                        else:
                            tome_str = ""
                        print(f"  ✓ new best: {dps:,.0f} DPS  ({names}){tome_str}",
                              flush=True)
                results.append(Result(dps=dps, build=build, tomes=tomes))
                return

            slot = gear_slots[depth]
            remaining = gear_slots[depth + 1:]

            # Constraint upper-bound prune.
            if constraints is not None and not upper_bound_meets(
                    weapon, chosen_list, remaining, slot_constraint_caps,
                    constraints):
                stats.setdefault("pruned_constraint", 0)
                stats["pruned_constraint"] += 1
                return

            # SP-feasibility prune (optimistic). Tome rescue adds up to
            # 16 SP of flex when tomes are enabled.
            tome_budget = 16 if use_tomes else 0
            if not _sp_feasible(weapon, chosen_list, remaining,
                                 slot_max, slot_min_req, sp_budget,
                                 tome_budget=tome_budget):
                stats["pruned_sp"] += 1
                # Dump a sample prune cause the first time.
                if verbose and stats["pruned_sp"] <= 3:
                    bd = _sp_feasible_last_breakdown
                    if bd:
                        worst = max(bd["breakdown"].items(),
                                     key=lambda kv: kv[1]["need"])
                        st, info = worst
                        print(f"  sp-prune @depth={depth} (sel={bd['n_selected']}, "
                              f"rem={bd['n_remaining']}): "
                              f"need total={bd['total']:.0f} > {bd['budget']}; "
                              f"worst stat={st} req={info['req']:.0f} "
                              f"gain={info['gain']:.0f} need={info['need']:.0f}",
                              flush=True)
                return

            # DPS upper-bound prune.
            ub = _upper_bound(weapon, chosen_list, remaining, slot_max)
            if ub < best_dps_overall * 0.99:
                stats["pruned_dps"] += 1
                return

            for cand in pools[slot]:
                if slot == "ring2" and chosen.get("ring1") is cand:
                    continue
                chosen[slot] = cand
                chosen_list.append(cand)
                dfs(depth + 1)
                chosen_list.pop()
            chosen.pop(slot, None)

        dfs(0)

    if verbose:
        elapsed = time.monotonic() - t_start
        print(f"\nDone in {elapsed:.1f}s: {stats['evaluated']:,} full builds "
              f"evaluated, {stats['pruned_sp']:,} sp-prunes, "
              f"{stats['pruned_dps']:,} dps-prunes, "
              f"{stats['infeasible']:,} infeasible.", flush=True)
        last_fail = getattr(_eval_full_build, "_last_fail", None)
        if constraints is not None and last_fail:
            print(f"  last constraint-failing build summary: {last_fail}",
                  flush=True)
        # If nothing was evaluated, dump an SP-feasibility breakdown to
        # help the user diagnose why their locked items are infeasible.
        if stats["evaluated"] == 0 and pools["weapon"]:
            w = pools["weapon"][0]
            print("\n▶ SP-feasibility breakdown (root-level, all-zero gear):",
                  flush=True)
            total_need = 0
            for stat in SKP_ORDER:
                raw_key = "raw" + stat[0].upper() + stat[1:]
                req = w.skill_reqs.get(stat, 0)
                gain = int(w.ids.get(raw_key, 0))
                floor = max((slot_min_req[s].get(stat, 0) for s in gear_slots), default=0)
                req_total = max(req, floor)
                remaining_gain = sum(int(slot_max[s].get(raw_key, 0)) for s in gear_slots)
                total_gain = gain + remaining_gain
                # Only stats that some item requires demand SP — negative gains
                # on unrequired stats don't consume SP.
                need = max(0, req_total - total_gain) if req_total > 0 else 0
                total_need += need
                print(f"    {stat:<13} req(w+floors)={req_total:>4}  "
                      f"gain(w+slots_max)={total_gain:>4}  need_assigned>={need:>3}",
                      flush=True)
            budget = sp_budget + (16 if use_tomes else 0)
            verdict = "FITS" if total_need <= budget else "OVER"
            print(f"    total need={total_need} vs budget={budget}  →  {verdict}",
                  flush=True)

    results.sort(key=lambda r: r.dps, reverse=True)
    seen: set[tuple[str, ...]] = set()
    unique: list[Result] = []
    for r in results:
        sig = tuple(sorted(it.name for it in r.build.all_items()))
        if sig in seen:
            continue
        seen.add(sig)
        unique.append(r)
        if len(unique) >= top_k:
            break
    return unique
