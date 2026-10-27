# About Me

**Name:** Tisya
**Email:** batrisyiaaiman00@gmail.com
**Project:** iProfiler (repo: Vaultmind) — a Flask + Qwen Vision app that extracts structured data from uploaded documents (invoices, passports, bank statements, etc.). See `CLAUDE.md` for architecture.

## Current goal: tutorial demo videos

I want to produce tutorial-style demo videos showing end users how iProfiler works — walking through uploading a document and reviewing the extracted results, using the actual running app rather than mockups.

### How I like to work on this

- I decide scope and direction: which document type, which steps, what the video should emphasize. Claude executes — drives the app, captures the visuals, assembles the video.
- I'll supply the sample documents and keep the app running for Claude to access live.
- Keep plans and check-ins concise. Confirm scope before building, but don't over-ask once direction is set.

### Recording pipeline (agreed approach)

There's no continuous screen-recording tool available, so the pipeline is capture-and-assemble rather than raw screen capture:

1. **Drive the app** — Claude uses browser automation (Claude in Chrome) against the live instance to perform the actual upload → extract → review steps.
2. **Capture screenshots** at each key step (not continuous video).
3. **Assemble in Remotion** — code-driven video composition: pans/zooms between screenshots, simulated cursor movement, text captions/callouts, rendered to `.mp4`.
4. **Voiceover** — ElevenLabs free tier (~10,000 characters/month, ~2–3 min of audio) for narration; keep scripts short enough to fit, or split across takes if a video runs long.
5. **CapCut (free tier)** — optional finishing pass only (music, trims), not the primary editing engine, since it has no automation surface.

### Video status

- First video: one document type, end-to-end (upload → extract → review), scope and sample doc TBD by Tisya.
- Format going forward: likely one video per document type, reusing this same template once the first one is validated.

## Notes for future sessions

- Live app instance: `http://192.168.1.18:5000/` (LM Studio/Ollama backend, per `docker-compose.yml`).
- Update this file as video goals, tooling choices, or working preferences change — don't let it go stale.
