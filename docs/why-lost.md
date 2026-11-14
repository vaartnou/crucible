# Why the AI lost: offline SupplyGym report

From the repository root, build a single HTML file from two **saved native JSONL
episode transcripts**, one agent and one default (`ss`):

```bash
uv run --no-dev --directory engine python -m crucible.why_lost --agent runs/agent/transcripts/EPISODE.jsonl --default runs/default/transcripts/EPISODE.jsonl --out ../why-lost.html
```

Paths after `--directory engine` are relative to `engine/`. Use the actual transcript
paths printed by your run; this command does not call an
LLM or create an evaluation. Open `why-lost.html` directly in any browser, including
offline. No server, JavaScript, fonts, CDN, or other assets are needed. Each report
compares one seed/repeat. It works for `supplygym@1` and `supplygym-fresh@1` on the
SupplyGym branch. It does not require PPO or the scoring reference policy.

Inputs must have matching task, seed, repeat, source commit and scoring hash. Unknown
source commits, failed or incomplete transcripts, and salted runs are rejected.
Salted Harbor worlds cannot be recovered from the numeric seed. Use unsalted local
eval transcripts; Harbor artifacts and summary CSV alone are not accepted.
The default is verified by its effective controls, not its policy name. All its
weeks must use default controls and never approve a backup. A default run that
immediately stops is valid because grading completes it on default controls.

The builder replays every saved call (including reads and refusals) and checks every
tool result against the transcript. It completes unplayed weeks exactly as the
grader does, then checks saved profit, all five costs and lost units against the
replay with absolute tolerance $0.000001. Run it with the original engine/world
revision; a changed simulator or data pack should fail replay validation. The page
records the input revision and scoring hash. Inputs are never modified.

## Tags

Counts are **weeks per type**, and tags can overlap. Thresholds and rules are also
printed in the generated page:

- **expedite without stockout**: the agent paid expedite cost and the paired default
  lost zero units in that same week. This does not prove expediting was unnecessary
  for later weeks.
- **backup approval**: a new alternative was approved that week. This records a
  divergence, not necessarily a mistake.
- **weight flip**: at least one effective supplier weight crossed from below 1 to
  above 1 or vice versa since the previous week, using tolerance 0.000001. Returning
  to 1 does not count. Changes to inactive supplier weights still count.
- **late reaction**: controls changed after at least one full week with a public
  late-order or fill-ratio < 0.9 warning, and hidden direct-supplier health was < 0.8
  at both the preceding and current decision. Grader completion is excluded. This
  conservative heuristic does not claim the warning caused the action.

Any difference in effective controls or approved supplier sets marks a divergent
week, whether tagged or not. A timeline row shows the public signals available at
the start, both controls, lost units and the weekly profit difference. Weeks after
the agent stops are explicitly marked as default completion; earlier decisions
can still affect their inventory and costs.

## Dollar accounting and visibility

The waterfall starts at default profit, adds `agent revenue - default revenue`,
and subtracts the agent-minus-default differences in purchase, holding, expedite,
switching and stockout costs, then adds the difference in terminal inventory credits. It ends at agent profit. Revenue is necessary: lost
sales affect both revenue and the separate stockout penalty. The builder asserts
that this bridge reconciles within $0.000001; it does not silently add a residual.
Displayed amounts round to cents. Negative gaps mean the agent lost; ties and wins
use the neutral page title “How the AI compared.”

Tags and hidden health are computed only in `crucible.why_lost`, after episodes have
ended. Task tools, `set_controls` outputs, observations and policies are unchanged.
The HTML is a verifier/demo artifact containing post hoc hidden truth: do not give
it to an agent during an evaluation. Cost components explain accounting, not the
causal dollar value of individual tags.
