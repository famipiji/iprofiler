"""Windows re-check for the PaddleOCR migration (Task 1).
Run:  python paddle_smoke_test.py            (picks a few images from uploads/)
      python paddle_smoke_test.py path\\to\\image.jpg
Paste the output back to Claude."""
import glob
import os
import platform
import sys
import time

print(f"Python {sys.version.split()[0]} on {platform.system()} {platform.release()}")

# torch first: on Windows both wheels ship libiomp5md.dll and paddle's older copy
# breaks a later torch import (WinError 127, torch\\lib\\shm.dll).
import torch  # noqa: F401
import paddle
print(f"paddlepaddle {paddle.__version__}")
paddle.utils.run_check()

import paddleocr
from paddleocr import PaddleOCR
print(f"paddleocr {paddleocr.__version__}")

files = sys.argv[1:] or sorted(
    glob.glob("uploads/*.jpeg") + glob.glob("uploads/*.jpg") + glob.glob("uploads/*.png")
)[:3]
if not files:
    sys.exit("No images found - pass an image path as an argument.")

for model in ("mobile", "server"):
    kwargs = dict(lang="en", use_doc_orientation_classify=False,
                  use_doc_unwarping=False, use_textline_orientation=False)
    if model == "mobile":
        kwargs.update(text_detection_model_name="PP-OCRv5_mobile_det",
                      text_recognition_model_name="en_PP-OCRv5_mobile_rec")
    t = time.time()
    ocr = PaddleOCR(**kwargs)
    print(f"\n=== {model} models (load {time.time() - t:.1f}s) ===")
    ocr.predict(files[0])  # warm-up, not timed
    for f in files:
        t = time.time()
        r = ocr.predict(f)[0]
        dt = time.time() - t
        texts = r["rec_texts"]
        print(f"{os.path.basename(f)}: {dt:.1f}s, {len(texts)} lines -> {texts[:4]}")
