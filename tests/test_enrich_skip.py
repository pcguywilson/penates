"""Pure skip helper for resolve desc enrichment. No HTTP, no store."""
import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import resolve

NOW = 1_800_000_000
DAY = 24 * 3600


def _row(desc="short", tried_at=None, tried_len=None):
    r = {"desc": desc}
    if tried_at is not None:
        r["enrich_tried_at"] = tried_at
    if tried_len is not None:
        r["enrich_tried_len"] = tried_len
    return r


class EnrichSkipTest(unittest.TestCase):
    def test_unstamped_does_not_skip(self):
        self.assertFalse(resolve.enrich_should_skip(_row("abc"), NOW))

    def test_recent_same_len_skips(self):
        r = _row("abc", NOW - 2 * DAY, 3)
        self.assertTrue(resolve.enrich_should_skip(r, NOW))

    def test_exactly_seven_days_retries(self):
        r = _row("abc", NOW - resolve.ENRICH_WINDOW_S, 3)
        self.assertFalse(resolve.enrich_should_skip(r, NOW))

    def test_older_than_seven_days_retries(self):
        r = _row("abc", NOW - resolve.ENRICH_WINDOW_S - 1, 3)
        self.assertFalse(resolve.enrich_should_skip(r, NOW))

    def test_changed_desc_retries_immediately(self):
        r = _row("abcd", NOW - 60, 3)
        self.assertFalse(resolve.enrich_should_skip(r, NOW))

    def test_force_ignores_fresh_stamp(self):
        r = _row("abc", NOW - 60, 3)
        self.assertFalse(resolve.enrich_should_skip(r, NOW, force=True))

    def test_non_int_stamps_do_not_skip(self):
        self.assertFalse(resolve.enrich_should_skip(_row("abc", "1700000000", 3), NOW))
        self.assertFalse(resolve.enrich_should_skip(_row("abc", NOW - 60, "3"), NOW))
        self.assertFalse(resolve.enrich_should_skip(_row("abc", True, 3), NOW))
        self.assertFalse(resolve.enrich_should_skip(_row("abc", NOW - 60, False), NOW))

    def test_window_is_seven_days(self):
        self.assertEqual(resolve.ENRICH_WINDOW_S, 7 * DAY)


if __name__ == "__main__":
    unittest.main()
