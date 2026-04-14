# wynn_dps

Wynncraft build + craft DPS optimizer. Pulls live items/ingredients from the
Wynncraft v3 API; ships recipe data from wynnbuilder.

## Quick start

```bash
cd /Users/aidensmith/test-projects/wynn_dps

# First run auto-creates a venv and installs the package.
./wynn-dps build --class warrior --level 106 --pool 12 --top-k 3
./wynn-dps craft --recipe Spear-103-105 --atk-speed slow --restarts 10
```

Subsequent runs just exec into the existing venv. Run from any directory by
calling the absolute path, or symlink it onto your `PATH`:

```bash
ln -s /Users/aidensmith/test-projects/wynn_dps/wynn-dps /usr/local/bin/wynn-dps
```

## Commands

- `wynn-dps build --class <warrior|mage|archer|assassin|shaman>
  [--weapon "<MythicName>"] [--level N] [--top-k K] [--pool N]
  [--no-pareto] [--no-tomes] [--atree "Node1,Node2,..."] [--refresh]`
  Branch-and-bound search over a 9-slot build for max melee DPS, locked
  to a specified mythic weapon. Output includes per-spell DPS, optimal
  mana-sustained spell rotation, and selected guild tomes.

- `wynn-dps atree --class <class> [--filter SUBSTR]`
  List the class's ability-tree nodes (use to discover names for `--atree`).

- `wynn-dps craft --recipe <Name> [--atk-speed slow|normal|fast|any]
  [--mat-tier 1|2|3|max] [--restarts N] [--top-k K] [--refresh]`
  Multi-start hill-climbing over the 6 ingredient slots of a recipe.

`--refresh` forces a fresh API pull; otherwise the on-disk cache is used.

## Cache

The fetched databases live **inside the repo** at `cache/`:

```
wynn_dps/cache/
├── items/database.json          # /v3/item/search type=weapon|armour|accessory
├── items/database.meta.json     # fetched_at, count, types
├── ingredients/database.json    # /v3/item/search type=ingredient
├── ingredients/database.meta.json
├── materials/database.json      # /v3/item/search type=material
└── optimized/                   # (reserved for future memoized runs)
```

Each fetch is paged (20/page) with 0.5s pacing and 429 back-off; first-run
items take ~3 min, ingredients ~30 s. After that, cache hits are instant.

TTL is 30 days; pass `--refresh` to force a refetch. Override the cache root
with `WYNN_DPS_CACHE=/some/path`.

The cache is **not gitignored** — commit it if you want to share the
database snapshot with collaborators (and skip the first-run fetch).

## Layout

```
wynn_dps/
├── pyproject.toml
├── wynn-dps                     # bash launcher (this file calls .venv)
├── README.md
└── wynn_dps/
    ├── api.py                   # Wynncraft v3 client + disk cache
    ├── cli.py                   # `build` / `craft` subcommands
    ├── constants.py             # powders, attack speeds, skill curve
    ├── craft.py                 # crafting math (port of craft.js)
    ├── craft_optimizer.py       # ingredient hill-climb + 2-opt
    ├── dps.py                   # melee DPS calc (port of damage_calc.js)
    ├── models.py                # Item / Ingredient / Recipe / CraftedItem
    ├── optimizer.py             # build B&B with Pareto + upper-bound prune
    ├── pareto.py                # Pareto-dominance filter
    ├── skillpoints.py           # skill-point assignment solver
    └── data/
        └── recipes.json         # bundled recipe DB (wynnbuilder snapshot)
```

## What's modeled

- Items: weapons, armor, accessories (live API)
- Crafted items: recipe + 6 ingredients + 2 material tiers + powders
  with positional-modifier effectiveness grid (port of `craft.js`)
- Powders: per-element flat damage + neutral conversion stacking
- Skill points: Wynn's asymptotic curve, 11-split optimization for str/dex
- Guild tomes: 16-entry static list, 0/1 knapsack to rescue near-feasible
  builds whose raw SP total is short
- Spells: base 4 spells per class + melee, with full ability-tree support
  for `replace_spell`, `add_spell_prop` (additive AND `behavior:"modify"`),
  and `raw_stat` (with full short-name → v3 ID mapping)
- Spell costs: int reduction + spRaw + spPctFinal
- Mana-sustained ability rotations: enumerates rotation patterns and picks
  the highest-DPS sustainable cycle

## Known gaps vs wynnbuilder

The DPS formula in `dps.py` is verified to match wynnbuilder **exactly**
when given identical stats (proven via the Node bridge at
`scripts/wb_compute.js`). However, the displayed numbers can still diverge
by ~10-30% on full builds because we don't yet:

- Decode wynnbuilder's bit-packed URL format (so per-item ID-roll values
  default to max-roll, while the URL may encode any roll).
- Apply toggleable atree buffs (Initiator, Divine Intervention etc.) by
  default — they require explicit toggle state.
- Handle per-spell-part `damMult.<spell>:<part>` scoping inside the
  spell evaluator (we apply globals only).
- Apply `stat_scaling` slider effects (Frenzy speed bonus etc.).

For the optimizer's purpose (ranking builds against each other) these are
internally consistent. For exact wynnbuilder parity see Phase 3 in the
plan file.

## References

- [wynnbuilder](https://github.com/wynnbuilder/wynnbuilder.github.io) —
  ground-truth source. Our calc is a Python port of `js/damage_calc.js`,
  `js/powders.js`, `js/craft.js`, `js/skillpoints.js`,
  `js/builder/atree_constants.json`, and `js/builder/build_utils.js`.
- [Wynncraft v3 API](https://docs.wynncraft.com/docs/) — live item +
  ingredient data.
- `scripts/wb_compute.js` — Node port of `calculateSpellDamage` for
  diffing intermediate values against ours.
