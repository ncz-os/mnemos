"""Deterministic consultation model selection."""


def select_tier_lineup(rows, tier):
    rows = [dict(row) for row in rows]
    if tier == "frontier":
        rows = [
            r
            for r in rows
            if (r.get("arena_rank") is not None and r["arena_rank"] <= 5) or (r["graeae_weight"] or 0) >= 0.95
        ]
    elif tier == "premium":
        rows = [
            r
            for r in rows
            if (r.get("arena_rank") is not None and 6 <= r["arena_rank"] <= 15)
            or (0.85 <= (r["graeae_weight"] or 0) < 0.95)
        ]
    else:
        rows = [
            r
            for r in rows
            if (r["graeae_weight"] or 0) >= 0.75
            and r["input_cost_per_mtok"] is not None
            and r["output_cost_per_mtok"] is not None
        ]
    seen: set[str] = set()
    out: list[dict] = []

    def rank(row):
        weight = -(row["graeae_weight"] or 0)
        arena = row.get("arena_rank") if row.get("arena_rank") is not None else float("inf")
        price = (row["input_cost_per_mtok"] or 0) + (row["output_cost_per_mtok"] or 0)
        return (
            (row["provider"], price, weight, arena, row["model_id"])
            if tier not in {"frontier", "premium"}
            else (row["provider"], weight, arena, row["model_id"])
        )

    for row in sorted(rows, key=rank):
        if row["provider"] not in seen:
            out.append(row)
            seen.add(row["provider"])
    return out
