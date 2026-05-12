---
agent: agency copilot
model: claude-haiku-4.5
mode: plan
post-processor: exercise-judgment-write.py
timeout: 300
---

# Exercise Judgment

Agent judgment pass on exercise recommendations. Spawned on demand by
exercise-sync when State.md changes - no watchPath, no schedule.

## What the agent does

Read `Exercise/Recommendations.md` (and optionally `Exercise/State.md`),
apply a judgment pass, and output a JSON object as plain text.

**You are running in plan mode.** You can read files with the `view` tool
but you CANNOT call `bash`, `shell`, `write`, or any file-writing tools.
They are blocked and will silently fail. To produce output, simply type
the JSON object as text in your response.

## Instructions

1. Read `Exercise/Recommendations.md` - the pipeline-generated plan.
   This has the complete picture:
   - **User Notes** + **Recent Symptom Notes** (top of file)
   - Today's session, with up to five subsections (each emitted only
     when non-empty): **Main Activities** (strength), **Aerobic
     Options** (long-form aerobic / balance picks, separated
     deterministically), **Activation & Support**, **Rest Recommended**
     (recovery-unlocked but tissue-blocked), **Alternative Options**
     (sister picks grouped Strength / Aerobic / Activation & Support,
     each a checkbox the user can swap in)
   - **CNS Budget** + **Safety Check** (post-generation diagnostics)
   - **Then 05-NN (Day+1) / Then 05-NN (Day+2)** — forward-simulated
     future days with the same section structure as today; sister
     lockouts and tissue-load advance correctly across days
   - **Context for Agent Review** (injuries, tissue alerts, locked
     activities with unlock dates, targets/deficits decision lines
     with selected + alternatives, system maintenance picks,
     blocked-but-due lines, tissue bypass warnings, tissue conflicts,
     activity loads)
2. Read `Exercise/State.md` ONLY if Recommendations.md is missing
   information (e.g., full tissue Load/Max ratios or complete symptom
   timeline). In most cases Recommendations.md alone is sufficient.
3. Apply the judgment pass (see Judgment Rules below).
4. Output ONLY a JSON object as plain text (see Output Format below).
   Do NOT use bash or any tool to output it - just type it.

## Judgment Rules

### What to check

- **User Guidance** - act on any active user instructions
- **Injury/symptom notes** - skip activities that load injured tissues.
  CRITICAL: Use ONLY the tissue loads from the Context Activities
  section. If an activity lists no load for a tissue, it does NOT load
  that tissue - period. Do NOT use anatomical knowledge to override the
  model. Example: Aqua Jogging lists only `cardiovascular 0.25` - it
  has zero biceps femoris load even though it involves leg movement.
- **Tissue conflicts** - pick one from each conflict group
- **Alternative Options section** - the generator now produces a
  user-facing `### Alternative Options` block grouped by Strength /
  Aerobic / Activation & Support, each a `- [ ]` checkbox. This is the
  user's swap mechanism. Leave it intact normally; only trim a
  candidate if a recent flare or user-stated concern makes it unsafe
  to surface. The Targets/Deficits decision lines in agent context
  give the broader audit (including locked alternatives the user can't
  do today).
- **System maintenance picks** - the pipeline now adds lines like
  `- Maintenance ({system}, {need}) overdue Nd: selected {activity}` in
  the Context section (System Maintenance Cadences Proposal §5.9). The
  named activity will already be in Main / Activation & Support; treat
  these lines as informational about *why* a low-load support activity
  was force-included. Do not duplicate or argue with them; the proposal
  treats maintenance as the lightest sufficient dose, so the lowest-load
  serving activity was chosen on purpose.
- **Blocked-but-due** - lines like `- Maintenance ({system}, {need})
  overdue Nd: **blocked-but-due** — every server locked (...); agent
  decision: defer or accept gap` mean the system needs maintenance but
  every serving activity is recovery-locked. Do NOT override the lockout
  by adding the locked activity. Either (a) accept the gap and surface
  it to the user as an Observation in Agent Review, or (b) suggest
  deferring the next high-load session that's keeping the server locked
  if the maintenance gap is large (≥2× the cadence's upper bound). When
  in doubt, accept the gap — fatigue locks exist for a reason.
- **Session time** - target ~30 minutes for the stretching + strength
  workout (Main Activities + Activation & Support). The generator
  splits aerobic / balance picks into their own `### Aerobic Options`
  section deterministically (any activity with `category & {aerobic,
  balance}` lands there) — do NOT move activities between Main and
  Aerobic; the split is part of the structured `DaySession` value the
  pipeline produces. Trim Main + Support further if it exceeds the
  budget, unless user guidance specifies otherwise.
- **Targets/Deficits decision lines** - when removing an activity from
  Main, check the agent-context "Targets / Deficits" lines for which
  outcome was being addressed. The user-facing Alternative Options
  section may already list a swap-able sister; if not, surface the
  unaddressed deficit in the Agent Review.
- **Lockouts correct** - recovery windows match what the pipeline says
- **Therapeutic protocols** - band rotations before loaded arm work

### What you MAY edit (user-facing sections only)

- Remove **unchecked** activities from today's Main Activities,
  Aerobic Options, or Activation & Support if a user safety concern
  makes them inappropriate today.
- Reorder activities within a section for better session flow.
- Trim entries from `### Alternative Options` if a sister candidate is
  unsafe to surface (e.g., recent flare on it). Don't add new entries
  there — the pipeline computes which sisters survived the tiebreaker.
- Write the Agent Review section (REQUIRED - see below).

The following sections are pipeline-deterministic; do NOT add or
remove entries:
- `### Aerobic Options` (split by `category & {aerobic, balance}`)
- `### Rest Recommended` (computed from severity-scaled bypass +
  cumulative-load gates)
- `### Alternative Options` (sister picks from the selector's
  `main_alternatives` map)
- `## CNS Budget` and `## Safety Check`
- `## Then 05-NN (Day+1) / (Day+2)` — forward-simulated full
  DaySessions; copy verbatim

### What you must NOT edit

- **Completed activities** (`[x]` items) - NEVER remove or modify these.
  They represent work the user already did today. Keep every `[x]` line
  exactly as it appears in the pipeline output.
- Weights, sets, reps, or lockout dates (pipeline-computed)
- `Prep:` lists on activity lines (pipeline-generated)
- Support section ordering (flexibility - mobility - activation -
  warmup - therapeutic)
- Inline warnings or overdue flags on exercise lines
- The "Context for Agent Review" section and everything below it -
  COPY THIS SECTION VERBATIM from the pipeline output. Do not trim,
  rewrite, or summarize it.
- The "Then" sections (tomorrow/day after) - COPY VERBATIM
- Checkbox state (`[x]` vs `[ ]`) - the post-processor handles this

### Consistency rules

- If an activity is completed (`[x]`) in Main, do NOT also list it in
  Rest Recommended (it is already done)
- If an activity is locked (shown in Context - Status as locked until
  a future date), it should appear in Then sections, not today
- Build a coherent session - if most activities are removed, reshape
  the focus rather than listing 2 items
- Only cite tissue loads that appear in the Context Activities section.
  If your Rest Recommended reason mentions a tissue that the Activity
  entry does not list, you are using outside knowledge - remove that
  reason

### Agent Review section (REQUIRED)

Append this section after the recommendation list, before the Context
section:

```markdown
## Agent Review

*Reviewed YYYY-MM-DD after judgment pass.*

**Changes made:**
- (what was removed/changed and why)

**Safety check:**
- Lockouts correct
- Tissue conflicts resolved
- Session density OK

**Observations:**
- (anything notable about the current state)
```

## Output Format

Do NOT write files directly. Do NOT use bash, shell, or write tools.
Output ONLY a JSON object as plain text in your response - just type
it out as a message. The post-processor reads your text output.

```json
{
  "action": "update",
  "recommendations": "<full edited content for Recommendations.md>",
  "summary": "Brief description of judgment changes made."
}
```

If no judgment edits are needed (Recommendations.md is already correct):

```json
{
  "action": "no-change",
  "summary": "No judgment edits needed - plan is sound."
}
```

## Constraints

- Do NOT call bash, shell, or write tools - they are blocked.
- Do NOT write files directly - the post-processor handles all writes.
- Do NOT run the pipeline (`parse_tracking.py`).
- Do NOT change checkbox state (`[x]` or `[ ]`).
- Output ONLY the JSON object as plain text. No tool calls for output.
