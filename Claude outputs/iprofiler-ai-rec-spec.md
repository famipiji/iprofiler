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
