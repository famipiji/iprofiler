# iProfiler Tutorial Video — Demo Plan (Draft 2)

Sample document confirmed: `Document_4.pdf` — a freight tax invoice from MITSUI O.S.K. LINES (M) SDN BHD, billed to TOP GLOVE SDN BHD (Invoice No. MYEX20171240490, dated 27-12-2017, due 29-01-2018, total MYR 1,159.12). Maps to the built-in **invoice** document type. Target length: ~90 seconds (fits ElevenLabs free tier, ~10,000 characters/month).

## Step-by-step outline

| # | Screen / action | Screenshot to capture | Caption / narration line |
|---|---|---|---|
| 1 | Landing page, empty document list | Full page | "This is iProfiler — upload any document and it extracts the data automatically." |
| 2 | Click upload, select the freight invoice PDF | File picker + selected file | "Let's upload a freight invoice from MOL." |
| 3 | Upload in progress (SSE progress bar) | Progress bar mid-fill | "iProfiler classifies the document type, then extracts every field." |
| 4 | Extraction complete, document opened | Full extracted-fields view | "In seconds we get structured fields — invoice number, vendor, customer, due date, total — pulled straight from the page." |
| 5 | Highlight key fields: invoice_number (MYEX20171240490), vendor_name (MITSUI O.S.K. LINES), total_amount (MYR 1,159.12), due date (29-01-2018) | Close-up crop / zoom on field panel | "Each field is confidence-checked, right down to the exact invoice number and total." |
| 6 | Click a field to show source highlight on original doc | Split view: field + highlighted source | "Click any field to see exactly where it came from in the original document." |
| 7 | Closing shot | Full page, document list with new entry | "That's it — one upload, fully structured data, ready to use." |

## What I need from you before I execute this

1. ~~Confirm or swap the document type~~ — done, using the freight invoice above.
2. **Confirm the app is up** at `http://192.168.1.18:5000/` right now so I can drive it live.
3. **Approve or edit the script** — the narration lines above are drafts tailored to this invoice; tell me what to change before I generate ElevenLabs audio.
4. **Any UI states to avoid showing** — e.g. other tenants' documents already in the list, internal-only settings pages.

## Execution steps (once approved)

1. I drive the browser (Claude in Chrome) against the live instance and perform steps 1–7, capturing a screenshot at each.
2. I generate narration audio from the approved script via ElevenLabs (free tier).
3. I build a Remotion composition: pans/zooms between the captured screenshots, simulated cursor, text captions synced to the narration, rendered to `.mp4`.
4. I hand you the rendered file — optional finishing (music, trims) can be done in CapCut afterward.

## Notes

- This is a template — once validated, the same structure can be reused per document type for a short series.
- No continuous screen recording is used; visuals are discrete captures animated in Remotion (same technique tools like Arcade/Supademo use for product tours).
