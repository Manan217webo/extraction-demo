"""Per-page CRF field schema for Visit 4 extraction."""

YES_NO = ["Yes", "No"]
NORMAL_ABNORMAL = ["Normal", "Abnormal"]
CS_NCS = ["CS", "NCS"]
TEMP_UNIT = ["C", "F"]
PREGNANCY_DONE = ["Yes", "No", "Not Applicable"]
PREGNANCY_RESULT = ["Positive", "Negative"]
PREGNANCY_REASON = [
    "Surgically Sterile",
    "Ongoing Menstruation",
    "Post-menopausal",
    "Male",
    "Other",
]
PGIC_SCORE = ["1", "2", "3", "4", "5", "6", "7"]
COMPLIANT_STATUS = ["Compliant", "Non-Compliant"]


def _field(key, label, type_, options=None, section=None):
    f = {"key": key, "label": label, "type": type_, "section": section}
    if options is not None:
        f["options"] = options
    return f


def _vital(name, label, section="Vital Signs"):
    return [
        _field(f"{name}_value", f"{label} — Value", "number", section=section),
        _field(
            f"{name}_evaluation",
            f"{label} — Evaluation",
            "enum",
            NORMAL_ABNORMAL,
            section=section,
        ),
        _field(
            f"{name}_significance",
            f"{label} — Significance",
            "enum",
            CS_NCS,
            section=section,
        ),
    ]


def _exam(name, label, section="Physical Examination"):
    return [
        _field(
            f"{name}_condition",
            f"{label} — Condition",
            "enum",
            NORMAL_ABNORMAL,
            section=section,
        ),
        _field(
            f"{name}_significance",
            f"{label} — Significance",
            "enum",
            CS_NCS,
            section=section,
        ),
        _field(
            f"{name}_description",
            f"{label} — Description",
            "text",
            section=section,
        ),
    ]


PAGE_SCHEMAS = {
    1: {
        "page_no": 1,
        "title": "Visit 4 — Page 1",
        "fields": [
            # Header
            _field("protocol_no", "Protocol No.", "text", section="Header"),
            _field("screening_no", "Screening No.", "digits", section="Header"),
            _field("patient_initials", "Patient Initials", "text", section="Header"),
            _field("visit_date", "Visit Date", "date", section="Header"),
            # Vital Signs
            _field(
                "vitals_performed",
                "Vital Signs Performed",
                "yes_no",
                YES_NO,
                section="Vital Signs",
            ),
            _field("vitals_date", "Vital Signs Date", "date", section="Vital Signs"),
            *_vital("pulse_rate", "Pulse Rate"),
            *_vital("respiratory_rate", "Respiratory Rate"),
            *_vital("systolic_bp", "Systolic BP"),
            *_vital("diastolic_bp", "Diastolic BP"),
            *_vital("body_temperature", "Body Temperature"),
            _field(
                "body_temperature_unit",
                "Body Temperature Unit",
                "enum",
                TEMP_UNIT,
                section="Vital Signs",
            ),
            # Physical Examination
            _field(
                "physical_exam_performed",
                "Physical Exam Performed",
                "yes_no",
                YES_NO,
                section="Physical Examination",
            ),
            _field(
                "physical_exam_date",
                "Physical Exam Date",
                "date",
                section="Physical Examination",
            ),
            *_exam("general_appearance", "General Appearance"),
            *_exam("heent", "HEENT"),
            *_exam("dermatological", "Dermatological"),
            *_exam("neurological", "Neurological"),
            *_exam("respiratory", "Respiratory"),
            *_exam("cardiovascular", "Cardiovascular"),
            *_exam("gastrointestinal", "Gastrointestinal"),
            *_exam("musculoskeletal", "Musculoskeletal"),
            *_exam("genitourinary", "Genitourinary"),
            *_exam("lymph_nodes", "Lymph Nodes"),
            *_exam("other", "Other"),
            # Cough Severity VAS
            _field(
                "vas_performed",
                "VAS Performed",
                "yes_no",
                YES_NO,
                section="Cough Severity VAS",
            ),
            _field("vas_date", "VAS Date", "date", section="Cough Severity VAS"),
            _field("vas_score", "VAS Score (0–100)", "number", section="Cough Severity VAS"),
        ],
    },
    2: {
        "page_no": 2,
        "title": "Visit 4 — Page 2",
        "fields": [
            _field("screening_no", "Screening No.", "digits", section="Header"),
            _field("patient_initials", "Patient Initials", "text", section="Header"),
            _field("visit_date", "Visit Date", "date", section="Header"),
            # PGIC
            _field(
                "pgic_performed",
                "PGIC Performed",
                "yes_no",
                YES_NO,
                section="PGIC",
            ),
            _field("pgic_date", "PGIC Date", "date", section="PGIC"),
            _field("pgic_score", "PGIC Score (1–7)", "enum", PGIC_SCORE, section="PGIC"),
            # Pregnancy Test
            _field(
                "pregnancy_test_done",
                "Pregnancy Test Done",
                "enum",
                PREGNANCY_DONE,
                section="Pregnancy Test",
            ),
            _field(
                "pregnancy_test_date",
                "Pregnancy Test Date",
                "date",
                section="Pregnancy Test",
            ),
            _field(
                "pregnancy_result",
                "Pregnancy Result",
                "enum",
                PREGNANCY_RESULT,
                section="Pregnancy Test",
            ),
            _field(
                "pregnancy_reason",
                "Pregnancy N/A Reason",
                "enum",
                PREGNANCY_REASON,
                section="Pregnancy Test",
            ),
            # Patient Diary
            _field(
                "diary_dispensed",
                "Diary Dispensed",
                "yes_no",
                YES_NO,
                section="Patient Diary",
            ),
            _field(
                "diary_reviewed",
                "Diary Reviewed",
                "yes_no",
                YES_NO,
                section="Patient Diary",
            ),
            _field(
                "avg_daily_cough_frequency",
                "Avg Daily Cough Frequency",
                "number",
                section="Patient Diary",
            ),
            _field(
                "adverse_event_since_last_visit",
                "Adverse Event Since Last Visit",
                "yes_no",
                YES_NO,
                section="Patient Diary",
            ),
            _field(
                "medication_since_last_visit",
                "Medication Since Last Visit",
                "yes_no",
                YES_NO,
                section="Patient Diary",
            ),
        ],
    },
    3: {
        "page_no": 3,
        "title": "Visit 4 — Page 3",
        "fields": [
            _field("screening_no", "Screening No.", "digits", section="Header"),
            _field("patient_initials", "Patient Initials", "text", section="Header"),
            _field("visit_date", "Visit Date", "date", section="Header"),
            # Study Medication
            _field(
                "study_med_dispensed",
                "Study Med Dispensed",
                "yes_no",
                YES_NO,
                section="Study Medication",
            ),
            _field(
                "tablets_dispensed",
                "Tablets Dispensed",
                "number",
                section="Study Medication",
            ),
            _field(
                "tablets_consumed",
                "Tablets Consumed",
                "number",
                section="Study Medication",
            ),
            _field(
                "tablets_returned",
                "Tablets Returned",
                "number",
                section="Study Medication",
            ),
            _field(
                "tablets_lost",
                "Tablets Lost",
                "number",
                section="Study Medication",
            ),
            _field(
                "compliance_percent",
                "Compliance %",
                "number",
                section="Study Medication",
            ),
            _field(
                "treatment_compliant_status",
                "Treatment Compliant Status",
                "enum",
                COMPLIANT_STATUS,
                section="Study Medication",
            ),
            _field("comments", "Comments", "text", section="Study Medication"),
            _field(
                "next_visit_date",
                "Next Visit Date",
                "date",
                section="Study Medication",
            ),
            # Signatures
            _field(
                "completed_by_signed",
                "Completed By Signed",
                "bool",
                section="Signatures",
            ),
            _field(
                "completed_by_date",
                "Completed By Date",
                "date",
                section="Signatures",
            ),
            _field("pi_signed", "PI Signed", "bool", section="Signatures"),
            _field("pi_date", "PI Date", "date", section="Signatures"),
        ],
    },
}


def field_keys_for_page(page_no):
    return [f["key"] for f in PAGE_SCHEMAS[page_no]["fields"]]


def json_schema_for_page(page_no):
    """JSON Schema object for Gemini structured output for one page."""
    properties = {}
    for f in PAGE_SCHEMAS[page_no]["fields"]:
        properties[f["key"]] = {
            "type": "object",
            "properties": {
                "value": {
                    "description": (
                        f"Extracted value for {f['label']}. "
                        "Use null if blank, UNCLEAR if illegible, MULTIPLE if ambiguous checkbox."
                    ),
                },
                "confidence": {
                    "type": "string",
                    "enum": ["high", "medium", "low"],
                },
            },
            "required": ["value", "confidence"],
        }
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties.keys()),
    }


SYSTEM_PROMPT = """You are transcribing a scanned clinical trial Case Report Form. Accuracy matters more
than completeness — this is regulated medical data.

Rules:
- Transcribe ONLY what is visibly written. Never infer, complete, or guess a value.
- If a character is ambiguous, illegible, or you are unsure, return "UNCLEAR" as the value
  and set confidence to "low". Returning UNCLEAR is always better than guessing.
- Comb boxes contain exactly one character per cell. Read each cell independently.
- For checkboxes, return the option whose box contains a tick or cross. Ticks may overflow
  their box — attribute the tick to the box it originates from. If no box is marked,
  return null. If more than one is marked, return "MULTIPLE" with confidence "low".
- The scan may show faint text bleeding through from the reverse side of the page.
  Ignore anything that appears mirrored, unusually faint, or misaligned with the form grid.
- Dates are formatted DD MMM YY. Return them as ISO YYYY-MM-DD. If the day, month, or year
  cannot be read confidently, return "UNCLEAR" for the whole date.
- Return only valid JSON matching the provided schema. No prose, no markdown fences."""
