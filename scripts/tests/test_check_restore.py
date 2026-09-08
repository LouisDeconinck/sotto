"""Restore assertions: what a dump has to prove before anyone would trust it.

The rehearsal these encode was run by hand on 2026-08-31 against the first backup taken from
the post-upgrade schema. The point of writing it down is that a drill nobody repeats is a drill
that stops being true, which is the same failure the backup cron itself had.
"""

import importlib.machinery
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOADER = importlib.machinery.SourceFileLoader("check_restore", str(ROOT / "scripts/check-restore"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
restore = importlib.util.module_from_spec(SPEC)
sys.modules[LOADER.name] = restore
LOADER.exec_module(restore)

REPO = [str(n) for n in range(1, 22)]


class Migrations(unittest.TestCase):
    def test_a_healthy_dump_passes(self):
        applied = [(v, "t") for v in REPO]
        self.assertEqual(restore.check_migrations(applied, REPO), [])

    def test_a_deployment_behind_this_branch_is_not_a_failure(self):
        # The property that decides whether anyone keeps paying attention to this job. A
        # migration merges, production has not taken it yet, and every monthly run in between
        # would go red for a backup that is completely fine.
        applied = [(v, "t") for v in REPO[:-3]]
        self.assertEqual(restore.check_migrations(applied, REPO), [])

    def test_a_deployment_ahead_of_this_branch_is_a_failure(self):
        # The other direction is not symmetrical. A dump carrying schema this checkout does not
        # know about cannot be restored into something this code can run, which is the only
        # question a restore drill is asking.
        applied = [(v, "t") for v in REPO + ["22"]]
        problems = restore.check_migrations(applied, REPO)
        self.assertEqual(len(problems), 1)
        self.assertIn("22", problems[0])
        self.assertIn("ahead of this branch", problems[0])

    def test_a_migration_recorded_as_failed_is_caught(self):
        applied = [(v, "t") for v in REPO[:-1]] + [("21", "f")]
        problems = restore.check_migrations(applied, REPO)
        self.assertIn("recorded as failed: 21", problems[0])

    def test_a_dump_with_no_migrations_at_all_is_caught(self):
        # What restoring an empty or wrong database looks like from here.
        problems = restore.check_migrations([], REPO)
        self.assertIn("records no migrations", problems[0])


class Tables(unittest.TestCase):
    def test_the_deletion_tables_are_required(self):
        # Named because organisation deletion is the one thing in Sotto that cannot be undone
        # from inside the product, so this backup is the only thing behind it.
        present = ["users", "organizations", "audit_events"]
        problems = restore.check_tables(present)
        self.assertIn("organization_deletions", problems[0])

    def test_a_complete_schema_passes(self):
        self.assertEqual(restore.check_tables(list(restore.MUST_EXIST)), [])


class Emptiness(unittest.TestCase):
    def test_a_restore_that_carried_no_rows_is_caught(self):
        # A dump of the wrong database, or of one truncated before the dump ran, passes every
        # structural check ever written. This is the one that notices.
        problems = restore.check_not_empty({"users": 0, "organizations": 1})
        self.assertIn("restored but empty: users", problems[0])

    def test_rows_present_passes(self):
        self.assertEqual(restore.check_not_empty({"users": 1, "organizations": 1}), [])


class RepositoryMigrations(unittest.TestCase):
    def test_it_reads_the_versions_this_checkout_actually_carries(self):
        versions = restore.repository_migrations(str(ROOT / "crates/server/migrations"))
        self.assertEqual(versions[:3], ["1", "2", "3"])
        self.assertEqual(len(set(versions)), len(versions), "no version is counted twice")

    def test_nothing_but_a_numbered_migration_counts(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ("0001_init.sql", "README.md", "notes.txt", "backup.sql.bak"):
                (Path(d) / name).write_text("")
            self.assertEqual(restore.repository_migrations(d), ["1"])


if __name__ == "__main__":
    unittest.main()
