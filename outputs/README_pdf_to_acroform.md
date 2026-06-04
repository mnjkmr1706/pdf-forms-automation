# PDF to Editable AcroForm Converter

This folder contains a reusable Python script:

```powershell
python pdf_to_acroform.py input.pdf output.pdf
```

For this workspace, use the bundled Python:

```powershell
& 'C:\Users\mnjkm\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' 'outputs\pdf_to_acroform.py' 'input.pdf' 'output.pdf'
```

To also save detected field coordinates for review:

```powershell
& 'C:\Users\mnjkm\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' 'outputs\pdf_to_acroform.py' 'input.pdf' 'output.pdf' --debug-json 'fields.json'
```

## What it does

- Preserves the original PDF page content.
- Adds editable AcroForm text fields over likely blank table cells and underlined blanks.
- Adds checkbox fields over printed square boxes.
- Sets `/NeedAppearances` so PDF viewers can render field values.

## Notes

This is a heuristic converter for printed/non-editable PDF forms. It works best when the form was generated from vector content with drawn lines, boxes, and selectable text. Scanned image-only PDFs need OCR or a separate image-detection pass before this kind of conversion can be accurate.
