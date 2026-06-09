import random
from datetime import UTC, datetime

from core.management.commands import seed_rich_demo as gen


def test_cgm_value_stays_in_physiologic_range():
    rng = random.Random("test")
    for hour in range(24):
        dt = datetime(2026, 6, 1, hour, 0, tzinfo=UTC)
        for risk in (0.0, 0.4, 0.85):
            v = gen.cgm_value(dt, risk, rng)
            assert 40 <= v <= 300
            assert isinstance(v, int)


def test_cgm_value_rises_after_a_meal():
    pre_meal = datetime(2026, 6, 1, 6, 0, tzinfo=UTC)  # before breakfast
    post_meal = datetime(2026, 6, 1, 8, 15, tzinfo=UTC)  # ~45 min after 7:30
    # Average several draws to wash out gaussian noise.
    pre = sum(gen.cgm_value(pre_meal, 0.4, random.Random(i)) for i in range(50)) / 50
    post = sum(gen.cgm_value(post_meal, 0.4, random.Random(i)) for i in range(50)) / 50
    assert post > pre + 15
