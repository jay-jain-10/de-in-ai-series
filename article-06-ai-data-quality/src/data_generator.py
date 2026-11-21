"""Synthetic healthcare claims. No real patient data, ever."""

import random
from datetime import date, timedelta
from typing import Dict, List, Set, Tuple

# (diagnosis_code, description) and the procedures that coherently go with them.
DIAGNOSES = {
    "M17.11": "knee osteoarthritis",
    "J01.90": "acute sinusitis",
    "K35.80": "acute appendicitis",
    "I10": "essential hypertension",
    "E11.9": "type 2 diabetes",
}
PROCEDURES = {
    "27447": "total knee replacement",
    "31256": "nasal/sinus endoscopy",
    "44970": "laparoscopic appendectomy",
    "99213": "office visit",
}
COHERENT_PAIRS = [
    ("M17.11", "27447"),
    ("J01.90", "31256"),
    ("K35.80", "44970"),
    ("I10", "99213"),
    ("E11.9", "99213"),
]
UNITS = ["ICU", "EMERGENCY", "OUTPATIENT", "SURGERY"]


def make_patients(n: int = 50) -> Set[str]:
    return {f"PAT-{i:05d}" for i in range(n)}


def make_claims(
    n: int = 100,
    seed: int = 42,
    semantic_error_rate: float = 0.03,
    syntactic_error_rate: float = 0.05,
    patients: Set[str] = None,
    today: date = date(2026, 6, 1),
) -> Tuple[List[Dict], Dict[str, str]]:
    """Return (claims, ground_truth) where ground_truth maps claim_id -> label.

    Labels: 'ok', 'syntactic_error', 'semantic_error'.
    Semantic errors pass every Layer 1 rule by construction.
    """
    rng = random.Random(seed)
    patients = sorted(patients or make_patients())
    claims, truth = [], {}
    for i in range(n):
        dx, px = rng.choice(COHERENT_PAIRS)
        admit = today - timedelta(days=rng.randint(10, 200))
        claim = {
            "claim_id": f"CLM-{i:06d}",
            "patient_id": rng.choice(patients),
            "diagnosis_code": dx,
            "diagnosis_description": DIAGNOSES[dx],
            "procedure_code": px,
            "procedure_description": PROCEDURES[px],
            "claim_amount": round(rng.uniform(80, 45000), 2),
            "claim_date": (admit + timedelta(days=5)).isoformat(),
            "admit_date": admit.isoformat(),
            "discharge_date": (admit + timedelta(days=rng.randint(0, 4))).isoformat(),
            "medication": "lisinopril" if dx == "I10" else None,
            "dosage_mg": rng.choice([10, 20, 40]) if dx == "I10" else None,
            "patient_age": rng.randint(30, 85),
            "care_unit": rng.choice(UNITS),
            "source_system": "healthcare_claims",
        }
        label = "ok"
        r = rng.random()
        if r < syntactic_error_rate:
            label = "syntactic_error"
            kind = rng.choice(["null_patient", "bad_amount", "bad_dx", "orphan_patient"])
            if kind == "null_patient":
                claim["patient_id"] = None
            elif kind == "bad_amount":
                claim["claim_amount"] = -50.0
            elif kind == "bad_dx":
                claim["diagnosis_code"] = "NOT-A-CODE"
            else:
                claim["patient_id"] = "PAT-99999"
        elif r < syntactic_error_rate + semantic_error_rate:
            label = "semantic_error"
            kind = rng.choice(["mismatch", "dosage", "temporal"])
            if kind == "mismatch":
                claim["diagnosis_code"], claim["diagnosis_description"] = "J01.90", DIAGNOSES["J01.90"]
                claim["procedure_code"], claim["procedure_description"] = "27447", PROCEDURES["27447"]
            elif kind == "dosage":
                claim["medication"], claim["dosage_mg"] = "lisinopril", 4000
            else:
                claim["discharge_date"] = (admit - timedelta(days=6)).isoformat()
        claims.append(claim)
        truth[claim["claim_id"]] = label
    return claims, truth
