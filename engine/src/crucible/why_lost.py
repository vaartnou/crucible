# pyright: basic
"""Offline, verifier-only SupplyGym comparison. Never imported by task or policy code."""

import argparse
import asyncio
import html
import json
import math
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np

from crucible.core.transcript import Transcript, read_transcript
from crucible.envs.supplygym.sim import ACT_DIM
from crucible.envs.supplygym.task import SupplyGymFreshTask, SupplyGymTask

COSTS = ("purchase", "holding", "expedite", "switching", "stockout")
RULES = {
    "expedite without stockout": "Paid expedite cost while the paired default lost zero units "
    "in that same week. This is a diagnostic, not proof that expediting was unnecessary later.",
    "backup approval": "A new alternative supplier was approved this week. Approval is a "
    "divergence, not necessarily a mistake.",
    "weight flip": "A supplier's effective weight crossed from below 1 to above 1, or vice "
    "versa, relative to the previous week (tolerance 0.000001). Returning to 1 is not a flip.",
    "late reaction": "After at least one full week with a public late-order or fill-ratio "
    "below 0.9 warning, controls changed while hidden direct-supplier health was below 0.8 "
    "both in the previous week and at this decision. A heuristic, not causal attribution.",
}


def validate_pair(agent: Transcript, default: Transcript) -> None:
    """A numeric seed alone is insufficient to establish a valid pair."""
    a, b = agent.header, default.header
    if a.task not in ("supplygym@1", "supplygym-fresh@1"):
        raise ValueError("only supplygym@1 and supplygym-fresh@1 are supported")
    for key in ("task", "seed", "repeat", "scoring_hash", "git_commit"):
        if getattr(a, key) != getattr(b, key):
            raise ValueError(f"runs differ in {key}")
    if a.git_commit == "unknown":
        raise ValueError("runs need a recorded git_commit")
    if a.salted or b.salted:
        raise ValueError("salted runs cannot be replayed from a seed; use unsalted local runs")
    for run in (agent, default):
        if run.footer.stop_reason == "error" or run.footer.error:
            raise ValueError("cannot report a failed episode")
        if run.footer.steps != len(run.steps):
            raise ValueError("transcript step count does not match footer")
        if [s.step for s in run.steps] != list(range(len(run.steps))):
            raise ValueError("transcript steps must be contiguous, starting at zero")


def snapshot(play: Any) -> dict[str, Any]:
    env = play.env
    status = play.status()
    return {
        "day": env.day,
        "controls": status["controls"],
        "signals": status["signals"],
        "warnings": [
            s["id"] for s in status["suppliers"] if s["late_orders"] or s["fill_ratio"] < 0.9
        ],
        "health": bool(np.any(env.tape.H[min(env.day, env.horizon - 1), :10] < 0.8)),
        "totals": dict(env.tot),
        "approved": set(env.approved),
    }


def week_row(play: Any, before: dict[str, Any], *, fallback: bool) -> dict[str, Any]:
    after = snapshot(play)
    delta = {k: after["totals"][k] - v for k, v in before["totals"].items()}
    return {
        **before,
        "end": after["day"],
        "controls": after["controls"],
        "approved": after["approved"],
        "new_approvals": sorted(after["approved"] - before["approved"]),
        "delta": delta,
        "fallback": fallback,
    }


async def replay(run: Transcript) -> dict[str, Any]:
    task = SupplyGymFreshTask() if run.header.task == "supplygym-fresh@1" else SupplyGymTask()
    if run.header.scoring_hash != task.scoring_hash:
        raise ValueError("saved scoring hash does not match this engine")
    await task.reset(run.header.seed)
    play = task._play
    assert play is not None
    weeks = []
    for step in run.steps:
        before = snapshot(play)
        result = await task.step(step.call)
        if result != step.result:
            raise ValueError(
                f"saved result differs from replay at step {step.step}; "
                "use the original engine/world revision"
            )
        if play.env.day != before["day"]:
            weeks.append(week_row(play, before, fallback=False))
    # Exactly the grader's remaining-week behavior: zero action resets standing controls.
    while play.env.day < play.env.horizon:
        before = snapshot(play)
        play.env.step(np.zeros(ACT_DIM))
        weeks.append(week_row(play, before, fallback=True))
    totals = play.env.summary()
    for key in ("profit", *COSTS, "lost_units"):
        saved = run.footer.score.breakdown.get(key)
        if (
            saved is None
            or not math.isfinite(saved)
            or not math.isclose(saved, totals[key], rel_tol=0, abs_tol=0.000001)
        ):
            raise ValueError(f"saved {key} does not match replay")
    return {"weeks": weeks, "totals": totals}


def tag_weeks(agent: list[dict[str, Any]], default: list[dict[str, Any]]) -> None:
    previous = None
    for a, b in zip(agent, default, strict=True):
        tags = []
        controls = a["controls"]
        a["divergent"] = controls != b["controls"] or a["approved"] != b["approved"]
        if a["delta"]["expedite"] > 0 and b["delta"]["lost"] == 0:
            tags.append("expedite without stockout")
        if a["new_approvals"]:
            tags.append("backup approval")
        if previous:
            old = previous["controls"]["supplier_weights"]
            new = controls["supplier_weights"]
            if any(
                (old[k] < 1 - 1e-6 and new[k] > 1 + 1e-6)
                or (old[k] > 1 + 1e-6 and new[k] < 1 - 1e-6)
                for k in old
            ):
                tags.append("weight flip")
            if (
                not a["fallback"]
                and previous["warnings"]
                and previous["health"]
                and a["health"]
                and controls != previous["controls"]
            ):
                tags.append("late reaction")
        a["tags"] = tags
        previous = a


def dollars(value: float) -> str:
    return f"{'-' if value < 0 else ''}${abs(value):,.2f}"


def escaped(value: Any) -> str:
    return html.escape(str(value), quote=True)


def waterfall(agent: Mapping[str, float], default: Mapping[str, float]) -> str:
    """Bridge default profit to agent profit: revenue delta minus each cost delta."""
    parts = [("Default profit", 0.0, default["profit"], default["profit"])]
    cursor = default["profit"]
    for key in ("revenue", *COSTS, "terminal_credit"):
        delta = (agent[key] - default[key]) * (1 if key in ("revenue", "terminal_credit") else -1)
        parts.append((key.replace("_", " ").title(), cursor, cursor + delta, delta))
        cursor += delta
    if not math.isclose(cursor, agent["profit"], rel_tol=0, abs_tol=1e-6):
        raise ValueError("waterfall does not reconcile to profit")
    parts.append(("Agent profit", 0.0, agent["profit"], agent["profit"]))
    lo = min(0.0, *(min(p[1:3]) for p in parts))
    hi = max(0.0, *(max(p[1:3]) for p in parts))
    scale = 280 / max(1, hi - lo)

    def y(v: float) -> float:
        return 330 - (v - lo) * scale

    svg = [
        '<svg viewBox="0 0 1200 400" role="img" '
        'aria-label="Profit waterfall from default to agent in dollars">'
    ]
    for i, (label, start, end, delta) in enumerate(parts):
        x = 30 + i * 130
        color = "#6589d8" if i in (0, len(parts) - 1) else ("#39bb92" if delta >= 0 else "#ef8a75")
        svg.append(
            f'<rect x="{x}" y="{min(y(start), y(end)):.3f}" width="100" '
            f'height="{max(1, abs(end - start) * scale):.3f}" fill="{color}">'
            f"<title>{label}: {dollars(delta)}</title></rect>"
        )
        svg.append(
            f'<text x="{x + 50}" y="{min(y(start), y(end)) - 10:.3f}" '
            f'text-anchor="middle">{dollars(delta)}</text>'
            f'<text x="{x + 50}" y="365" text-anchor="middle">{label}</text>'
        )
        if i < len(parts) - 1:
            svg.append(
                f'<path d="M{x + 100},{y(end):.3f} h30" stroke="#8893a7" stroke-dasharray="3 3"/>'
            )
    return "".join(svg) + "</svg>"


def render(agent_run: Transcript, agent: dict[str, Any], default: dict[str, Any]) -> str:
    tag_weeks(agent["weeks"], default["weeks"])
    counts = Counter(t for w in agent["weeks"] for t in w["tags"])
    gap = agent["totals"]["profit"] - default["totals"]["profit"]
    title = "Why the AI lost" if gap < 0 else "How the AI compared"
    rows = []
    for i, (a, b) in enumerate(zip(agent["weeks"], default["weeks"], strict=True), 1):
        badges = "".join(f'<span class="tag">{escaped(t)}</span>' for t in a["tags"])
        signals = {"news_and_rumors": a["signals"], "supplier_warnings": a["warnings"]}
        details = lambda obj: escaped(json.dumps(obj, indent=2, sort_keys=True))  # noqa: E731
        rows.append(
            f'<tr class="{"divergent" if a["divergent"] else ""}">'
            f"<td>W{i:02d}<br><small>Days {a['day']}-{a['end'] - 1}</small></td>"
            f"<td>{'Divergent' if a['divergent'] else 'Default controls'}{badges}"
            f"{'<span class=tag>Default completion</span>' if a['fallback'] else ''}</td>"
            f"<td><details><summary>Signals at decision</summary>"
            f"<pre>{details(signals)}</pre></details>"
            f"<small>Post hoc truth: direct supplier health &lt; 0.8: "
            f"{a['health']}</small></td>"
            f"<td><details><summary>Agent vs default</summary>"
            f"<pre>Agent\n{details(a['controls'])}\nDefault\n"
            f"{details(b['controls'])}</pre></details>"
            f"Lost units: {a['delta']['lost']:g} / {b['delta']['lost']:g}</td>"
            f"<td>{dollars(a['delta']['profit'] - b['delta']['profit'])}</td></tr>"
        )
    rules = "".join(
        f"<li><strong>{escaped(k)} · {counts[k]}</strong>: {escaped(v)}</li>"
        for k, v in RULES.items()
    )
    h = agent_run.header
    return f"""<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{title} · seed {h.seed}</title>
<style>
:root{{color-scheme:dark}}body{{margin:0;background:#101724;color:#e5eaf4;
font:16px/1.55 system-ui,sans-serif}}main{{max-width:1250px;margin:auto;padding:40px 24px}}
h1{{font-size:clamp(30px,5vw,52px);margin:8px 0}}h2{{margin-top:36px}}small,.muted{{color:#afbacd}}
.hero{{display:flex;gap:40px;flex-wrap:wrap;padding:24px 0}}.metric{{font-size:28px}}
.panel{{background:#192337;border:1px solid #33425c;border-radius:12px;padding:20px}}
.scroll{{overflow:auto}}svg{{width:100%;min-width:820px}}svg text{{fill:#e5eaf4;font-size:13px}}
table{{width:100%;border-collapse:collapse;min-width:880px}}th,td{{padding:16px 12px;
border-bottom:1px solid #33425c;text-align:left;vertical-align:top}}th{{color:#afbacd}}
tr.divergent{{background:#202c41}}.tag{{display:block;width:fit-content;border-radius:5px;
background:#3b3440;color:#ffd4b8;font-size:12px;padding:2px 7px;margin-top:5px}}
pre{{white-space:pre-wrap;overflow-wrap:anywhere;max-width:380px;font-size:12px}}
summary{{cursor:pointer}}li{{margin:12px 0}}@media print{{details>pre{{display:block}}}}
</style><main><div class="muted">CRUCIBLE / SUPPLYGYM / POST HOC ANALYSIS</div>
<h1>{title}</h1><p>{escaped(h.policy)} vs default · {escaped(h.task)} · seed {h.seed}
 · repeat {h.repeat}</p><div class="hero">
<div>Profit gap (agent - default)<div class="metric">{dollars(gap)}</div></div>
<div>Agent profit<div class="metric">{dollars(agent["totals"]["profit"])}</div></div>
<div>Default profit<div class="metric">{dollars(default["totals"]["profit"])}</div></div>
<div>Divergent weeks<div class="metric">{sum(w["divergent"] for w in agent["weeks"])}</div></div>
</div><section class="panel"><h2>Where the dollars went</h2>
<p>Start with default profit, add the revenue difference, subtract each cost difference,
and add the difference in terminal stock credits.
Green increases agent profit; coral decreases it. Tags do not allocate or double-count costs.</p>
<div class="scroll">{waterfall(agent["totals"], default["totals"])}</div></section>
<h2>Decision timeline</h2><p class="muted">Signals are the replayed public view at the start
of each week. Hidden truth and tags are computed only after the run. Dollar gaps include
effects of earlier decisions; they are not causal estimates.</p>
<div class="scroll"><table><thead><tr><th>Week</th><th>Divergence / tags</th><th>Signals</th>
<th>Controls / lost units (agent / default)</th><th>Profit gap</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table></div><h2>Rule-based tags and counts</h2><ul>{rules}</ul>
<p class="muted">Counts are weeks per type; a week may have several tags. No tag asserts
optimality. Default completion means the agent stopped and the grader completed the year.</p>
<footer class="muted">Source revision: {escaped(h.git_commit)}<br>
Scoring hash: {escaped(h.scoring_hash)}<br>Both saved transcripts were replayed and checked
against their tool results and final dollar totals. Unsalted runs only. No external assets.</footer>
</main></html>"""


def build(agent_path: Path, default_path: Path, out: Path) -> None:
    agent_run, default_run = read_transcript(agent_path), read_transcript(default_path)
    validate_pair(agent_run, default_run)
    agent, default = asyncio.run(replay(agent_run)), asyncio.run(replay(default_run))
    for week in default["weeks"]:
        c = week["controls"]
        if (
            week["new_approvals"]
            or any(v != 1 for v in c["supplier_weights"].values())
            or c["order_multiplier"] != 1
            or c["safety_stock_days"] != 0
            or c["expedite_fraction"] != 0
        ):
            raise ValueError("default run must use default controls throughout")
    page = render(agent_run, agent, default)
    if out.resolve() in (agent_path.resolve(), default_path.resolve()):
        raise ValueError("output must not overwrite an input transcript")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", required=True, type=Path, help="Saved agent JSONL transcript")
    parser.add_argument("--default", required=True, type=Path, help="Matching default JSONL")
    parser.add_argument("--out", type=Path, default=Path("why-lost.html"))
    args = parser.parse_args()
    try:
        build(args.agent, args.default, args.out)
    except (ValueError, OSError) as exc:
        parser.exit(2, f"Error: {exc}\n")
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
