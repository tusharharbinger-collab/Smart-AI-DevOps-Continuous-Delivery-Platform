"""
services/verification-engine/tests/test_confidence.py
"""
import pytest
from src.scoring.confidence import compute_confidence, compute_composite_score, determine_verdict


class TestComputeConfidence:
    def test_full_confidence_ideal_conditions(self):
        c = compute_confidence(
            n_baseline=500, n_canary=500,
            var_baseline=0.001, var_canary=0.001,
            elapsed_seconds=300, min_eval_seconds=120,
        )
        assert c == pytest.approx(1.0, abs=0.05)

    def test_low_sample_count_low_confidence(self):
        c = compute_confidence(
            n_baseline=500, n_canary=5,   # only 5 canary samples
            var_baseline=0.001, var_canary=0.001,
            elapsed_seconds=120, min_eval_seconds=120,
        )
        assert c < 0.30, "Low sample count must yield low confidence"

    def test_high_variance_divergence_penalized(self):
        c_low_div = compute_confidence(500, 500, 0.001, 0.001, 300, 120)
        c_high_div = compute_confidence(500, 500, 0.001, 0.100, 300, 120)
        assert c_high_div < c_low_div, "High variance divergence must reduce confidence"

    def test_early_verdict_penalized(self):
        c_full = compute_confidence(500, 500, 0.001, 0.001, 300, 120)
        c_early = compute_confidence(500, 500, 0.001, 0.001, 10, 120)
        assert c_early < c_full, "Early verdict must have lower stability factor"

    def test_n_required_floor_at_100(self):
        c = compute_confidence(
            n_baseline=500, n_canary=100,
            var_baseline=0.001, var_canary=0.001,
            elapsed_seconds=300, min_eval_seconds=120,
            n_required=100,
        )
        # N = min(500, 100) = 100, C_sample = sqrt(100/100) = 1.0
        assert c > 0.9

    def test_confidence_between_0_and_1(self):
        c = compute_confidence(10, 10, 0.5, 2.0, 5, 120)
        assert 0.0 <= c <= 1.0


class TestComputeCompositeScore:
    def test_all_healthy_metrics_score_100(self):
        scores = [
            {"tier": "important", "weight": 1.0, "score": 100.0},
            {"tier": "important", "weight": 2.5, "score": 100.0},
        ]
        assert compute_composite_score(scores) == pytest.approx(100.0)

    def test_weighted_average_correct(self):
        scores = [
            {"tier": "important", "weight": 1.0, "score": 80.0},
            {"tier": "important", "weight": 3.0, "score": 60.0},
        ]
        expected = (1.0 * 80.0 + 3.0 * 60.0) / 4.0
        assert compute_composite_score(scores) == pytest.approx(expected)

    def test_critical_tier_excluded_from_weighted_average(self):
        scores = [
            {"tier": "critical", "weight": 10.0, "score": 0.0},   # excluded
            {"tier": "important", "weight": 1.0, "score": 90.0},
        ]
        # Should return 90.0, not be dragged down by the critical metric
        assert compute_composite_score(scores) == pytest.approx(90.0)

    def test_empty_scorable_metrics_returns_100(self):
        scores = [{"tier": "critical", "weight": 1.0, "score": 50.0}]
        assert compute_composite_score(scores) == pytest.approx(100.0)


class TestDetermineVerdict:
    def test_tier1_breach_is_failed(self):
        assert determine_verdict(
            tier1_breaches=["http_error_rate"],
            composite_score=95.0, confidence=0.95,
            cusum_detected=False, bocpd_detected=False, business_metric_breach=False,
        ) == "FAILED"

    def test_business_metric_breach_is_failed(self):
        assert determine_verdict(
            tier1_breaches=[],
            composite_score=90.0, confidence=0.90,
            cusum_detected=False, bocpd_detected=False, business_metric_breach=True,
        ) == "FAILED"

    def test_low_composite_score_is_failed(self):
        assert determine_verdict(
            tier1_breaches=[], composite_score=50.0, confidence=0.95,
            cusum_detected=False, bocpd_detected=False, business_metric_breach=False,
        ) == "FAILED"

    def test_low_confidence_is_degraded(self):
        assert determine_verdict(
            tier1_breaches=[], composite_score=90.0, confidence=0.70,
            cusum_detected=False, bocpd_detected=False, business_metric_breach=False,
        ) == "DEGRADED"

    def test_cusum_detected_is_degraded(self):
        assert determine_verdict(
            tier1_breaches=[], composite_score=90.0, confidence=0.90,
            cusum_detected=True, bocpd_detected=False, business_metric_breach=False,
        ) == "DEGRADED"

    def test_healthy_all_conditions_met(self):
        assert determine_verdict(
            tier1_breaches=[], composite_score=90.0, confidence=0.95,
            cusum_detected=False, bocpd_detected=False, business_metric_breach=False,
        ) == "HEALTHY"
