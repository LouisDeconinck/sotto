"""Status page shaping: what the page is allowed to claim about what was observed.

Most of these guard one property. A status page is believed or it is useless, so the failures
worth testing are the ones where it would say something confident that the data does not
support: a day nobody checked drawn as a good day, a percentage with no denominator, an
unconfigured component voting on whether the service is up.
"""

import datetime as dt
import importlib.machinery
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOADER = importlib.machinery.SourceFileLoader("build_status", str(ROOT / "scripts/build-status"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
page = importlib.util.module_from_spec(SPEC)
sys.modules[LOADER.name] = page
LOADER.exec_module(page)

TODAY = dt.date(2026, 9, 9)


def component(state, days=None, **kw):
    return {"id": "x", "name": "X", "state": state, "days": days or [], **kw}


class Banner(unittest.TestCase):
    def test_everything_measured_and_up(self):
        self.assertEqual(page.overall([component("ok"), component("ok")]), page.OPERATIONAL)

    def test_one_down_is_partial_and_all_down_is_major(self):
        self.assertEqual(page.overall([component("ok"), component("down")]), page.PARTIAL)
        self.assertEqual(page.overall([component("down"), component("down")]), page.MAJOR)

    def test_an_unconfigured_component_does_not_vote(self):
        # A deployment that has chosen not to run billing is not having a billing outage, and
        # must not be shown one.
        self.assertEqual(page.overall([component("ok"), component("unconfigured")]),
                         page.OPERATIONAL)

    def test_nothing_measured_is_unknown_rather_than_fine(self):
        # The dangerous default. Before the first check lands, or if every probe is
        # unconfigured, there is no evidence of health, and "All systems operational" would be
        # an assertion nobody made.
        self.assertEqual(page.overall([component("unconfigured")]), page.UNKNOWN)
        self.assertEqual(page.overall([]), page.UNKNOWN)


class Bars(unittest.TestCase):
    def test_a_day_nobody_checked_is_blank_not_green(self):
        # The property the whole page rests on. The collector runs when GitHub gets round to
        # it, so gaps are normal; drawing them as good days would invent uptime, and drawing
        # them as bad ones would invent outages.
        slots = page.day_slots([{"date": "2026-09-09", "ok": 2, "total": 2}], TODAY)
        self.assertEqual(len(slots), page.SPAN_DAYS)
        self.assertIsNone(slots[0]["tally"], "the oldest day has no samples")
        self.assertEqual(page.bar_class(slots[0]["tally"]), "none")
        self.assertEqual(slots[-1]["date"], "2026-09-09")
        self.assertEqual(page.bar_class(slots[-1]["tally"]), "")

    def test_a_partly_failing_day_is_neither_colour(self):
        self.assertEqual(page.bar_class({"ok": 1, "total": 2}), "partial")
        self.assertEqual(page.bar_class({"ok": 0, "total": 2}), "down")

    def test_the_hover_text_carries_the_counts(self):
        slot = {"date": "2026-09-09", "tally": {"ok": 1, "total": 4}}
        self.assertEqual(page.bar_title(slot), "2026-09-09: 1 of 4 checks passed")
        self.assertIn("not checked", page.bar_title({"date": "2026-01-01", "tally": None}))


class Uptime(unittest.TestCase):
    def test_the_denominator_survives(self):
        up = page.uptime([{"date": "d", "ok": 3, "total": 4}])
        self.assertEqual((up["ok"], up["total"]), (3, 4))
        self.assertAlmostEqual(up["percent"], 75.0)

    def test_no_checks_is_not_zero_percent(self):
        # Zero would read as a total outage. There is simply nothing to report.
        self.assertIsNone(page.uptime([]))

    def test_cadence_is_measured_not_assumed(self):
        # The collector asks for one check every ten minutes and does not get it. Whatever the
        # page says about how often it looks has to come from the data.
        days = [{"date": "a", "ok": 2, "total": 2}, {"date": "b", "ok": 4, "total": 4}]
        self.assertEqual(page.observed_cadence(days), 3.0)
        self.assertIsNone(page.observed_cadence([]))


class Incidents(unittest.TestCase):
    def test_the_stage_comes_from_the_labels(self):
        issue = {"title": "Sync is slow", "state": "open", "createdAt": "2026-09-01T10:00:00Z",
                 "labels": [{"name": "incident"}, {"name": "identified"}], "comments": []}
        self.assertEqual(page.parse_incidents([issue], TODAY)[0]["stage"], "identified")

    def test_closing_the_issue_resolves_it_whatever_the_labels_say(self):
        # Otherwise an incident closed without tidying its labels would sit on the page
        # claiming to be under investigation for ever.
        issue = {"title": "Outage", "state": "closed", "createdAt": "2026-09-01T10:00:00Z",
                 "labels": [{"name": "investigating"}], "comments": []}
        self.assertEqual(page.parse_incidents([issue], TODAY)[0]["stage"], "resolved")

    def test_comments_become_the_updates_in_order(self):
        issue = {"title": "Outage", "state": "open", "createdAt": "2026-09-01T10:00:00Z",
                 "labels": [], "comments": [
                     {"createdAt": "2026-09-01T10:30:00Z", "body": "Looking into it"},
                     {"createdAt": "2026-09-01T11:00:00Z", "body": "Fixed"}]}
        updates = page.parse_incidents([issue], TODAY)[0]["updates"]
        self.assertEqual([u["body"] for u in updates], ["Looking into it", "Fixed"])
        self.assertEqual(updates[0]["at"], "2026-09-01 10:30")

    def test_newest_first_even_on_the_same_day(self):
        # Two incidents on one bad day is exactly when the order matters, and exactly when
        # sorting on the date alone stops distinguishing them.
        def issue(title, at):
            return {"title": title, "createdAt": at, "labels": [], "comments": []}

        issues = [issue("Morning", "2026-09-08T09:00:00Z"),
                  issue("Evening", "2026-09-08T21:00:00Z"),
                  issue("Yesterday", "2026-09-07T12:00:00Z")]
        self.assertEqual([i["title"] for i in page.parse_incidents(issues, TODAY)],
                         ["Evening", "Morning", "Yesterday"])

    def test_updates_are_ordered_however_the_api_returned_them(self):
        issue = {"title": "Outage", "state": "open", "createdAt": "2026-09-01T10:00:00Z",
                 "labels": [], "comments": [
                     {"createdAt": "2026-09-01T12:00:00Z", "body": "Resolved"},
                     {"createdAt": "2026-09-01T10:30:00Z", "body": "Looking into it"}]}
        updates = page.parse_incidents([issue], TODAY)[0]["updates"]
        self.assertEqual([u["body"] for u in updates], ["Looking into it", "Resolved"])

    def test_a_resolved_label_does_not_resolve_an_open_incident(self):
        # Closing the issue is what resolves an incident. Reading it from a label would let a
        # stale one publish an outage as over while it was still happening.
        issue = {"title": "Ongoing", "state": "open", "createdAt": "2026-09-08T10:00:00Z",
                 "labels": [{"name": "resolved"}], "comments": []}
        self.assertEqual(page.parse_incidents([issue], TODAY)[0]["stage"], "investigating")

    def test_old_closed_incidents_age_out_but_open_ones_never_do(self):
        # The bars cover ninety days, so the log does too. The exception is the one that
        # matters: ageing out an incident that is still happening would be the worst thing
        # this page could do.
        stale = {"title": "Long resolved", "state": "closed", "labels": [], "comments": [],
                 "createdAt": "2025-01-01T00:00:00Z"}
        ancient_open = {"title": "Still open", "state": "open", "labels": [], "comments": [],
                        "createdAt": "2025-01-01T00:00:00Z"}
        titles = [i["title"] for i in page.parse_incidents([stale, ancient_open], TODAY)]
        self.assertEqual(titles, ["Still open"])


class Rendering(unittest.TestCase):
    def summary(self, **kw):
        return {"generated_at": "2026-09-09T05:05:01Z",
                "components": [component("ok", [{"date": "2026-09-09", "ok": 4, "total": 4}],
                                         description="D", **kw)]}

    def test_the_page_states_the_denominator_and_not_only_the_percentage(self):
        out = page.render(page.build(self.summary(), [], TODAY),
                          dt.datetime(2026, 9, 9, tzinfo=dt.timezone.utc))
        self.assertIn("100.00% of 4 checks", out)
        self.assertIn("sampled, not measured continuously", out)
        self.assertIn("shown blank rather than green", out)

    def test_content_from_the_collector_is_escaped(self):
        # Details are written by the probe and can quote a redirect target or an error, so they
        # are not this page's to trust; a status page that could be scripted by a misbehaving
        # deployment would be a poor thing to publish.
        model = page.build(self.summary(detail='<img src=x onerror="alert(1)">'), [], TODAY)
        out = page.render(model, dt.datetime(2026, 9, 9, tzinfo=dt.timezone.utc))
        self.assertNotIn("<img src=x", out)
        self.assertIn("&lt;img src=x", out)

    def test_a_full_fetch_is_reported_as_possibly_incomplete(self):
        # A short list that does not say it is short is the one failure a page about honesty
        # cannot afford.
        model = page.build(self.summary(), [], TODAY, truncated=True)
        out = page.render(model, dt.datetime(2026, 9, 9, tzinfo=dt.timezone.utc))
        self.assertIn("the list above is incomplete", out)
        self.assertNotIn("incomplete", page.render(page.build(self.summary(), [], TODAY),
                                                   dt.datetime(2026, 9, 9, tzinfo=dt.timezone.utc)))

    def test_a_zero_is_rendered_rather_than_blanked(self):
        # `text or ""` would turn a legitimate 0 into nothing at all.
        self.assertEqual(page.e(0), "0")
        self.assertEqual(page.e(False), "False")
        self.assertEqual(page.e(None), "")

    def test_it_says_so_when_nothing_has_been_observed(self):
        model = page.build({"generated_at": "x", "components": [component("unconfigured")]},
                           [], TODAY)
        out = page.render(model, dt.datetime(2026, 9, 9, tzinfo=dt.timezone.utc))
        self.assertIn("Status unknown", out)
        self.assertNotIn("All systems operational", out)


if __name__ == "__main__":
    unittest.main()
