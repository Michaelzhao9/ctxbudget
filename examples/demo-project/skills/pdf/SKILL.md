---
name: pdf
description: Read, split, merge and fill PDF files when the user mentions a PDF, a form, or a scanned document.
license: MIT
---

# PDF workflow

1. Inspect the page count and whether a text layer exists.
2. Use pymupdf for text extraction, OCR as a fallback.
3. Never overwrite the input file; write to a new path.
