"""Vertical federated logistic regression (diabetes risk) across the three departments.

Used in two places with the same code:
  - inside the SuperGrid AgentApp (in-process LocalSite handles, one per department)
  - by the local deployments (the dashboard wraps each site server in an HTTP handle)

Each site holds different columns for the same (pre-aligned) entities. Per round:
  1. every site returns its partial score X_k w_k plus a pairwise additive mask;
     masks cancel in the coordinator's sum, so the coordinator learns only z = sum_k X_k w_k
  2. the label holder (GP) turns z into residuals, updates its own weights, and
     pushes residuals straight to the other sites (never via the coordinator)
  3. each site updates its own weights. Weights and features never leave a site.

ponytail: residuals reveal label signal to the non-label sites and the coordinator
learns per-sample z; production VFL would add homomorphic encryption / DP noise here.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from pathlib import Path

import numpy as np

ROLE_ORDER = ["gp", "lab", "pharmacy"]
LABEL_COLUMN = "outcome"
ZERO_MEANS_MISSING = {"glucose", "blood_pressure", "skin_thickness", "insulin", "bmi"}
MASK_SCALE = 100.0
DEFAULT_ROUNDS = 30
DEFAULT_LR = 0.5
EVAL_EVERY = 5
# ponytail: fixed risk-band cut points; calibrate on a clinical validation set before real use.
RISK_BANDS = ((0.15, "low"), (0.40, "moderate"), (1.01, "high"))

COLUMN_LABELS = {
    "pregnancies": "Pregnancies", "blood_pressure": "Blood pressure", "skin_thickness": "Skinfold thickness",
    "bmi": "Body mass index", "diabetes_pedigree": "Family history score", "age": "Age",
    "outcome": "Developed diabetes", "glucose": "Glucose", "insulin": "Insulin",
    "antihypertensive_dispensed": "Blood-pressure medicine",
}

COLUMN_INFO = {
    "pregnancies": "Number of times pregnant",
    "blood_pressure": "Diastolic blood pressure (mm Hg)",
    "skin_thickness": "Triceps skinfold thickness (mm)",
    "bmi": "Body mass index (kg/m²)",
    "diabetes_pedigree": "Diabetes pedigree function (family-history score)",
    "age": "Age (years)",
    "outcome": "Diabetes diagnosed within 5 years (1 = yes)",
    "glucose": "Plasma glucose 2 h into an oral glucose tolerance test (mg/dL)",
    "insulin": "2-hour serum insulin (μU/mL)",
    "antihypertensive_dispensed": "SYNTHETIC: blood-pressure medicine dispensed (1 = yes)",
}


def _pair_mask(secret: str, a: int, b: int, nonce: str, n: int) -> np.ndarray:
    seed = int.from_bytes(hashlib.sha256(f"{secret}|{a}|{b}|{nonce}".encode()).digest()[:8], "big")
    return np.random.default_rng(seed).normal(0.0, MASK_SCALE, n)


def sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


def auc(y: np.ndarray, p: np.ndarray) -> float:
    ranks = np.empty(len(p))
    ranks[np.argsort(p)] = np.arange(1, len(p) + 1)
    n1 = int(y.sum())
    n0 = len(y) - n1
    return float((ranks[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


class SiteModel:
    def __init__(self, role: str, cohort_path: Path, secret: str):
        if role not in ROLE_ORDER:
            raise ValueError(f"unknown role {role}")
        if not secret:
            raise ValueError("a shared mask secret is required")
        self.role = role
        self._secret = secret
        with cohort_path.open() as f:
            columns = json.loads(f.readline())["columns"]
            rows = sorted((dict(zip(columns, json.loads(line))) for line in f if line.strip()),
                          key=lambda r: r["entity_id"])

        self.feature_names = [c for c in rows[0] if c not in ("entity_id", "split", LABEL_COLUMN)]
        self.is_label_holder = LABEL_COLUMN in rows[0]
        self.ids = np.array([r["entity_id"] for r in rows])
        self.split = np.array([r["split"] for r in rows])
        X = np.array([[float(r[c]) for c in self.feature_names] for r in rows])
        train = self.split == "train"
        cohort = self.split != "review"
        self.y = np.array([float(r[LABEL_COLUMN] or 0) for r in rows]) if self.is_label_holder else None
        self._summary = self._describe(X[cohort], self.y[cohort] if self.is_label_holder else None)

        for j, name in enumerate(self.feature_names):
            if name in ZERO_MEANS_MISSING:
                col = X[:, j]
                col[col == 0] = np.median(col[train & (col != 0)])
        self._mean = X[train].mean(axis=0)
        self._std = X[train].std(axis=0) + 1e-9
        self.X = (X - self._mean) / self._std

        self.used_nonces: set[str] = set()
        self.reset()

    def _describe(self, X: np.ndarray, y) -> dict:
        """Aggregate-only description of this site's cohort (federated analytics: no rows leave)."""
        columns = []
        for j, name in enumerate(self.feature_names):
            col = X[:, j]
            missing = int((col == 0).sum()) if name in ZERO_MEANS_MISSING else 0
            present = col[col != 0] if name in ZERO_MEANS_MISSING else col
            columns.append({
                "column": name, "label": COLUMN_LABELS.get(name, name), "description": COLUMN_INFO.get(name, name), "n": int(len(col)),
                "missing_recorded_as_0": missing, "mean": round(float(present.mean()), 2),
                "min": round(float(present.min()), 2), "max": round(float(present.max()), 2),
            })
        out = {"role": self.role, "rows": int(len(X)), "columns": columns}
        if y is not None:
            out["label"] = {"column": LABEL_COLUMN, "name": COLUMN_LABELS[LABEL_COLUMN],
                            "description": COLUMN_INFO[LABEL_COLUMN],
                            "positives": int(y.sum()), "prevalence": round(float(y.mean()), 3)}
        return out

    def summary(self) -> dict:
        counts = {s: int((self.split == s).sum()) for s in ("train", "test", "review")}
        return {**self._summary, "splits": counts}

    def reset(self) -> None:
        self.w = np.zeros(len(self.feature_names))
        self.b = 0.0
        self.rounds = 0
        self.last_train_loss = None

    def _rows(self, split: str, entity_id: str | None = None) -> np.ndarray:
        if split == "review":
            return (self.split == "review") & (self.ids == entity_id)
        if split not in ("train", "test"):
            raise ValueError(f"unknown split {split}")
        return self.split == split

    def alignment_digest(self) -> str:
        """Hash of this site's train+test entity ids; lets the coordinator check alignment blind."""
        ids = self.ids[self.split != "review"]
        return hashlib.sha256("|".join(ids).encode()).hexdigest()

    def forward(self, split: str, nonce: str, entity_id: str | None = None) -> list[float]:
        if nonce in self.used_nonces:
            raise ValueError("nonce reuse refused: it would let masks be cancelled out")
        self.used_nonces.add(nonce)
        rows = self._rows(split, entity_id)
        if not rows.any():
            raise ValueError(f"no rows for split={split} entity={entity_id}")
        z = self.X[rows] @ self.w + self.b
        me = ROLE_ORDER.index(self.role)
        for other in range(len(ROLE_ORDER)):
            if other != me:
                a, b = sorted((me, other))
                mask = _pair_mask(self._secret, a, b, nonce, len(z))
                z += mask if me == a else -mask
        return z.tolist()

    def backward(self, residual: list[float], lr: float) -> None:
        X = self.X[self.split == "train"]
        r = np.asarray(residual, dtype=float)
        if r.shape != (len(X),):
            raise ValueError("residual length does not match this site's training rows")
        self.w -= lr * (X.T @ r) / len(r)
        self.rounds += 1

    def _require_labels(self) -> np.ndarray:
        if not self.is_label_holder:
            raise ValueError(f"{self.role} does not hold labels")
        return self.y

    def residual_step(self, z: list[float], lr: float) -> tuple[list[float], dict]:
        """Label holder: compute residuals from summed scores, update own weights."""
        y = self._require_labels()[self.split == "train"]
        p = sigmoid(np.asarray(z, dtype=float))
        if p.shape != y.shape:
            raise ValueError("score length does not match training rows")
        r = p - y
        self.b -= lr * float(r.mean())
        self.backward(r.tolist(), lr)
        loss = float(-np.mean(y * np.log(p + 1e-12) + (1 - y) * np.log(1 - p + 1e-12)))
        self.last_train_loss = loss
        return r.tolist(), {"train_loss": loss, "train_accuracy": float(((p >= 0.5) == y).mean())}

    def evaluate(self, z: list[float]) -> dict:
        y = self._require_labels()[self.split == "test"]
        p = sigmoid(np.asarray(z, dtype=float))
        if p.shape != y.shape:
            raise ValueError("score length does not match test rows")
        return {"test_accuracy": float(((p >= 0.5) == y).mean()), "test_auc": auc(y, p), "n_test": int(len(y))}


class LocalSite:
    """In-process handle to one department's model (used inside the SuperGrid AgentApp).

    Exposes exactly what the HTTP site servers expose, so the orchestrator below
    cannot tell (or reach past) the difference.
    """

    def __init__(self, model: SiteModel, peers: list["LocalSite"] | None = None):
        self._model = model
        self._peers = peers or []

    def reset(self) -> None:
        self._model.reset()

    def alignment(self) -> str:
        return self._model.alignment_digest()

    def summary(self) -> dict:
        return self._model.summary()

    def forward(self, split: str, nonce: str, patient_id: str | None = None) -> list[float]:
        return self._model.forward(split, nonce, patient_id)

    def backward(self, residual: list[float], lr: float) -> None:
        self._model.backward(residual, lr)

    def residual(self, z: list[float], lr: float) -> dict:
        residual, metrics = self._model.residual_step(z, lr)
        for peer in self._peers:  # residuals go department-to-department, never to the coordinator
            peer.backward(residual, lr)
        return metrics

    def evaluate(self, z: list[float]) -> dict:
        return self._model.evaluate(z)


def local_federation(data_dir: Path, secret: str | None = None) -> dict[str, LocalSite]:
    """One isolated model per department; each reads ONLY data_dir/<role>/cohort.jsonl.

    The mask secret is created here and handed only to the departments; the
    coordinator receives the handles, never the secret.
    ponytail: inside one SuperGrid process this isolation is enforced by code structure,
    not by separate machines; a physically distributed run needs one Flower node per department.
    """
    secret = secret or secrets.token_hex(16)
    lab = LocalSite(SiteModel("lab", data_dir / "lab" / "cohort.jsonl", secret))
    pharmacy = LocalSite(SiteModel("pharmacy", data_dir / "pharmacy" / "cohort.jsonl", secret))
    gp = LocalSite(SiteModel("gp", data_dir / "gp" / "cohort.jsonl", secret), peers=[lab, pharmacy])
    return {"gp": gp, "lab": lab, "pharmacy": pharmacy}


# --- coordinator side: sees only masked sums and aggregate metrics -------------

def _summed_scores(sites: dict, split: str, patient_id: str | None = None) -> list[float]:
    nonce = uuid.uuid4().hex
    parts = [site.forward(split, nonce, patient_id) for site in sites.values()]
    if len({len(p) for p in parts}) != 1:
        raise ValueError("departments returned different row counts; entity alignment is broken")
    return [sum(col) for col in zip(*parts)]


def train_federated(sites: dict, rounds: int = DEFAULT_ROUNDS, lr: float = DEFAULT_LR,
                    on_round=None) -> list[dict]:
    """Coordinator loop. `sites` maps role -> handle; the label holder is 'gp'."""
    if len({site.alignment() for site in sites.values()}) != 1:
        raise ValueError("departments' cohorts are not aligned")
    for site in sites.values():
        site.reset()
    history = []
    for t in range(1, rounds + 1):
        point = {"round": t, **sites["gp"].residual(_summed_scores(sites, "train"), lr)}
        if t % EVAL_EVERY == 0 or t == rounds:
            point |= sites["gp"].evaluate(_summed_scores(sites, "test"))
        history.append(point)
        if on_round:
            on_round(point)
    return history


def risk_band(p: float) -> str:
    return next(label for cut, label in RISK_BANDS if p < cut)


def score_patient(sites: dict, patient_id: str) -> dict:
    p = float(sigmoid(np.array(_summed_scores(sites, "review", patient_id)))[0])
    return {"probability": round(p, 3), "band": risk_band(p)}


def fl_report(sites: dict, history: list[dict], rounds: int, lr: float, source: str) -> dict:
    final = history[-1]
    return {
        "source": source, "rounds": rounds, "lr": lr,
        "test_auc": final["test_auc"], "test_accuracy": final["test_accuracy"],
        "n_test": final["n_test"], "train_loss": final["train_loss"],
        "history": history,
        "dataset_summary": [site.summary() for site in sites.values()],
    }
