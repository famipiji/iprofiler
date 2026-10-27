---
name: iprofiler-morning
description: "Triage today's iProfiler backlog directly from Notion, start the top 3 non-security Performance/Bug/DB-fix items, and generate the local task file (with a Mermaid system-flow diagram) plus the VS Code execution prompt to fix them. Use whenever the user invokes /iprofiler-morning by name, or asks to set up their iProfiler morning triage, pick today's iProfiler tasks, or start the day's iProfiler work. Distinct from the built-in \"morning\" skill (the visual daily-brief) — this one is specifically the Notion backlog triage workflow."
---

# /iprofiler-morning — daily iProfiler backlog triage

This is the start-of-day ritual for iProfiler: instead of me picking tasks from memory or the user re-explaining priorities each morning, I pull live from the 'iProfiler Backlog' Notion database, mark today's picks as started right there, and hand off a ready-to-run local task file. The Notion database and the local `.tasks/` file are two views of the same commitment — this skill is what keeps them in sync at the start of the day, the same way the `/task` skill keeps them in sync at the end.

## 1. Pull candidates from Notion

Query the connected 'iProfiler Backlog' Notion database for everything not already `Status: Done`.

**Exclude security-flavored items.** The database has no dedicated "Security" field, so judge this from the Task Name/Description text: skip anything naming XSS, CSRF, SSRF, IDOR, auth/authN/authZ, TLS/certificate verification, credentials/secrets/passwords, tenant scoping or cross-tenant leakage, injection, CORS, encryption, or privilege/RCE concerns. These matter, but they're a different kind of work (and often a different reviewer) than the performance/stability work this triage is for — leave them in the backlog for a dedicated security pass rather than silently mixing them in.

**Prefer items that are genuinely Performance Bottlenecks, System Bugs, or DB fixes.** Again, judge this from the text rather than a field — look for latency/caching/N+1-query/blocking-call/memory-growth language (performance), unhandled-exception/crash/race-condition/silent-failure language (system bugs), and migration/query/index/constraint/connection-pool language (DB). A item can straddle categories; that's fine, the point is ranking signal, not a strict taxonomy.

**Rank and pick 3.** Sort matching candidates by `Priority` (High, then Medium, then Low) and take the top 3. Don't hard-filter on `Priority = High` — treat "as HIGH priority" as the *ranking* these categories get among what's actually open, not a literal field filter. High-priority work gets cleared in bursts (it has been, this project), so a literal filter would return nothing on a day when the High tier is empty; ranking degrades gracefully to Medium and Low instead of coming up empty when there's still real work to triage.

If genuinely fewer than 3 non-security Performance/Bug/DB items remain anywhere in the backlog, say so plainly and pick what's actually there — don't pad the count with security items or unrelated ones just to hit 3.

## 2. Start the 3 picks in Notion

For each of the 3 selected pages, use the Notion page-update tool to set:
- `Status` → `In Progress`
- `Started Date` → today

Do this before writing the local file, so if something interrupts the run, Notion already reflects which 3 are actually being worked today — the local file is a convenience, Notion is the source of truth.

## 3. Write today's task file

Create (or overwrite, if re-run same-day) `.tasks/YYYY-MM-DD-tasks.md` in the connected local project folder, using today's date. Reach the folder via the device bridge the same way the `/task` skill does — check `mcp__remote-devices__device_list_dir` if you're not sure which connected folder has `.tasks/` in it.

For each of the 3 tasks, write a section in the same shape the `/task` skill expects to consume later:

```
### Task N: <title>

**Notion Reference:** <page URL>

**Target File:** `<file from the Description's "File: ..." prefix>`

**Issue Summary:**
<the Description text, minus the "File: X." prefix>

**Step-by-step fix logic:**
1. ...
2. ...
...
N. Verify: <a concrete way to confirm the fix worked>
```

Write real step-by-step fix logic from the actual issue — don't just restate the Issue Summary as a single step. If the local folder is connected, it's worth a quick look at the target file to ground the steps in the real code rather than guessing at line numbers from a possibly-stale Description.

**Then add one Mermaid flowchart block** placed after the three task sections, titled "System Process Mapping." It should show where today's 3 target files sit in iProfiler's request/data flow — not a generic diagram, the real one: Frontend (`templates/index.html`) → API Layer (Flask routes) → Processing/AI (`qwen_service.py`, OCR/PDF services) → Data & Infra (MySQL, retention) → External (iVault), with today's 3 target modules visually called out (e.g. a distinct fill color or a `%% today` comment) so it's obvious at a glance which part of the system is getting attention. Keep it to the modules actually relevant to today's 3 tasks plus enough surrounding context to place them — not the entire system every time.

## 4. Update the index

Append a row to `.tasks/INDEX.md` for today's file: date, task count (3), the target modules, a link to the new file, and status `To Do`. Match the table format already used in that file from prior days.

## 5. Output the execution prompt

End by printing the exact prompt the user can paste into Claude Code's VS Code terminal to implement the 3 tasks. Something in this shape, adapted to today's actual file and tasks:

```
claude "Implement Tasks 1-3 in .tasks/YYYY-MM-DD-tasks.md. Follow each task's step-by-step fix logic exactly, ground changes in the current code (line numbers in the file may have shifted), and run any existing tests/lint after each fix. Don't touch Notion — task closure is handled separately via /task."
```

This is the handoff point: Notion is updated, the task file is written, and this prompt is what actually gets the fixes coded. This skill doesn't write the fixes itself — that's deliberately a separate step (in VS Code, via Claude Code) so the person is in the loop on the actual code changes.

## Recurring setup

If the user asks to run this every weekday instead of on demand, set it up as a scheduled task (never the local cron tools — see the scheduled-task guidance in the system prompt) firing on weekday mornings in the user's home timezone. Infer the day's date fresh at each firing; don't bake today's date into the scheduled prompt.

## Notes

- This is a separate skill from the built-in "morning" skill (the visual HTML daily-brief with calendar/email/chat). That one still exists and still triggers on `/morning` — this one triggers on `/iprofiler-morning` and does the Notion backlog triage instead. If the user's request is ambiguous between the two, ask rather than guessing which one they mean.
- If the Notion connector or the local device bridge isn't available when this runs, say so plainly and do as much of the rest as possible (e.g. Notion updates without a task file, or vice versa) rather than failing the whole run silently.
