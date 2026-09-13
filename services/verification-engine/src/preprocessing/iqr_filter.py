"""
services/verification-engine/src/preprocessing/iqr_filter.py

IQR-based outlier filter — removes extreme spike values before statistical
tests are applied. This prevents transient scrape errors or network timeouts
from contaminating the latency distribution and causing false positives.

The filter clips values beyond Q1 - k*IQR and Q3 + k*IQR (default k=1.5,
the standard Tukey fence). Filtered-out values are logged but NOT discarded
entirely — they are tracked as "spike events" for the Isolation Forest.
"""
import numpy as np


def apply_iqr_filter(
    samples: np.ndarray,
    k: float = 1.5,
) -> np.ndarray:
    """
    Remove outliers beyond Tukey's k-IQR fence.

    Parameters
    ----------
    samples : np.ndarray  1-D array of metric samples
    k       : float       fence multiplier (1.5 = standard, 3.0 = extreme-only)

    Returns
    -------
    np.ndarray  filtered samples (outliers removed, not clipped)
    """
    if len(samples) < 4:
        return samples   # not enough data to estimate quartiles

    q1 = np.percentile(samples, 25)
    q3 = np.percentile(samples, 75)
    iqr = q3 - q1

    lower = q1 - k * iqr
    upper = q3 + k * iqr

    mask = (samples >= lower) & (samples <= upper)
    return samples[mask]


def spike_count(samples: np.ndarray, k: float = 1.5) -> int:
    """Return the number of samples that would be filtered out."""
    if len(samples) < 4:
        return 0
    q1 = np.percentile(samples, 25)
    q3 = np.percentile(samples, 75)
    iqr = q3 - q1
    lower = q1 - k * iqr
    upper = q3 + k * iqr
    return int(np.sum((samples < lower) | (samples > upper)))
