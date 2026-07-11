from rs_project.core import DEFAULT_THRESHOLDS, score_to_rating


def test_score_to_rating_extremes() -> None:
    assert score_to_rating(250.0, DEFAULT_THRESHOLDS) == 99
    assert score_to_rating(10.0, DEFAULT_THRESHOLDS) == 1


def test_score_to_rating_midrange() -> None:
    rating = score_to_rating(100.0, DEFAULT_THRESHOLDS)
    assert 50 <= rating <= 89
