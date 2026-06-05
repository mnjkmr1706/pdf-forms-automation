# JSON-to-Payer-PDF Filling Engine

This is the product path for filling payer forms from the stable database JSON schema.
It does not require converting PDFs into editable AcroForms.

## Create a Mapping

```powershell
& 'C:\Users\mnjkm\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' 'outputs\create_mapping.py' --pdf 'local_inputs\payer_form.pdf' --schema 'local_inputs\claim_payload.json' --out 'local_outputs\payer_form_template_mapping.json' --template-id 'payer_form_v1' --llm
```

Without `--llm`, the command creates a deterministic heuristic draft.

## Fill a PDF

```powershell
& 'C:\Users\mnjkm\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' 'outputs\fill_pdf.py' --pdf 'local_inputs\payer_form.pdf' --mapping 'local_outputs\payer_form_template_mapping.json' --data 'local_inputs\claim_payload.json' --out 'local_outputs\payer_form_filled.pdf'
```

The fill command is deterministic and does not call GPT. It verifies the PDF fingerprint against the saved mapping unless `--allow-drift` is passed.

PDF and JSON files are runtime inputs/outputs and may contain PHI. They are intentionally ignored by git.

Mapping artifacts and fill diagnostics are validated with Pydantic models before they are written, loaded, or returned.

## Current Notes

- Runtime dependencies include `pypdf`, `reportlab`, and `pydantic`.
- PyMuPDF is supported as an optional primary extraction layer when installed; this runtime currently falls back to `pypdf`.
- GPT is used only for mapping creation with `--llm`.
- Generated mappings are drafts and should be reviewed before production use.
- Credentials are read only from environment variables or local `.env`; they are not written to outputs.
