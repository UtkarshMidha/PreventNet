"""Split the public Pima Indians Diabetes dataset vertically across the three sites.

Source: National Institute of Diabetes and Digestive and Kidney Diseases, via the
UCI ML repository (768 rows, no identifiers). Each site receives ONLY the columns
its function would plausibly hold; the diabetes outcome label stays at the GP.
Pima has no medication data, so the pharmacy's feature is synthetic (seeded,
derived from blood pressure) and labelled as such.

Writes preventnet/data/{role}/cohort.jsonl next to that department's review
records (preventnet/data/{role}/records.jsonl). One folder per department, used by
both the SuperGrid AgentApp and the local deployments.

Usage: uv run python scripts/prepare_data.py
"""

import csv
import json
import random
from pathlib import Path

ROOT = Path(__file__).parent
SOURCE = ROOT / "source_data" / "pima-indians-diabetes.csv"
SITES = ROOT.parent / "preventnet" / "data"
SEED = 7
TEST_FRACTION = 0.2

PIMA_COLUMNS = [
    "pregnancies", "glucose", "blood_pressure", "skin_thickness",
    "insulin", "bmi", "diabetes_pedigree", "age", "outcome",
]
SITE_COLUMNS = {
    "gp": ["pregnancies", "blood_pressure", "skin_thickness", "bmi", "diabetes_pedigree", "age", "outcome"],
    "lab": ["glucose", "insulin"],
    "pharmacy": ["antihypertensive_dispensed"],  # synthetic
}

# Feature rows for the patients under review (never used for training).
REVIEW_PATIENTS = {
    "P001": {"pregnancies": 0, "glucose": 118, "blood_pressure": 88, "skin_thickness": 30, "insulin": 95,
             "bmi": 31.2, "diabetes_pedigree": 0.62, "age": 58, "antihypertensive_dispensed": 0},
    "P002": {"pregnancies": 2, "glucose": 124, "blood_pressure": 76, "skin_thickness": 34, "insulin": 140,
             "bmi": 33.5, "diabetes_pedigree": 0.81, "age": 45, "antihypertensive_dispensed": 0},
    "P003": {"pregnancies": 0, "glucose": 90, "blood_pressure": 70, "skin_thickness": 20, "insulin": 60,
             "bmi": 23.0, "diabetes_pedigree": 0.20, "age": 28, "antihypertensive_dispensed": 0},
}


def _num(v):
    f = float(v)
    return int(f) if f.is_integer() else f


def main() -> None:
    rng = random.Random(SEED)
    with SOURCE.open() as f:
        rows = [dict(zip(PIMA_COLUMNS, line)) for line in csv.reader(f) if line]

    order = list(range(len(rows)))
    rng.shuffle(order)
    n_test = int(len(rows) * TEST_FRACTION)
    split = {idx: ("test" if pos < n_test else "train") for pos, idx in enumerate(order)}

    for i, row in enumerate(rows):
        # Synthetic pharmacy feature: people with raised BP are more likely to be
        # dispensed an antihypertensive. Derived here, then held only by the pharmacy.
        p = 0.7 if float(row["blood_pressure"]) >= 85 else 0.1
        row["antihypertensive_dispensed"] = int(rng.random() < p)
        row["entity_id"] = f"C{i + 1:04d}"
        row["split"] = split[i]

    for role, cols in SITE_COLUMNS.items():
        site_dir = SITES / role
        site_dir.mkdir(parents=True, exist_ok=True)
        # Compact JSONL (a header line, then one array per row): Flower bundles only
        # .py/.toml/.md/.yaml/.json/.jsonl files, and smaller files upload faster.
        with (site_dir / "cohort.jsonl").open("w") as f:
            f.write(json.dumps({"columns": ["entity_id", "split", *cols]}, separators=(",", ":")) + "\n")
            for row in rows:
                f.write(json.dumps([row["entity_id"], row["split"], *(_num(row[c]) for c in cols)],
                                   separators=(",", ":")) + "\n")
            for pid, feats in REVIEW_PATIENTS.items():
                f.write(json.dumps([pid, "review", *(_num(feats[c]) if c in feats else None for c in cols)],
                                   separators=(",", ":")) + "\n")
        print(f"{role}: {len(rows)} cohort rows + {len(REVIEW_PATIENTS)} review rows -> {site_dir}")


if __name__ == "__main__":
    main()
