"""
evals/rubric.py — explicit scoring guides, no default score of 3.
0 means "cannot score" and is excluded from means, never silently filled in.
"""
RUBRIC = {
    "accuracy": {
        "weight": 0.30,
        "guide": {5: "Every checkable claim is correct and current; numbers match their sources.",
                  4: "One minor inaccuracy or slightly dated figure; nothing misleading.",
                  3: "A few inaccuracies or unsupported numbers, but the main guidance holds.",
                  2: "Several wrong or invented claims a reader would act on.",
                  1: "Largely wrong, fabricated or outdated.",
                  0: "Cannot assess."}},
    "depth_actionability": {
        "weight": 0.25,
        "guide": {5: "Concrete steps, configs, thresholds, trade-offs and decision rules an engineer can apply today.",
                  4: "Mostly concrete; a few sections stay general.",
                  3: "Mix of concrete and generic advice.",
                  2: "Mostly generic; few specifics.",
                  1: "Platitudes only.",
                  0: "Cannot assess."}},
    "citation_quality": {
        "weight": 0.15,
        "guide": {5: "Claims cite specific primary sources (docs, papers, engineering blogs) that plausibly support them.",
                  4: "Good sources with a few generic or homepage links.",
                  3: "Citations present but often generic, secondary, or loosely related.",
                  2: "Few citations or many that cannot support the claim.",
                  1: "No meaningful citations.",
                  0: "Cannot assess."}},
    "clarity_structure": {
        "weight": 0.15,
        "guide": {5: "Scannable, logically ordered, tables where options are compared, no padding.",
                  4: "Clear with minor redundancy.",
                  3: "Readable but meandering or repetitive.",
                  2: "Hard to follow.",
                  1: "Incoherent.",
                  0: "Cannot assess."}},
    "honesty_calibration": {
        "weight": 0.15,
        "guide": {5: "Separates evidence from opinion, flags uncertainty, no invented incidents or statistics.",
                  4: "Mostly calibrated; one overconfident claim.",
                  3: "Some unhedged speculation presented as fact.",
                  2: "Frequent confident claims with no basis (e.g. anonymous 'incidents', made-up survey numbers).",
                  1: "Systematically overclaims or fabricates.",
                  0: "Cannot assess."}},
}


def rubric_text() -> str:
    lines = []
    for k, d in RUBRIC.items():
        lines.append(f"### {k} (weight {d['weight']})")
        lines += [f"- {s}: {g}" for s, g in sorted(d["guide"].items(), reverse=True)]
    return "\n".join(lines)


def weighted(scores: dict[str, int]) -> float | None:
    num = den = 0.0
    for k, d in RUBRIC.items():
        v = scores.get(k, 0)
        if v and v > 0:
            num += v * d["weight"]
            den += d["weight"]
    return round(num / den, 3) if den else None
