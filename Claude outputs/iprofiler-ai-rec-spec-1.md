# iProfiler AI Rec — Demo Spec

Source: requirements from Johnson (boss), clarified and confirmed with Tisya. This is the single source of truth for building the PDF merge/split/export demo — use this instead of re-deriving requirements from chat history.

## 1. Merge order (test sample PDF)

Merge 5 sample documents into one PDF, in this exact order:

| Merged page(s) | Source sample | Pages |
|---|---|---|
| Page 1 | Sample 1 | 1 |
| Page 2 | Sample 2 | 1 |
| Pages 3–4 | Sample 5 | 2 |
| Page 5 | Sample 3 | 1 |
| Page 6 | Sample 4 | 1 |

Total: 6 pages, 5 source documents.

## 2. Document types (2, not 1)

**Out-patient Prescription** — applies to Sample 1, Sample 2, Sample 4 (merged pages 1, 2, 6)

| Field | Location | Required? |
|---|---|---|
| MRN Barcode | Top 1D barcode | Required |
| QR 5 | Bottom | Optional — Sample 2 is missing this sticker; a blank/missing value here must NOT be treated as a failed extraction |

**Out-patient Drug List** — applies to Sample 3, Sample 5 (merged pages 3–4, 5)

| Field | Location | Required? |
|---|---|---|
| Case No. | Barcode 1 | Required |
| MRN Barcode | Barcode 2 | Required |
| Barcode 3 | — | Required |
| QR 4 | Bottom left | Required — must be added to the extraction prompt/config, not present in older config |
| QR 5 | Bottom right | Required — must be added to the extraction prompt/config, not present in older config |

If these two doc types were created directly in iProfiler's own app/admin UI, confirm whether this coding session's database is the same one the live app writes to before assuming they don't exist — a prior check in this codebase found neither doc type in "this DB," which may mean this session is pointed at a different DB instance (e.g. local dev vs. live/staging) rather than the types genuinely not existing.

## 3. Document-splitting rule

**Trigger: MRN Barcode detected (any value) → split the current page onwards into a new document.**

This replaces an earlier draft of the rule that used QR 5 as the trigger. QR 5 was rejected as the trigger because it is not present on every sample (Sample 2 has no QR 5 sticker), which would cause Sample 2 to incorrectly merge into Sample 1's document instead of splitting out on its own. MRN Barcode is present on every sample, including Sample 2, so it reliably produces the correct split.

Expected split result on the 6-page merged PDF, using this rule:
- Page 1 → own document (Out-patient Prescription)
- Page 2 → own document (Out-patient Prescription; QR 5 field comes back blank — expected, not an error)
- Pages 3–4 → one combined 2-page document (Out-patient Drug List) — page 4 has no MRN Barcode of its own, so it correctly stays attached to page 3 rather than splitting again
- Page 5 → own document (Out-patient Drug List)
- Page 6 → own document (Out-patient Prescription)

Total: 5 documents from the 6-page merged PDF.

## 4. MRN Barcode value

The MRN value is the FULL scanned barcode string (e.g. `082614010184`), not the short human-readable number printed next to it on the sticker (e.g. `184`). Any logic reading "MRN barcode" must capture the full string.

## 5. Demo walkthrough (7 steps)

1. Show the 1 merged PDF (5 samples, including the 1 two-page document).
2. Upload it to iProfiler AI Rec.
3. System shows the split into 5 individual documents.
4. Open the first document (page 1 / Sample 1) — show its 1 page and its extracted fields.
5. Open the third document (pages 3–4 / Sample 5) — show the 2-page split evidence.
6. Export all 5 documents as PDFs.
7. Export matching XML data files, one per document.

## 6. Export / output naming convention

- `{doctype-name}_{mrn-barcode}.pdf`
- `{doctype-name}_{mrn-barcode}_DATA.xml`

Use the doc type's configured name (e.g. "Out-patient Prescription") and the full MRN barcode value (see #4). Do not use Case No. barcode for naming — it doesn't exist on the Prescription doc type, so it can't serve as a universal naming key across both types.

## 7. Open items / still needs confirmation from Johnson

- Explicit sign-off that the splitting rule is MRN Barcode (not QR 5) — this was Tisya's/Claude's inferred fix based on Johnson's own statement that MRN is "the only barcode consistent in all 5 documents," but Johnson has not confirmed this change in his own words yet.

## 8. Known issues tracker (from live testing)

Status as of last test round. Update this table as fixes are verified.

| # | Issue | Status |
|---|---|---|
| 1 | Split trigger: change from QR 5 to `mrn_barcode` (added as a NEW split_mode option, not replacing existing "barcode" mode used by other workflows) | ✅ Fixed & verified — 6-page merged PDF correctly splits into 5 documents (p1, p2, p3-4 combined, p5, p6) |
| 2 | Doc Types "Out-Patient Prescription" / "Out-Patient Drug List" not found by coding session's DB, despite being created in the live app's admin UI | ✅ Resolved — correct doc types are now being matched during testing |
| 3 | QR 5 field returns a false value ("184") on Sample 2 (Out-Patient Prescription, page 2) instead of empty/null, even though no QR code is physically present on that page | ✅ Fixed & verified — QR 5 now correctly shows "NOT DETECTED" instead of a guessed value |
| 4 | Drug List pages (p3-4, p5) classifying as "Unclassified" instead of "Out-Patient Drug List" | ✅ Fixed & verified — later tests show these pages correctly tagged "OUT-PATIENT DRUG LIST" |
| 5a | Export PDF filename used a random internal hash instead of `{doctype-name}_{mrn-barcode}.pdf` | ✅ Fixed & verified — e.g. `outpatient_prescription_082614010186.pdf` |
| 5b | Export XML filename still uses old pattern (`vaultmind_622.xml`) instead of `{doctype-name}_{mrn-barcode}_DATA.xml` | ⏳ Reported, still broken — PDF and XML export likely use separate filename logic, only PDF path was fixed |
| 6 | Extracted Fields panel display order doesn't match the field order configured in Doc Type manager (likely a general/shared component bug affecting all doc types, not just ours) | ✅ Fixed & verified — later test shows Extracted Fields panel order matches Doc Type manager order (Case No. → MRN Barcode → Barcode 3 → QR 4 → QR 5) |
| 7 | MRN Barcode (barcode 2) sometimes reads the wrong value on Out-Patient Drug List docs — one instance showed "0398f" (a clinic/form code, not a barcode) which self-corrected via the in-app "type it + apply for future docs" flow; a different instance (ticket 187 / merged p3-4) showed "MOEQEH2500007368" (the Order No. barcode) instead of the correct MRN value, likely because that page's queue-ticket sticker sits close to/overlapping the Case No. barcode, confusing position-based extraction | ⏳ Partially addressed via in-app self-correction; needs retest with a fresh upload to confirm the fix actually generalizes rather than being a one-off correction. See item 12 — this is likely a symptom of the root-cause issue below. |
| 8 | System flagged a detected field ("mrn barcode barcode") as a "new record type" not matching the existing configured "MRN Barcode (barcode 2)" field — looks like a field-name/key matching bug | ✅ Worked around — Tisya removed the duplicate field via the in-app chat ("Removed" option). Root cause (why the system detected a mismatched duplicate field in the first place) not investigated further. |
| 9 | Document History list sorts by completion timestamp (newest first), so split documents from the same source PDF can display out of page order (e.g. p5/p6 above p1/p2) — confusing for demo walkthroughs | ⏳ Requested as a UI polish item (sort/group by original page order), not yet addressed |
| 10 | PDF split mode UI: should "MRN Barcode" be its own flat option or nested inside a "Barcode / QR" dropdown? | ✅ Decision made — keep as its own separate, visible option. Matches the backend architecture (distinct `mrn_barcode` split_mode, not a QR sub-mode) and keeps the active mode visible at a glance for demo purposes. No change needed. |
| 11 | Document viewer doesn't support click-and-drag panning or scroll-to-zoom — feels static when zoomed in, can't move the page around to inspect different regions | ⏳ Requested; a follow-up attempt showed partial functionality (can't drag left specifically) — needs more detail from Tisya on exactly what does/doesn't work (zoom itself, drag direction, cursor state) before this can be narrowed down further |
| 12 | **Root cause candidate for items 3 and 7**: all fields in Doc Type manager (including MRN Barcode, QR 5, Case No., etc.) are configured as Type: "Text" — there is no distinct "Barcode/QR" field type. This means barcode-type fields are being read via AI/OCR-style document understanding rather than true barcode/QR decoding, which is why the system can "hallucinate" a value from nearby text/visual clutter instead of returning empty or the correct decoded value. Recommendation: add a genuine Barcode/QR field type that runs a true decoder (e.g. pyzbar/ZXing/OpenCV) first, falling back to AI/OCR reading only when no valid barcode pattern is found. | ⏳ Proposed as an architectural recommendation, not yet actioned — would likely resolve items 3 and 7 at the root instead of one-off patching |
