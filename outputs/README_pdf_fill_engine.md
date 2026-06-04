# JSON-to-Payer-PDF Filling Engine

This is the product path for filling payer forms from the stable database JSON schema.
It does not require converting PDFs into editable AcroForms.

## Create a Mapping

```powershell
& 'C:\Users\mnjkm\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' 'outputs\create_mapping.py' --pdf 'ANTHEM_CLAIMS_NV_00001.pdf' --schema 'database_schema.json' --out 'outputs\ANTHEM_CLAIMS_NV_00001_template_mapping.json' --template-id 'anthem_claims_nv_v1' --llm
```

Without `--llm`, the command creates a deterministic heuristic draft.

## Fill a PDF

```powershell
& 'C:\Users\mnjkm\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' 'outputs\fill_pdf.py' --pdf 'ANTHEM_CLAIMS_NV_00001.pdf' --mapping 'outputs\ANTHEM_CLAIMS_NV_00001_template_mapping_llm_v2.json' --data 'database_schema.json' --out 'outputs\ANTHEM_CLAIMS_NV_00001_filled_from_json.pdf'
```

The fill command is deterministic and does not call GPT. It verifies the PDF fingerprint against the saved mapping unless `--allow-drift` is passed.

## Current Notes

- PyMuPDF is supported as an optional primary extraction layer when installed; this runtime currently falls back to `pypdf`.
- GPT is used only for mapping creation with `--llm`.
- Generated mappings are drafts and should be reviewed before production use.
- Credentials are read only from environment variables or local `.env`; they are not written to outputs.
