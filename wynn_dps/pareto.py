"""Pareto dominance filter for pruning per-slot item pools."""
from __future__ import annotations

from typing import Callable, Sequence, TypeVar

T = TypeVar("T")


def pareto_filter(
    items: Sequence[T],
    vector_fn: Callable[[T], tuple[float, ...]],
    cost_fn: Callable[[T], tuple[float, ...]] | None = None,
) -> list[T]:
    """Return the Pareto-optimal subset.

    `vector_fn(x)` returns a tuple of "goods" we want maximized (higher is
    better). `cost_fn(x)` returns "costs" we want minimized (lower is
    better), e.g. skill-point requirements. Pass `None` if there are no costs.

    Item A dominates item B iff:
        all(A.good_i >= B.good_i) and all(A.cost_i <= B.cost_i)
        and (strict in at least one dimension)
    """
    vectors = [vector_fn(it) for it in items]
    costs = [cost_fn(it) if cost_fn else () for it in items]

    n = len(items)
    keep = [True] * n
    for i in range(n):
        if not keep[i]:
            continue
        gi, ci = vectors[i], costs[i]
        for j in range(n):
            if i == j or not keep[j]:
                continue
            gj, cj = vectors[j], costs[j]
            ge = all(a >= b for a, b in zip(gj, gi))
            le = all(a <= b for a, b in zip(cj, ci))
            strict = any(a > b for a, b in zip(gj, gi)) or \
                     any(a < b for a, b in zip(cj, ci))
            if ge and le and strict:
                keep[i] = False
                break
    return [items[i] for i in range(n) if keep[i]]
