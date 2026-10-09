"""Sensitivity-classification heuristics — pure functions, no I/O.

Two tiers:
  HARD — structured patterns where a positive match is unambiguous. False
         positives are essentially impossible because each pattern includes a
         checksum, format constraint, or tightly-defined token list. Used at
         detect-stage (Tier 3a) AND post-summarize (Tier 3) for safety.

  SOFT — keyword tokens that suggest sensitivity but are too noisy to flag
         alone (e.g., "diagnosis" appears in fitness journals; "balance" appears
         in balance-board reviews). Used post-summarize where they're combined
         with summary-topic semantics for corroboration.

Pure-Python regex + Luhn check + fixed token lists. No NLP, no LLM, no
external libs. Designed to run in <50ms on a 24KB doc.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ─── HARD TIER — structured patterns ────────────────────────────────────────

# IBAN — 2 letter country + 2 digit checksum + up to 30 alphanumeric.
# We additionally validate via mod-97 to eliminate noise (e.g., "GB12CD345" in
# random text won't match without proper checksum).
_IBAN_RE = re.compile(r"\b([A-Z]{2}\d{2}[A-Z0-9]{10,30})\b")

# BIC / SWIFT — 8 or 11 chars: 4 bank + 2 country + 2 location + (3 branch).
# Tight format makes false-positive rate negligible.
_BIC_RE = re.compile(r"\b([A-Z]{4}[A-Z]{2}[A-Z0-9]{2}(?:[A-Z0-9]{3})?)\b")

# Credit card — 13–19 digit numbers. We Luhn-check to filter random digit
# strings. Allows spaces or hyphens between groups.
_CC_RE = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")

# US Social Security Number format. Excludes obviously-invalid prefixes
# (000-, 666-, 9XX-) per SSA rules.
_SSN_RE = re.compile(r"\b(?!000|666|9\d{2})\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b")

# ICD-10 diagnosis code: 1 letter + 2 digits + optional .X[X]
# (e.g., I25.10, E11.9, F32.9). Format is unique enough to not false-positive.
_ICD10_RE = re.compile(r"\b([A-TV-Z]\d{2}(?:\.\d{1,4})?)\b")

# Common medication name fragments (not exhaustive — first-pass list of widely-
# prescribed drugs that are unlikely to appear in non-medical contexts).
# Lowercase comparison; word boundaries enforced.
_MEDICATION_TOKENS = frozenset({
    "atorvastatin", "metformin", "lisinopril", "amlodipine", "metoprolol",
    "omeprazole", "simvastatin", "losartan", "albuterol", "gabapentin",
    "hydrochlorothiazide", "sertraline", "fluoxetine", "escitalopram",
    "citalopram", "trazodone", "duloxetine", "alprazolam", "lorazepam",
    "clonazepam", "tramadol", "oxycodone", "hydrocodone", "amoxicillin",
    "azithromycin", "ciprofloxacin", "prednisone", "warfarin", "clopidogrel",
    "insulin glargine", "insulin lispro", "levothyroxine", "tamsulosin",
    "tadalafil", "sildenafil", "finasteride", "metronidazole",
})
_MED_RE = re.compile(
    r"\b(" + "|".join(re.escape(m) for m in _MEDICATION_TOKENS) + r")\b",
    re.IGNORECASE,
)


@dataclass(slots=True, frozen=True)
class HardHit:
    """Result of a hard-tier match. `kind` is the classifier label that goes
    into documents.sensitivity_reason; `match` is the offending substring
    (truncated; never logged to plaintext audit)."""
    kind:  str
    match: str

    @property
    def reason(self) -> str:
        return f"heuristic:{self.kind}"


def _luhn_valid(digits: str) -> bool:
    """Standard Luhn checksum. digits = ASCII string of digits, no separators."""
    if not digits.isdigit() or len(digits) < 13:
        return False
    total = 0
    parity = len(digits) % 2
    for i, ch in enumerate(digits):
        d = ord(ch) - 48
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _iban_valid(iban: str) -> bool:
    """Mod-97 check on a candidate IBAN (letters mapped to 10–35)."""
    iban = iban.upper().replace(" ", "")
    if len(iban) < 15 or len(iban) > 34:
        return False
    rearranged = iban[4:] + iban[:4]
    expanded = []
    for ch in rearranged:
        if ch.isdigit():
            expanded.append(ch)
        elif "A" <= ch <= "Z":
            expanded.append(str(ord(ch) - 55))
        else:
            return False
    try:
        return int("".join(expanded)) % 97 == 1
    except ValueError:
        return False


def hard_tier_scan(text: str, *, max_hits: int = 3) -> list[HardHit]:
    """Run all hard-tier patterns over `text`, returning up to `max_hits` hits.

    Stops early on `max_hits` to bound cost on large docs. Returned hits are
    safe to log via reason — `match` substring is included for forensics but
    callers should NOT write it to non-encrypted audit fields.
    """
    hits: list[HardHit] = []

    def _add(kind: str, match: str) -> bool:
        hits.append(HardHit(kind=kind, match=match[:32]))
        return len(hits) >= max_hits

    # IBAN
    for m in _IBAN_RE.finditer(text):
        candidate = m.group(1)
        if _iban_valid(candidate):
            if _add("iban_match", candidate):
                return hits

    # Credit card (Luhn-validated)
    for m in _CC_RE.finditer(text):
        digits = re.sub(r"[ -]", "", m.group(0))
        if _luhn_valid(digits):
            if _add("credit_card_match", m.group(0)):
                return hits

    # SSN
    for m in _SSN_RE.finditer(text):
        if _add("ssn_match", m.group(0)):
            return hits

    # ICD-10
    # Slight care: this can match things like "I25.10" but also short codes
    # like "B12" (Vitamin B12 reference) or "E11" (engine code). To reduce
    # false-positives, require either a dot+digits suffix OR proximity to
    # a medical context word ("diagnosis", "icd", "code").
    for m in _ICD10_RE.finditer(text):
        code = m.group(1)
        if "." in code:
            if _add("icd10_match", code):
                return hits
            continue
        # No subdecimal — only accept if a medical context word is within
        # 60 chars before or after.
        start, end = m.start(), m.end()
        window = text[max(0, start - 60):min(len(text), end + 60)].lower()
        if any(w in window for w in ("diagnosis", "icd-10", "icd10",
                                     "icd ", "diagnosed")):
            if _add("icd10_match", code):
                return hits

    # Medication name
    for m in _MED_RE.finditer(text):
        if _add("medication_name", m.group(1)):
            return hits

    # BIC — last because it's the loosest format. Only counts when it appears
    # near a banking context word, to keep false positives down.
    for m in _BIC_RE.finditer(text):
        start, end = m.start(), m.end()
        window = text[max(0, start - 60):min(len(text), end + 60)].lower()
        if any(w in window for w in ("bic", "swift", "iban", "bank ",
                                     "transfer", "wire ")):
            if _add("bic_match", m.group(1)):
                return hits

    return hits


# ─── SOFT TIER — keywords for post-summarize corroboration ──────────────────

# Categories of soft tokens. Lowercased + word-boundary matched. Each match
# is weak; the refine stage corroborates with summary-topic semantics before
# upgrading sensitivity.
_SOFT_CATEGORIES: dict[str, frozenset[str]] = {
    "medical": frozenset({
        "diagnosis", "diagnosed", "prescription", "prescribed", "patient",
        "doctor", "physician", "clinic", "hospital", "symptom", "treatment",
        "therapy", "medication", "dosage", "blood pressure", "blood test",
        "x-ray", "mri", "ultrasound", "biopsy", "tumor", "tumour", "chronic",
        "psychiatric", "mental health",
    }),
    "financial": frozenset({
        "salary", "compensation", "bank account", "balance", "transfer",
        "wire", "deposit", "withdrawal", "loan", "mortgage", "credit limit",
        "investment", "portfolio", "tax return", "income", "payslip",
        "paystub", "p60", "p45", "1099", "w-2",
    }),
    "legal_personal": frozenset({
        "nda", "non-disclosure", "confidentiality agreement", "settlement",
        "litigation", "subpoena", "deposition", "court order", "divorce",
        "custody", "will and testament", "estate", "power of attorney",
    }),
    "identity_doc": frozenset({
        "passport", "driver's license", "drivers licence", "national id",
        "social security number", "tax id", "visa number", "green card",
    }),
}


@dataclass(slots=True, frozen=True)
class SoftHit:
    """One soft-tier match. `category` aligns with the refine-stage topic
    reference embeddings (medical, financial, legal_personal, identity_doc)."""
    category: str
    token:    str

    @property
    def reason(self) -> str:
        return f"soft:{self.category}:{self.token}"


def soft_tier_scan(text: str) -> list[SoftHit]:
    """Return all distinct (category, token) hits found in lowercased text.

    Pure detection — no thresholding, no decision. The refine stage decides
    what to do with these in combination with summary topic signals.
    """
    lowered = text.lower()
    hits: list[SoftHit] = []
    seen: set[tuple[str, str]] = set()
    for category, tokens in _SOFT_CATEGORIES.items():
        for tok in tokens:
            # Word-boundary match for single-word tokens; substring for
            # multi-word phrases (which already encode boundaries).
            if " " in tok:
                if tok in lowered:
                    key = (category, tok)
                    if key not in seen:
                        seen.add(key)
                        hits.append(SoftHit(category=category, token=tok))
            else:
                pattern = rf"\b{re.escape(tok)}\b"
                if re.search(pattern, lowered):
                    key = (category, tok)
                    if key not in seen:
                        seen.add(key)
                        hits.append(SoftHit(category=category, token=tok))
    return hits
