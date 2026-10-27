import os
import fitz  # PyMuPDF


DPI = 300  # Target DPI for PDF → image conversion
MAX_RASTERIZE_PAGES = int(os.environ.get("MAX_RASTERIZE_PAGES", "300"))


class PdfRasterizeError(Exception):
    """Raised when a PDF can't be opened/rasterized - corrupt, password-
    protected, or truncated. Callers should catch this and report a clean
    per-file error instead of letting a raw PyMuPDF exception propagate out
    of an upload-processing worker thread."""


def pdf_to_images(pdf_path: str, output_folder: str, dpi: int = DPI) -> list[str]:
    """
    Convert each page of a PDF to a PNG image using PyMuPDF.
    PyMuPDF base resolution is 72 DPI, so zoom = dpi / 72.
      dpi=144 → zoom 2.0 (low quality)
      dpi=200 → zoom 2.78
      dpi=300 → zoom 4.17 (default, good OCR quality)
      dpi=400 → zoom 5.56 (high quality, slower)
    """
    os.makedirs(output_folder, exist_ok=True)

    try:
        pdf = fitz.open(pdf_path)
    except Exception as e:
        raise PdfRasterizeError(f"Could not open '{os.path.basename(pdf_path)}': {e}") from e

    try:
        if pdf.is_encrypted:
            raise PdfRasterizeError(f"'{os.path.basename(pdf_path)}' is password-protected")
        if len(pdf) > MAX_RASTERIZE_PAGES:
            raise PdfRasterizeError(
                f"'{os.path.basename(pdf_path)}' has {len(pdf)} pages, exceeding the "
                f"{MAX_RASTERIZE_PAGES}-page rasterization limit"
            )

        image_paths = []
        for i, page in enumerate(pdf):
            try:
                zoom = dpi / 72.0
                mat = fitz.Matrix(zoom, zoom)  # dpi / 72 = zoom factor
                pix = page.get_pixmap(matrix=mat)
                image_path = os.path.join(output_folder, f'page_{i + 1}.png')
                pix.save(image_path)
                image_paths.append(image_path)
            except Exception as e:
                raise PdfRasterizeError(f"Failed to render page {i + 1} of '{os.path.basename(pdf_path)}': {e}") from e
        return image_paths
    finally:
        pdf.close()