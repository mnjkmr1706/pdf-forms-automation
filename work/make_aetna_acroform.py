from pathlib import Path

from pypdf import PdfReader, PdfWriter
from pypdf.generic import BooleanObject, NameObject
from reportlab.pdfgen import canvas
from reportlab.lib.colors import black


ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "AETNA_Form1.pdf"
WORK = ROOT / "work"
OUTPUT_DIR = ROOT / "outputs"
OVERLAY = WORK / "aetna_acroform_overlay.pdf"
OUTPUT = OUTPUT_DIR / "AETNA_Form1_editable_acroform_corrected.pdf"


def add_text(form, name, x, y, w, h, multiline=False, maxlen=500):
    flags = "multiline" if multiline else ""
    form.textfield(
        name=name,
        tooltip=name.replace("_", " ").title(),
        x=x,
        y=y,
        width=w,
        height=h,
        borderWidth=0,
        borderColor=None,
        fillColor=None,
        textColor=black,
        fontName="Helvetica",
        fontSize=9,
        fieldFlags=flags,
        maxlen=maxlen,
        forceBorder=False,
    )


def add_checkbox(form, name, x, y, size=9.24):
    form.checkbox(
        name=name,
        tooltip=name.replace("_", " ").title(),
        x=x,
        y=y,
        size=size,
        buttonStyle="check",
        borderWidth=0,
        borderColor=None,
        fillColor=None,
        textColor=black,
        checked=False,
        fieldFlags="",
        forceBorder=False,
    )


def build_overlay():
    reader = PdfReader(str(INPUT))
    c = canvas.Canvas(str(OVERLAY), pagesize=(612, 792))
    form = c.acroForm

    for page_index in range(len(reader.pages)):
        if page_index == 1:
            # Page 2, section A: Provider Information
            add_text(form, "provider_name", 170, 618, 250, 16)
            add_text(form, "tin_npi", 482, 618, 92, 16)
            add_text(form, "provider_group", 246, 595, 328, 16)
            add_text(form, "contact_name", 170, 571, 222, 16)
            add_text(form, "contact_title", 447, 571, 127, 16)
            add_text(form, "contact_address", 166, 536, 408, 28, multiline=True)
            add_text(form, "phone", 121, 513, 64, 16)
            add_text(form, "fax", 235, 513, 78, 16)
            add_text(form, "email", 371, 513, 203, 16)

            # Page 2, section B: Patient Information
            add_text(form, "patient_name", 165, 489, 208, 16)
            add_text(form, "insurance_id", 426, 489, 148, 16)
            add_checkbox(form, "assignment_benefits_yes", 217.25, 463.87)
            add_checkbox(form, "assignment_benefits_no", 262.73, 463.87)
            add_checkbox(form, "assignment_benefits_na", 303.65, 463.87)
            add_checkbox(form, "consent_form_yes", 488.50, 429.07)
            add_checkbox(form, "consent_form_no", 533.86, 429.07)

            # Page 2, section C: Claim Information
            add_text(form, "claim_number", 212, 416, 96, 12)
            add_text(form, "date_of_service", 404, 416, 170, 12)
            add_text(form, "authorization_number", 218, 398, 90, 12)
            add_checkbox(form, "filing_method_electronic", 88.82, 373.63)
            add_checkbox(form, "filing_method_fax", 88.82, 362.11)
            add_checkbox(form, "filing_method_paper", 88.22, 350.57)
            add_checkbox(form, "reason_no_action", 88.82, 315.65)
            add_checkbox(form, "reason_denied_claim", 88.82, 303.53)
            add_text(form, "denial_date", 335.23, 303.5, 80.64, 12)
            add_checkbox(form, "reason_not_timely", 88.22, 291.89)
            add_checkbox(form, "additional_info_requested_yes", 105.50, 280.49)
            add_checkbox(form, "additional_info_requested_no", 139.82, 280.49)
            add_text(form, "additional_info_requested_date", 406.39, 280.0, 80.78, 12)
            add_checkbox(form, "additional_info_provided_yes", 105.50, 268.97)
            add_checkbox(form, "additional_info_provided_no", 139.82, 268.97)
            add_text(form, "additional_info_provided_date", 382.51, 268.5, 80.64, 12)
            add_checkbox(form, "prompt_payment_interest_yes", 105.50, 257.21)
            add_checkbox(form, "prompt_payment_interest_no", 139.82, 257.21)
            add_checkbox(form, "reason_amount_dispute", 88.82, 245.81)
            add_checkbox(form, "reason_codes_in_dispute", 88.82, 234.29)
            for i, x in enumerate([179.18, 223.73, 268.25, 312.77, 357.19, 401.71, 446.11], start=1):
                add_text(form, f"code_in_dispute_{i}", x, 235, 41, 11, maxlen=10)
            add_checkbox(form, "reason_overpayment", 88.82, 222.77)
            add_checkbox(form, "reason_offset_amount", 88.82, 211.25)
            add_text(form, "reason_for_appeal", 72, 64, 503, 141, multiline=True, maxlen=3000)

        if page_index == 2:
            # Page 3 continuation fields
            add_text(form, "page3_provider_name", 126, 641.5, 168, 14)
            add_text(form, "page3_contact_number", 482, 641.5, 50, 14)
            add_text(form, "page3_member_name", 126, 618.5, 168, 14)
            add_text(form, "page3_dos", 432, 618.5, 50, 14)
            add_checkbox(form, "attachments_yes", 109.34, 377.35)
            add_checkbox(form, "attachments_no", 189.38, 377.35)
            add_text(form, "signature", 82, 252, 277, 14)
            add_text(form, "signature_date", 386, 252, 80, 14)

        c.showPage()

    c.save()


def merge_overlay():
    source = PdfReader(str(INPUT))
    writer = PdfWriter(clone_from=str(OVERLAY))

    for i, source_page in enumerate(source.pages):
        page = writer.pages[i]
        page.merge_page(source_page, over=True)

    if "/AcroForm" in writer._root_object:
        writer.set_need_appearances_writer(True)
        writer._root_object["/AcroForm"].update(
            {NameObject("/NeedAppearances"): BooleanObject(True)}
        )

    OUTPUT_DIR.mkdir(exist_ok=True)
    with OUTPUT.open("wb") as fh:
        writer.write(fh)


if __name__ == "__main__":
    WORK.mkdir(exist_ok=True)
    build_overlay()
    merge_overlay()
    print(OUTPUT)
