"""Command-line interface: `wynn_dps build` and `wynn_dps craft`."""
from __future__ import annotations

import argparse
import sys

from .api import fetch_ingredients, fetch_items
from .atree import apply_atree, list_class_nodes, load_atree
from .constants import CLASS_TO_WEAPON, WEAPON_TO_CLASS
from .craft_optimizer import optimize_craft
from .cycle import melee_hits_per_second, optimal_cycle, simulate_rotation
from .dps import (
    Build, compute_melee_dps, compute_poison_dps, compute_spell_cost,
    evaluate_spell,
)
from .models import (
    Recipe, load_recipes, parse_all, parse_all_ingredients,
)
from .optimizer import list_mythic_weapons, optimize
from .spells import load_spells


def _run_build(args: argparse.Namespace) -> int:
    raw = fetch_items(force=args.refresh)
    items = parse_all(raw)

    mythics = list_mythic_weapons(items, args.wclass)
    if not mythics:
        print(f"No mythic weapons found for {args.wclass}.", file=sys.stderr)
        return 1

    if not args.weapon:
        print(f"\nMythic {args.wclass} weapons available "
              f"(pick one with --weapon \"<name>\"):\n")
        for w in mythics:
            base = " + ".join(f"{e}={v[0]}-{v[1]}"
                              for e, v in w.base_damage.items() if v[1] > 0)
            print(f"  - {w.name:<25} lvl {w.level:>3}  {w.attack_speed:<10} "
                  f"powders:{w.powder_slots}  base[{base}]")
        return 0

    # Resolve --weapon to a real Item (case/space-insensitive substring match).
    needle = args.weapon.strip().lower()
    matches = [w for w in mythics if needle in w.name.lower()]
    if len(matches) == 0:
        print(f"No mythic {args.wclass} weapon matches '{args.weapon}'. "
              f"Run without --weapon to see the list.", file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(f"Multiple matches for '{args.weapon}': "
              f"{[w.name for w in matches]}", file=sys.stderr)
        return 1
    weapon = matches[0]
    print(f"Loaded {len(items)} items. Optimizing build around mythic weapon: "
          f"{weapon.name} ({weapon.attack_speed}, "
          f"{weapon.powder_slots} powder slots) for {args.wclass} "
          f"(level {args.level})...", flush=True)

    results = optimize(
        items, wclass=args.wclass, level=args.level,
        top_k=args.top_k, max_pool_per_slot=args.pool,
        pareto=not args.no_pareto, verbose=True,
        weapon=weapon, use_tomes=not args.no_tomes,
    )
    if not results:
        print("No feasible builds found.", file=sys.stderr)
        return 1
    spells_by_class = load_spells()
    spells = spells_by_class.get(args.wclass, {})
    damage_slots = _damage_slots_for_class(args.wclass)

    # Apply ability-tree nodes (if any) to the spell engine for display.
    atree_node_names = [s.strip() for s in args.atree.split(",") if s.strip()]
    toggle_names = [s.strip() for s in (args.toggles or "").split(",") if s.strip()]
    slider_pairs: dict[str, float] = {}
    for raw in (args.sliders or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        if "=" not in raw:
            print(f"warning: --sliders entry '{raw}' missing '='", file=sys.stderr)
            continue
        name, _, value = raw.partition("=")
        try:
            slider_pairs[name.strip()] = float(value.strip())
        except ValueError:
            print(f"warning: --sliders value '{value}' not numeric", file=sys.stderr)

    atree_applied = None
    if atree_node_names:
        atree_applied = apply_atree(args.wclass, atree_node_names, spells,
                                     active_toggles=toggle_names,
                                     sliders=slider_pairs)
        spells = {**spells, **atree_applied.spells}
        print(f"\nAbility tree active ({len(atree_node_names)} nodes): "
              f"{', '.join(atree_node_names)}")
        if toggle_names:
            print(f"  Toggles ON: {', '.join(toggle_names)}")
        if slider_pairs:
            print(f"  Sliders: {slider_pairs}")

    for i, r in enumerate(results, 1):
        print(f"\n=== Build #{i} — melee DPS: {r.dps:,.0f} ===")
        print(f"  Weapon:    {r.build.weapon.name} ({r.build.weapon.tier}, "
              f"{r.build.weapon.attack_speed})")
        if r.build.powders:
            pstr = ", ".join(f"{e} t{t}" for e, t in r.build.powders)
            print(f"  Powders:   {pstr}")
        for a in r.build.armor:
            print(f"  {a.sub_type:<10} {a.name} ({a.tier})")
        for a in r.build.accessories:
            print(f"  {a.sub_type:<10} {a.name} ({a.tier})")
        skp_str = ", ".join(f"{k[:3]}={v}" for k, v in r.build.skillpoints.items() if v)
        print(f"  Skill pts: {skp_str}")
        if r.tomes:
            print(f"  Tomes:     " + ", ".join(t.name for t in r.tomes))
        # Decorate the build with atree bonuses so the spell display reflects them.
        if atree_applied is not None:
            r.build.atree_bonuses = atree_applied.stat_bonuses
            r.build.atree_short_bonuses = atree_applied.raw_short_bonuses
            r.build.atree_spells = atree_applied.spells
            r.build.atree_spell_cost_delta = atree_applied.spell_cost_delta
            r.build.atree_damage_mults = atree_applied.damage_mults
        _print_spell_section(r.build, spells, damage_slots)
    return 0


def _damage_slots_for_class(wclass: str) -> tuple[int, ...]:
    return {
        "archer": (1, 3),     # Arrow Storm + Arrow Bomb
        "warrior": (1, 3),    # Bash + Charge (verify per class)
        "mage": (1, 3),
        "assassin": (1, 3),
        "shaman": (1, 3),
    }.get(wclass, (1, 3))


def _print_spell_section(build: Build, spells, damage_slots: tuple[int, ...]) -> None:
    if not spells:
        return
    melee_aps = melee_hits_per_second(build)
    melee_per_hit = compute_melee_dps(build) / melee_aps if melee_aps else 0
    print(f"\n  Spells (melee aps {melee_aps:.2f} hits/s, "
          f"per-hit {melee_per_hit:,.0f}):")
    for slot in (0, 1, 2, 3, 4):
        spell = spells.get(slot)
        if spell is None or not spell.parts:
            continue
        cost = compute_spell_cost(build, slot, spell.base_cost) if slot > 0 else 0
        results = evaluate_spell(build, spell)
        # Pick the most informative line: display_part if set, else first part.
        display = spell.display_part or spell.parts[-1].name
        val = results.get(display, 0.0)
        cost_str = f" ({cost:.2f} mana)" if cost else ""
        print(f"    [{slot}] {spell.name:<18}{cost_str:<15} {display}: {val:,.0f}")
    poison = compute_poison_dps(build)
    if poison > 0:
        print(f"    Poison Tick DPS: {poison:,.0f}")

    cycle = optimal_cycle(build, spells, damage_slots=damage_slots)
    rot_str = " → ".join(cycle.rotation)
    sustain = "✓ sustainable" if cycle.sustainable else (
        f"✗ deficit {cycle.mana_deficit_per_sec:.1f} mana/s")
    print(f"\n  Cycle: {rot_str}")
    print(f"    DPS {cycle.dps:,.0f}  |  mana income {cycle.mana_per_sec:.1f}/s  "
          f"|  spell-cost {cycle.total_mana_cost:.1f} over {cycle.duration_s:.2f}s  "
          f"|  {sustain}")


def _find_recipe(recipes: list[Recipe], query: str) -> Recipe | None:
    q = query.strip().lower()
    for r in recipes:
        if r.name.lower() == q:
            return r
    # Loose match: e.g. "spear 119"
    parts = q.replace("-", " ").split()
    matches = [
        r for r in recipes
        if all(p in r.name.lower().replace("-", " ") for p in parts)
    ]
    if len(matches) == 1:
        return matches[0]
    if matches:
        print("Multiple recipes match; be more specific:")
        for r in matches[:20]:
            print(f"  {r.name}")
    return None


def _run_craft(args: argparse.Namespace) -> int:
    recipes = load_recipes()
    recipe = _find_recipe(recipes, args.recipe)
    if recipe is None:
        print(f"No recipe matching '{args.recipe}'", file=sys.stderr)
        return 1
    print(f"Recipe: {recipe.name} ({recipe.skill}, "
          f"lvl {recipe.level_min}-{recipe.level_max})", flush=True)

    raw = fetch_ingredients(force=args.refresh)
    ingredients = parse_all_ingredients(raw)
    print(f"Loaded {len(ingredients)} ingredients.", flush=True)

    wclass = args.wclass or (
        WEAPON_TO_CLASS[recipe.item_type.lower()] if recipe.is_weapon else "warrior"
    )
    atk_speeds: tuple[str, ...]
    if not recipe.is_weapon:
        atk_speeds = ("normal",)
    elif args.atk_speed == "any":
        atk_speeds = ("slow", "normal", "fast")
    else:
        atk_speeds = (args.atk_speed,)

    results = optimize_craft(
        ingredients, recipe, wclass=wclass, char_level=args.level,
        atk_speeds=atk_speeds,
        material_tiers=((3, 3),) if args.mat_tier == "max" else ((args.mat_tier, args.mat_tier),),
        n_restarts=args.restarts, top_k=args.top_k, verbose=True,
    )
    if not results:
        print("No viable craft found.", file=sys.stderr)
        return 1

    for i, r in enumerate(results, 1):
        print(f"\n=== Craft #{i} — DPS: {r.dps:,.0f} "
              f"(atk={r.atk_speed}, mats={r.material_tiers}) ===")
        for slot_idx, ing in enumerate(r.ingredients):
            print(f"  slot {slot_idx}: {ing.name}  (tier {ing.tier}, lvl {ing.level})")
        print(f"  powders: {[(e,t) for e,t in []] if not r.crafted.powder_slots else 'optimized inside'}")
        if r.crafted.skill_reqs:
            reqs = ", ".join(f"{k[:3]}={v}" for k, v in r.crafted.skill_reqs.items())
            print(f"  item reqs: {reqs}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="wynn_dps",
                                description="Wynncraft build+craft DPS optimizer.")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="Optimize a 9-slot build around a mythic weapon.")
    b.add_argument("--class", dest="wclass", required=True,
                   choices=list(CLASS_TO_WEAPON))
    b.add_argument("--weapon", default=None,
                   help="Mythic weapon name (substring match). "
                        "Omit to list available mythics for the class.")
    b.add_argument("--level", type=int, default=106)
    b.add_argument("--top-k", type=int, default=5)
    b.add_argument("--pool", type=int, default=6,
                   help="Max items per gear slot after Pareto filter (default 6).")
    b.add_argument("--no-pareto", action="store_true")
    b.add_argument("--no-tomes", action="store_true",
                   help="Disable guild-tome rescue (default: enabled).")
    b.add_argument("--atree", default="",
                   help="Comma-separated ability-tree node names to apply "
                        "(e.g. 'Bow Proficiency,Thunder Mastery,Arrow Storm'). "
                        "Use 'wynn-dps atree --class X' to list nodes.")
    b.add_argument("--toggles", default="",
                   help="Comma-separated atree toggle names to enable "
                        "(e.g. 'Initiator,Divine Intervention Arrow Storm').")
    b.add_argument("--sliders", default="",
                   help="Comma-separated slider_name=value pairs "
                        "(e.g. 'Hits dealt=60,Focus=3').")
    b.add_argument("--refresh", action="store_true")

    a = sub.add_parser("atree", help="List ability-tree nodes for a class.")
    a.add_argument("--class", dest="wclass", required=True,
                   choices=list(CLASS_TO_WEAPON))
    a.add_argument("--filter", default=None,
                   help="Substring filter for node names.")

    c = sub.add_parser("craft", help="Optimize a crafted item's ingredients.")
    c.add_argument("--recipe", required=True,
                   help="Recipe name, e.g. 'Spear-119-121'.")
    c.add_argument("--class", dest="wclass", default=None,
                   choices=list(CLASS_TO_WEAPON))
    c.add_argument("--level", type=int, default=106)
    c.add_argument("--atk-speed", default="any",
                   choices=["slow", "normal", "fast", "any"])
    c.add_argument("--mat-tier", default="max",
                   help="1,2,3 or 'max' (default).")
    c.add_argument("--restarts", type=int, default=20)
    c.add_argument("--top-k", type=int, default=3)
    c.add_argument("--refresh", action="store_true")

    args = p.parse_args(argv)
    if args.cmd == "build":
        return _run_build(args)
    if args.cmd == "craft":
        if args.mat_tier != "max":
            args.mat_tier = int(args.mat_tier)
        return _run_craft(args)
    if args.cmd == "atree":
        return _run_atree_list(args)
    p.print_help()
    return 1


def _run_atree_list(args: argparse.Namespace) -> int:
    nodes = list_class_nodes(args.wclass)
    if args.filter:
        f = args.filter.lower()
        nodes = [n for n in nodes if f in n.name.lower()]
    print(f"{len(nodes)} {args.wclass} ability-tree nodes:\n")
    for n in sorted(nodes, key=lambda x: (x.archetype or "", x.name)):
        arch = f" [{n.archetype}]" if n.archetype else ""
        print(f"  {n.name:<28} cost={n.cost}  base_abil={n.base_abil}{arch}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
