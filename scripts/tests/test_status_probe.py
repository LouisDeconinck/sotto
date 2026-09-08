"""Status probe verdicts and the running summary they accumulate into.

The verdicts are the part worth pinning down. Each one decides whether a public answer
means the component works, and several of the wrong readings are worse than a missed
outage: an unsigned webhook that succeeds is a security failure reported as green, and the
single page app answering for the API is the exact shape of a misconfigured deployment
that looks healthy from outside.
"""

import datetime as dt
import importlib.machinery
import importlib.util
import json
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOADER = importlib.machinery.SourceFileLoader("status_probe", str(ROOT / "scripts/status-probe"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
probe = importlib.util.module_from_spec(SPEC)
# Register before executing: the module defines dataclasses, and the decorator resolves
# annotations through sys.modules, which a loader-only import leaves without an entry.
sys.modules[LOADER.name] = probe
LOADER.exec_module(probe)

NOW = dt.datetime(2026, 9, 8, 12, 0, tzinfo=dt.timezone.utc)


def response(status, headers=None, body=""):
    return probe.Response(status=status, headers=headers or {}, body_prefix=body)


class ApiVerdict(unittest.TestCase):
    def test_ok_needs_the_body_not_just_the_status(self):
        self.assertEqual(probe.judge_api(response(200, body="ok\n")).state, probe.OK)
        self.assertEqual(probe.judge_api(response(200, body="")).state, probe.DOWN)

    def test_a_readiness_503_is_an_outage_not_a_missing_feature(self):
        # The endpoint returns 503 for exactly one reason: it could not reach the database.
        # Excusing that as "unconfigured" would drop it out of the uptime tally entirely,
        # so the one failure the probe exists to see would be the one it never counted.
        outcome = probe.judge_api(response(503))
        self.assertEqual(outcome.state, probe.DOWN)
        self.assertIn("database", outcome.detail)

    def test_html_names_the_reverse_proxy_rather_than_the_api(self):
        outcome = probe.judge_api(response(200, {"content-type": "text/html; charset=utf-8"}))
        self.assertEqual(outcome.state, probe.DOWN)
        self.assertIn("web app answered", outcome.detail)

    def test_the_diagnostic_survives_an_unusually_cased_media_type(self):
        outcome = probe.judge_api(response(200, {"content-type": "TEXT/HTML"}))
        self.assertIn("web app answered", outcome.detail)


class WebVerdict(unittest.TestCase):
    def test_html_is_the_whole_requirement(self):
        ok = response(200, {"content-type": "text/html; charset=utf-8"})
        self.assertEqual(probe.judge_web(ok).state, probe.OK)

    def test_the_media_type_is_matched_the_way_http_defines_it(self):
        # Case insensitive, and the whole token rather than a prefix. A prefix match would
        # call a legal Text/HTML response an outage and a text/html-extra one healthy, both
        # of which put the wrong cause in a record people are meant to trust.
        upper = response(200, {"content-type": "Text/HTML; charset=UTF-8"})
        self.assertEqual(probe.judge_web(upper).state, probe.OK)
        lookalike = response(200, {"content-type": "text/html-extra"})
        self.assertEqual(probe.judge_web(lookalike).state, probe.DOWN)

    def test_a_200_that_is_not_a_page_is_not_the_app(self):
        self.assertEqual(probe.judge_web(response(200, {"content-type": "text/plain"})).state, probe.DOWN)
        self.assertEqual(probe.judge_web(response(502)).state, probe.DOWN)


class SigninVerdict(unittest.TestCase):
    def test_a_redirect_to_github_is_the_flow_starting(self):
        r = response(303, {"location": "https://github.com/login/oauth/authorize?client_id=x"})
        self.assertEqual(probe.judge_signin(r).state, probe.OK)

    def test_a_redirect_somewhere_else_is_not(self):
        # A deployment sending sign-in traffic anywhere but GitHub is broken at best, and
        # counting it as healthy would hide the more alarming readings of the same symptom.
        r = response(303, {"location": "https://elsewhere.example/login"})
        self.assertEqual(probe.judge_signin(r).state, probe.DOWN)

    def test_no_oauth_credentials_is_a_choice_not_an_outage(self):
        self.assertEqual(probe.judge_signin(response(503)).state, probe.UNCONFIGURED)

    def test_a_200_means_the_redirect_never_happened(self):
        self.assertEqual(probe.judge_signin(response(200)).state, probe.DOWN)


class BillingVerdict(unittest.TestCase):
    def test_rejecting_an_unsigned_webhook_is_the_healthy_answer(self):
        self.assertEqual(probe.judge_billing(response(401)).state, probe.OK)

    def test_accepting_an_unsigned_webhook_is_never_healthy(self):
        # This probe sends an unsigned payload. A 200 means signature verification is not
        # happening, which is a security failure, and reporting it green on a status page
        # would be the worst possible place to be quiet about it.
        self.assertEqual(probe.judge_billing(response(200)).state, probe.DOWN)

    def test_no_billing_configured_is_a_choice_not_an_outage(self):
        self.assertEqual(probe.judge_billing(response(503)).state, probe.UNCONFIGURED)


class Observation(unittest.TestCase):
    def probe_for(self, judge, path="/x"):
        return probe.Probe(id="t", name="T", description="", method="GET", path=path, judge=judge)

    def test_a_transport_failure_is_recorded_as_the_component_being_gone(self):
        # Nothing is listening on port 1, so this is a real connection refusal rather than a
        # stubbed one, and it is the honest reading: no visitor could have used it either.
        outcomes = probe.observe("http://127.0.0.1:1", [self.probe_for(probe.judge_web)])
        self.assertEqual(outcomes["t"].state, probe.DOWN)
        self.assertIn("URLError", outcomes["t"].detail)

    def test_a_broken_verdict_raises_rather_than_reporting_an_outage(self):
        # The failure this guards against is subtle and bad: a defect in our own code recorded
        # as somebody else's downtime, published on a status page, with the job still green.
        # Nothing would ever have pointed at the collector.
        def broken(_response):
            raise AttributeError("verdict bug")

        with unittest.mock.patch.object(probe, "fetch", return_value=response(200)):
            with self.assertRaises(AttributeError):
                probe.observe("https://example.invalid", [self.probe_for(broken)])


class Summary(unittest.TestCase):
    def test_a_first_round_records_every_component(self):
        summary = probe.merge(probe.empty_summary(), {"api": probe.Outcome(probe.OK)}, NOW)
        ids = [c["id"] for c in summary["components"]]
        self.assertEqual(ids, ["api", "web", "signin", "billing", "sync"])
        self.assertEqual(summary["generated_at"], "2026-09-08T12:00:00Z")

    def test_tallies_accumulate_across_rounds(self):
        summary = probe.empty_summary()
        summary = probe.merge(summary, {"api": probe.Outcome(probe.OK)}, NOW)
        summary = probe.merge(summary, {"api": probe.Outcome(probe.DOWN, "boom")}, NOW)
        api = next(c for c in summary["components"] if c["id"] == "api")
        self.assertEqual(api["days"], [{"date": "2026-09-08", "ok": 1, "total": 2}])
        self.assertEqual(api["state"], probe.DOWN)
        self.assertEqual(api["detail"], "boom")

    def test_an_unconfigured_component_never_enters_the_tally(self):
        # Otherwise every self-hoster running without billing would watch their published
        # uptime fall for a feature they deliberately do not run.
        summary = probe.merge(probe.empty_summary(), {"billing": probe.Outcome(probe.UNCONFIGURED)}, NOW)
        billing = next(c for c in summary["components"] if c["id"] == "billing")
        self.assertEqual(billing["days"], [])
        self.assertEqual(billing["state"], probe.UNCONFIGURED)

    def test_a_component_with_no_probe_is_declared_not_omitted(self):
        summary = probe.merge(probe.empty_summary(), {}, NOW)
        sync = next(c for c in summary["components"] if c["id"] == "sync")
        self.assertEqual(sync["state"], probe.UNCONFIGURED)
        self.assertEqual(sync["days"], [])

    def test_days_older_than_the_horizon_fall_off(self):
        days = {
            "2026-01-01": {"date": "2026-01-01", "ok": 1, "total": 1},
            "2026-09-08": {"date": "2026-09-08", "ok": 1, "total": 1},
        }
        kept = probe.trim(days, NOW.date())
        self.assertEqual([d["date"] for d in kept], ["2026-09-08"])

    def test_the_horizon_keeps_exactly_the_retained_window(self):
        oldest = NOW.date() - dt.timedelta(days=probe.RETAINED_DAYS - 1)
        days = {d.isoformat(): {"date": d.isoformat(), "ok": 1, "total": 1}
                for d in (oldest - dt.timedelta(days=1), oldest, NOW.date())}
        kept = [d["date"] for d in probe.trim(days, NOW.date())]
        self.assertIn(oldest.isoformat(), kept)
        self.assertNotIn((oldest - dt.timedelta(days=1)).isoformat(), kept)


class Samples(unittest.TestCase):
    def test_one_line_per_observation_carrying_its_own_timestamp(self):
        # The cadence of this job is whatever GitHub's scheduler decides on the day, so a
        # sample that did not carry its own time could only be placed by assuming one.
        lines = probe.sample_lines({"api": probe.Outcome(probe.OK), "web": probe.Outcome(probe.DOWN, "x")}, NOW)
        parsed = [json.loads(line) for line in lines]
        self.assertEqual([p["component"] for p in parsed], ["api", "web"])
        self.assertEqual(parsed[0]["at"], "2026-09-08T12:00:00Z")
        self.assertEqual(parsed[1]["detail"], "x")


class Persistence(unittest.TestCase):
    def test_a_missing_summary_starts_empty_rather_than_failing(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(probe.load(d)["components"], [])

    def test_a_second_run_appends_rather_than_replacing(self):
        with tempfile.TemporaryDirectory() as d:
            for _ in range(2):
                outcomes = {"api": probe.Outcome(probe.OK)}
                probe.write(d, probe.merge(probe.load(d), outcomes, NOW), probe.sample_lines(outcomes, NOW), NOW)
            api = next(c for c in probe.load(d)["components"] if c["id"] == "api")
            self.assertEqual(api["days"], [{"date": "2026-09-08", "ok": 2, "total": 2}])
            lines = (Path(d) / "samples" / "2026-09-08.jsonl").read_text().strip().split("\n")
            self.assertEqual(len(lines), 2)


if __name__ == "__main__":
    unittest.main()
