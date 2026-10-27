import os
import io
from datetime import datetime
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
from reportlab.platypus import KeepTogether


# ── Colour palette (matches VaultMind blue theme) ─────────────────────────────
BLUE_DARK   = colors.HexColor("#0052a3")
BLUE_MID    = colors.HexColor("#0066cc")
BLUE_LIGHT  = colors.HexColor("#e6f2ff")
BLUE_BORDER = colors.HexColor("#b3d9ff")
TEXT_DARK   = colors.HexColor("#0a1a33")
TEXT_MUTED  = colors.HexColor("#5a7a99")
BG_LIGHT    = colors.HexColor("#f0f8ff")
SUCCESS     = colors.HexColor("#0080ff")
WARNING     = colors.HexColor("#ff9500")
ERROR       = colors.HexColor("#cc0000")
WHITE       = colors.white


def confidence_color(confidence: str):
    return {
        "high":   SUCCESS,
        "medium": WARNING,
        "low":    ERROR,
    }.get(confidence, TEXT_MUTED)


def doc_type_label(doc_type: str) -> str:
    labels = {
        "invoice":        "Invoice",
        "receipt":        "Receipt",
        "id_card":        "ID Card",
        "passport":       "Passport",
        "contract":       "Contract",
        "medical_report": "Medical Report",
        "bank_statement": "Bank Statement",
        "form":           "Form",
        "letter":         "Letter",
        "other":          "Other",
    }
    return labels.get(doc_type, doc_type.replace("_", " ").title())


def _xml_escape(s: str) -> str:
    """Escape XML special chars so plain text is safe inside ReportLab Paragraph markup."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def flatten_fields(fields: dict, prefix: str = "") -> list:
    """Flatten nested fields dict into list of (key, value) tuples."""
    rows = []
    for k, v in fields.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            rows.extend(flatten_fields(v, key))
        elif isinstance(v, list):
            rows.append((key, ", ".join(str(i) for i in v) if v else "—"))
        else:
            rows.append((key, str(v) if v is not None and v != "" else "—"))
    return rows


def _fallback_export_pdf(doc_dict: dict, error: str) -> bytes:
    """Minimal plain-text PDF used when the styled export fails to build -
    matches the fallback convention already used by the sibling
    pdf_highlighted_service.py/pdf_searchable_service.py modules, so callers
    always get a valid PDF back instead of a raised exception."""
    buf = io.BytesIO()
    pdf = SimpleDocTemplate(buf, pagesize=A4, leftMargin=20*mm, rightMargin=20*mm, topMargin=20*mm, bottomMargin=20*mm)
    styles = getSampleStyleSheet()
    story = [
        Paragraph("VaultMind — Export failed, showing a plain-text fallback", styles["Heading2"]),
        Paragraph(f"Document ID: {doc_dict.get('id', '—')} · Filename: {doc_dict.get('filename', '—')}", styles["Normal"]),
        Spacer(1, 4*mm),
        Paragraph(f"Reason: {_xml_escape(str(error))}", styles["Normal"]),
        Spacer(1, 4*mm),
        Paragraph("Extracted fields:", styles["Heading3"]),
    ]
    for key, val in flatten_fields(doc_dict.get("fields") or {}):
        story.append(Paragraph(f"{_xml_escape(key)}: {_xml_escape(val)}", styles["Normal"]))
    pdf.build(story)
    return buf.getvalue()


def generate_export_pdf(doc_dict: dict) -> bytes:
    """
    Generate a styled result PDF from a document dict.
    Returns PDF as bytes.
    """
    try:
        return _build_export_pdf(doc_dict)
    except Exception as e:
        print(f"[Export] Styled PDF build failed for doc {doc_dict.get('id')}: {e}")
        return _fallback_export_pdf(doc_dict, str(e))


def _build_export_pdf(doc_dict: dict) -> bytes:
    buffer = io.BytesIO()

    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=20*mm,
        rightMargin=20*mm,
        topMargin=20*mm,
        bottomMargin=20*mm,
    )

    W = A4[0] - 40*mm  # usable width

    styles = getSampleStyleSheet()

    # ── Custom styles ──────────────────────────────────────────────────────────
    style_h1 = ParagraphStyle("h1",
        fontName="Helvetica-Bold", fontSize=22,
        textColor=BLUE_DARK, spaceAfter=2*mm, leading=26)

    style_subtitle = ParagraphStyle("subtitle",
        fontName="Helvetica", fontSize=10,
        textColor=TEXT_MUTED, spaceAfter=0)

    style_section = ParagraphStyle("section",
        fontName="Helvetica-Bold", fontSize=8,
        textColor=BLUE_MID, spaceBefore=6*mm, spaceAfter=3*mm,
        letterSpacing=1.5, leading=12)

    style_body = ParagraphStyle("body",
        fontName="Helvetica", fontSize=9,
        textColor=TEXT_DARK, leading=14, spaceAfter=2*mm)

    style_raw = ParagraphStyle("raw",
        fontName="Courier", fontSize=8,
        textColor=TEXT_DARK, leading=13,
        backColor=BG_LIGHT, borderPad=4)

    style_footer = ParagraphStyle("footer",
        fontName="Helvetica", fontSize=8,
        textColor=TEXT_MUTED, alignment=TA_CENTER)

    story = []

    # ── Header bar ─────────────────────────────────────────────────────────────
    header_data = [[
        Paragraph('<font color="#0052a3"><b>Vault</b></font><font color="#0066cc"><b>Mind</b></font>', ParagraphStyle("logo", fontName="Helvetica-Bold", fontSize=18, textColor=BLUE_DARK, leading=22)),
        Paragraph('Document Analysis Result<br/><font color="#5a7a99" size="8">Powered by Qwen Vision · LM Studio</font>', ParagraphStyle("hdr_right", fontName="Helvetica", fontSize=10, textColor=TEXT_DARK, alignment=TA_RIGHT, leading=16)),
    ]]
    header_table = Table(header_data, colWidths=[W*0.5, W*0.5])
    header_table.setStyle(TableStyle([
        ("ALIGN",       (0,0), (0,0), "LEFT"),
        ("ALIGN",       (1,0), (1,0), "RIGHT"),
        ("VALIGN",      (0,0), (-1,-1), "MIDDLE"),
        ("BACKGROUND",  (0,0), (-1,-1), BLUE_LIGHT),
        ("ROUNDEDCORNERS", [4]),
        ("TOPPADDING",  (0,0), (-1,-1), 5*mm),
        ("BOTTOMPADDING",(0,0), (-1,-1), 5*mm),
        ("LEFTPADDING", (0,0), (0,0), 5*mm),
        ("RIGHTPADDING",(-1,-1), (-1,-1), 5*mm),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 5*mm))

    # ── Document info strip ────────────────────────────────────────────────────
    created_at = doc_dict.get("created_at", "")
    if created_at:
        try:
            dt = datetime.fromisoformat(created_at)
            created_str = dt.strftime("%d %b %Y, %H:%M")
        except:
            created_str = created_at
    else:
        created_str = "—"

    doc_type   = doc_dict.get("document_type", "other")
    confidence = doc_dict.get("confidence", "low")
    filename   = doc_dict.get("filename", "—")
    file_type  = doc_dict.get("file_type", "").upper()

    conf_color = confidence_color(confidence)

    info_data = [[
        Paragraph(f'<b>File</b><br/><font size="9">{filename}</font>', style_body),
        Paragraph(f'<b>Type</b><br/><font size="9">{file_type}</font>', style_body),
        Paragraph(f'<b>Document Type</b><br/><font size="9" color="{BLUE_MID.hexval()}">{doc_type_label(doc_type)}</font>', style_body),
        Paragraph(f'<b>Confidence</b><br/><font size="9" color="{conf_color.hexval()}">{confidence.upper()}</font>', style_body),
        Paragraph(f'<b>Processed</b><br/><font size="9">{created_str}</font>', style_body),
    ]]
    info_table = Table(info_data, colWidths=[W*0.28, W*0.1, W*0.22, W*0.18, W*0.22])
    info_table.setStyle(TableStyle([
        ("BACKGROUND",   (0,0), (-1,-1), WHITE),
        ("BOX",          (0,0), (-1,-1), 0.5, BLUE_BORDER),
        ("INNERGRID",    (0,0), (-1,-1), 0.5, BLUE_BORDER),
        ("TOPPADDING",   (0,0), (-1,-1), 3*mm),
        ("BOTTOMPADDING",(0,0), (-1,-1), 3*mm),
        ("LEFTPADDING",  (0,0), (-1,-1), 3*mm),
        ("RIGHTPADDING", (0,0), (-1,-1), 3*mm),
        ("VALIGN",       (0,0), (-1,-1), "TOP"),
    ]))
    story.append(info_table)
    story.append(Spacer(1, 5*mm))

    # ── Extracted Fields ───────────────────────────────────────────────────────
    story.append(Paragraph("EXTRACTED FIELDS", style_section))
    story.append(HRFlowable(width="100%", thickness=0.5, color=BLUE_BORDER, spaceAfter=3*mm))

    fields = doc_dict.get("fields", {})
    flat_fields = flatten_fields(fields) if fields else []

    if flat_fields:
        field_rows = [
            [
                Paragraph(f'<font color="{TEXT_MUTED.hexval()}">{_xml_escape(key.replace("_", " ").title())}</font>',
                    ParagraphStyle("fk", fontName="Helvetica", fontSize=8, textColor=TEXT_MUTED, leading=12)),
                Paragraph(f'<b>{_xml_escape(val)}</b>' if val != "—" else '<font color="#5a7a99">—</font>',
                    ParagraphStyle("fv", fontName="Helvetica", fontSize=8, textColor=TEXT_DARK, leading=12)),
            ]
            for key, val in flat_fields
        ]

        # Chunk into 2 columns for compact layout
        left_rows  = field_rows[:len(field_rows)//2 + len(field_rows)%2]
        right_rows = field_rows[len(field_rows)//2 + len(field_rows)%2:]

        col_w = (W - 6*mm) / 2

        def make_field_table(rows):
            t = Table(rows, colWidths=[col_w*0.42, col_w*0.58])
            t.setStyle(TableStyle([
                ("BACKGROUND",    (0,0), (0,-1), BLUE_LIGHT),
                ("BACKGROUND",    (1,0), (1,-1), WHITE),
                ("BOX",           (0,0), (-1,-1), 0.5, BLUE_BORDER),
                ("INNERGRID",     (0,0), (-1,-1), 0.5, BLUE_BORDER),
                ("TOPPADDING",    (0,0), (-1,-1), 2.5*mm),
                ("BOTTOMPADDING", (0,0), (-1,-1), 2.5*mm),
                ("LEFTPADDING",   (0,0), (-1,-1), 3*mm),
                ("RIGHTPADDING",  (0,0), (-1,-1), 3*mm),
                ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
            ]))
            return t

        if right_rows:
            two_col = Table(
                [[make_field_table(left_rows), make_field_table(right_rows)]],
                colWidths=[col_w + 3*mm, col_w + 3*mm],
            )
            two_col.setStyle(TableStyle([
                ("VALIGN",      (0,0), (-1,-1), "TOP"),
                ("LEFTPADDING", (0,0), (-1,-1), 0),
                ("RIGHTPADDING",(0,0), (-1,-1), 0),
                ("TOPPADDING",  (0,0), (-1,-1), 0),
                ("BOTTOMPADDING",(0,0), (-1,-1), 0),
                ("INNERGRID",   (0,0), (-1,-1), 0, colors.white),
            ]))
            story.append(two_col)
        else:
            story.append(make_field_table(left_rows))
    else:
        story.append(Paragraph("No fields extracted.", style_body))

    story.append(Spacer(1, 5*mm))

    # ── Raw Extracted Text ─────────────────────────────────────────────────────
    story.append(Paragraph("RAW EXTRACTED TEXT", style_section))
    story.append(HRFlowable(width="100%", thickness=0.5, color=BLUE_BORDER, spaceAfter=3*mm))

    raw_text = doc_dict.get("raw_text", "") or "(no text extracted)"
    raw_safe = _xml_escape(raw_text)
    # Wrap in background box
    raw_data = [[Paragraph(raw_safe, style_raw)]]
    raw_table = Table(raw_data, colWidths=[W])
    raw_table.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,-1), BG_LIGHT),
        ("BOX",           (0,0), (-1,-1), 0.5, BLUE_BORDER),
        ("TOPPADDING",    (0,0), (-1,-1), 4*mm),
        ("BOTTOMPADDING", (0,0), (-1,-1), 4*mm),
        ("LEFTPADDING",   (0,0), (-1,-1), 4*mm),
        ("RIGHTPADDING",  (0,0), (-1,-1), 4*mm),
    ]))
    story.append(raw_table)
    story.append(Spacer(1, 8*mm))

    # ── Footer ─────────────────────────────────────────────────────────────────
    story.append(HRFlowable(width="100%", thickness=0.5, color=BLUE_BORDER, spaceAfter=3*mm))
    story.append(Paragraph(
        f'Generated by VaultMind &nbsp;·&nbsp; Document ID: {doc_dict.get("id", "—")} &nbsp;·&nbsp; {datetime.utcnow().strftime("%d %b %Y %H:%M")} UTC',
        style_footer
    ))

    doc.build(story)
    return buffer.getvalue()