"""
Investigate PDF text layers in uploaded documents.
Reports whether PDFs have native text layers or are image-only.
"""
import os
import fitz  # PyMuPDF

UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "uploads")

def analyze_pdf(pdf_path):
    """Analyze if a PDF has native text layer or is image-only."""
    try:
        doc = fitz.open(pdf_path)
        total_pages = len(doc)
        pages_with_text = 0
        total_text_items = 0

        for page_num in range(total_pages):
            page = doc[page_num]
            # Get text content - if it has native text, this will have items
            text_content = page.get_text("text").strip()
            words = page.get_text("words")

            if text_content or words:
                pages_with_text += 1
                total_text_items += len(words)

        doc.close()

        has_text_layer = pages_with_text > 0
        is_digital = total_text_items > (total_pages * 5)  # Threshold: >5 words/page avg

        return {
            "total_pages": total_pages,
            "pages_with_text": pages_with_text,
            "total_text_items": total_text_items,
            "avg_items_per_page": total_text_items / total_pages if total_pages > 0 else 0,
            "has_text_layer": has_text_layer,
            "is_digital_pdf": is_digital,
            "is_scanned_pdf": has_text_layer and not is_digital,
            "is_image_only": not has_text_layer
        }
    except Exception as e:
        return {"error": str(e)}

def main():
    if not os.path.exists(UPLOAD_FOLDER):
        print(f"Upload folder not found: {UPLOAD_FOLDER}")
        return

    pdf_files = [f for f in os.listdir(UPLOAD_FOLDER) if f.lower().endswith('.pdf')]

    if not pdf_files:
        print(f"No PDF files found in {UPLOAD_FOLDER}")
        return

    print(f"Found {len(pdf_files)} PDF files in uploads folder\n")
    print("="*80)

    digital_pdfs = []
    scanned_pdfs = []
    image_only_pdfs = []

    for pdf_file in pdf_files[:10]:  # Analyze first 10 PDFs
        pdf_path = os.path.join(UPLOAD_FOLDER, pdf_file)
        result = analyze_pdf(pdf_path)

        if "error" in result:
            print(f"\nFile: {pdf_file}")
            print(f"  ERROR: {result['error']}")
            continue

        print(f"\nFile: {pdf_file}")
        print(f"  Pages: {result['total_pages']}")
        print(f"  Pages with text: {result['pages_with_text']}")
        print(f"  Total text items: {result['total_text_items']}")
        print(f"  Avg items/page: {result['avg_items_per_page']:.1f}")
        print(f"  Classification: ", end="")

        if result['is_digital_pdf']:
            print("DIGITAL PDF (native text layer)")
            digital_pdfs.append(pdf_file)
        elif result['is_scanned_pdf']:
            print("SCANNED PDF (minimal text layer)")
            scanned_pdfs.append(pdf_file)
        else:
            print("IMAGE-ONLY PDF (no text layer)")
            image_only_pdfs.append(pdf_file)

    print("\n" + "="*80)
    print("\nSUMMARY:")
    print(f"  Digital PDFs (native text): {len(digital_pdfs)}")
    print(f"  Scanned PDFs (minimal text): {len(scanned_pdfs)}")
    print(f"  Image-only PDFs (no text): {len(image_only_pdfs)}")

    if digital_pdfs:
        print(f"\n  Examples of DIGITAL PDFs: {', '.join(digital_pdfs[:3])}")
    if scanned_pdfs or image_only_pdfs:
        print(f"  Examples of SCANNED/IMAGE PDFs: {', '.join((scanned_pdfs + image_only_pdfs)[:3])}")

    print("\nCONCLUSION:")
    if len(digital_pdfs) > len(scanned_pdfs + image_only_pdfs):
        print("  Most PDFs have NATIVE TEXT LAYERS (digital PDFs)")
        print("  → Searchable PDF export can use original file directly")
    else:
        print("  Most PDFs are SCANNED/IMAGE-ONLY")
        print("  → Searchable PDF export needs to add OCR text layer")

if __name__ == "__main__":
    main()
