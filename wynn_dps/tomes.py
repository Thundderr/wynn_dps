"""Guild tomes: bundled static list + small knapsack solver.

Wynncraft caps worn guild tomes at 4. Each tome grants 4 SP to one stat or
2/2 to two stats (or 1/1/1/1/1 'Rainbow').
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from importlib.resources import files

from .constants import SKP_ORDER

MAX_GUILD_TOMES = 4


@dataclass(frozen=True)
class GuildTome:
    name: str
    level: int
    grants: tuple[tuple[str, int], ...]   # immutable ((stat, n), ...)

    def grants_dict(self) -> dict[str, int]:
        return dict(self.grants)


def load_guild_tomes() -> list[GuildTome]:
    raw = json.loads(files("wynn_dps.data").joinpath("guild_tomes.json").read_text())
    out = []
    for entry in raw:
        grants = tuple((k, int(v)) for k, v in entry["sp_grants"].items())
        out.append(GuildTome(
            name=entry["name"],
            level=int(entry.get("level", 100)),
            grants=grants,
        ))
    return out


def pick_tomes_for_shortfall(
    shortfall: dict[str, int], tomes: list[GuildTome] | None = None,
    max_slots: int = MAX_GUILD_TOMES,
) -> list[GuildTome]:
    """Greedy 0/1 knapsack: cover the per-stat shortfall using ≤ max_slots tomes.

    Returns the chosen tome list (possibly empty). May leave some shortfall
    uncovered if no combination fits.
    """
    if not tomes:
        tomes = load_guild_tomes()
    remaining = {s: max(0, v) for s, v in shortfall.items()}

    chosen: list[GuildTome] = []
    available = list(tomes)
    for _ in range(max_slots):
        if not any(v > 0 for v in remaining.values()):
            break
        best, best_score = None, 0.0
        for t in available:
            score = 0.0
            for stat, n in t.grants:
                score += min(n, remaining.get(stat, 0))
            # Tie-break on least over-allocation.
            over = sum(max(0, n - remaining.get(stat, 0)) for stat, n in t.grants)
            score -= over * 0.01
            if score > best_score:
                best_score = score
                best = t
        if best is None or best_score <= 0:
            break
        chosen.append(best)
        available.remove(best)
        for stat, n in best.grants:
            if stat in remaining:
                remaining[stat] = max(0, remaining[stat] - n)
    return chosen


def total_sp_from_tomes(tomes: list[GuildTome]) -> dict[str, int]:
    out = {s: 0 for s in SKP_ORDER}
    for t in tomes:
        for stat, n in t.grants:
            out[stat] = out.get(stat, 0) + n
    return out
