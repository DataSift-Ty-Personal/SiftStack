"""The Register of Deeds puller must name a lapsed subscription, not guess.

Zero network. A lapsed account logs in and renders the search page normally;
the only signals are the header timer and a browser alert. From 2026-08-26 that
was reported as "the page layout changed" for 11 weeks.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import knox_rod  # noqa: E402


def test_the_live_expired_timer_reads_as_expired():
    assert knox_rod.subscription_state("DAYS UNTIL SUBSCRIPTION EXPIRES: Expired") == ("expired", None)


def test_days_left_are_read_as_a_number():
    assert knox_rod.subscription_state("DAYS UNTIL SUBSCRIPTION EXPIRES: 23") == ("active", 23)
    assert knox_rod.subscription_state("days until subscription expires:   7 ") == ("active", 7)


def test_a_missing_timer_is_unknown_not_expired():
    # Unknown must not block a pull: the search itself is the second check.
    assert knox_rod.subscription_state("") == ("unknown", None)
    assert knox_rod.subscription_state(None) == ("unknown", None)
    assert knox_rod.subscription_state("DAYS UNTIL SUBSCRIPTION EXPIRES:") == ("unknown", None)


def test_expired_is_its_own_error_type():
    assert issubclass(knox_rod.RodSubscriptionExpired, RuntimeError)
    assert knox_rod.SUBSCRIPTION_WARN_DAYS >= 7
