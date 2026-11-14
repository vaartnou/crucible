"""Public-information Cala teacher, used only for offline training."""

import numpy as np

DEFAULT_TEACHER = {"power": 2.0, "late": 0.4, "safety": 1.5, "expedite": 0.75, "bias": -0.15}


def policy(x, p):
    # Seven public numbers per supplier: active, pending, ETA, risk, late, lead, fill.
    # Risk/fill predict loss; acknowledged overdue bookings add a separate penalty.
    a = np.zeros(16)
    avail = (np.clip(x[6:70:7], 0.1, 1) + np.clip(1 - (x[3:70:7] - 0.15) / 0.7, 0.1, 1)) / 2
    late = x[4:70:7]
    logits = p.get("power", 2.0) * np.log(avail) - p.get("late", 0.0) * np.clip(late, 0, 5)
    logits -= logits.mean()
    a[:10] = np.clip(logits / 4, -1, 1)
    # Add stock/expedite only when broad public availability falls below threshold.
    stress = max(0.0, p.get("threshold", 0.95) - np.mean(avail))
    stress = max(stress, p.get("late_stress", 0.0) * float(np.mean(late)))
    # Index 104 is on-hand normalized by one week of mean demand.
    onhand_days = x[104] * 7
    a[12] = p.get("bias", 0.0) + p.get("inventory", 0.0) * (p.get("target", 2.0) - onhand_days)
    a[13] = p.get("safety", 0.0) * stress
    a[14] = p.get("expedite", 0.0) * stress
    a[15] = -0.75
    return np.clip(a, -1, 1)


def training_partitions():
    """One seed in each width-31 stratum; selection is offset by 15."""
    tuning = [20000 + 31 * i for i in range(64)]
    validation = [20015 + 31 * i for i in range(64)]
    reserved = set(tuning + validation)
    available = [s for s in range(20000, 22000) if s not in reserved]
    distill = available[:128]
    gradient = available[128:]
    return tuning, validation, distill, gradient
