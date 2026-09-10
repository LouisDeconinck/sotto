"""Behaviour of deploy/rotate-deletion-tokens.sh, against a stubbed deployment.

The script edits a secrets file and restarts a server, so it cannot be exercised anywhere real
and had been checked by hand instead. That missed a bug three times over: the stubs answered
whatever was asked of them, so a script pointed at an address nothing serves looked perfectly
healthy. The stubs here model the deployment's actual topology, where the server has no port on
the host and is reachable only at the served origin, because that is the assumption that was
wrong.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy/rotate-deletion-tokens.sh"

# Answers at the served origin, and refuses at any host port, because compose publishes none.
CURL_STUB = """#!/bin/sh
url=""; token=""
while [ $# -gt 0 ]; do
  case "$1" in
    http*) url="$1" ;;
    "@"*) f="${1#@}"; [ -f "$f" ] && token="$(sed -n 's/^Authorization: Bearer //p' "$f")" ;;
  esac
  shift
done
case "$url" in *127.0.0.1:*|*localhost:*) printf 000; exit 7 ;; esac
if [ -n "$STUB_UNREACHABLE" ]; then printf 000; exit 7; fi
case "$url" in *"/health"*) exit 0;; esac
new_m=$(grep -m1 '^SOTTO_ORGANISATION_DELETION_METRICS_TOKEN=' "$PWD/.env" | cut -d= -f2-)
new_o=$(grep -m1 '^SOTTO_ORGANISATION_DELETION_OPERATOR_TOKEN=' "$PWD/.env" | cut -d= -f2-)
if [ -n "$token" ] && { [ "$token" = "$new_m" ] || [ "$token" = "$new_o" ]; }; then
  printf 200
else
  printf 401
fi
"""

DOCKER_STUB = "#!/bin/sh\nexit 0\n"

HEALTHY_ENV = (
    "SOTTO_DOMAIN=example.test\n"
    "SOTTO_ORGANISATION_DELETION_METRICS_TOKEN=old-metrics\n"
    "SOTTO_ORGANISATION_DELETION_OPERATOR_TOKEN=old-operator\n"
    "STRIPE_API_KEY=untouched\n"
)


class Rotation(unittest.TestCase):
    def run_script(self, env_text, **environ):
        work = Path(self.enterContext(tempfile.TemporaryDirectory()))
        bin_dir = work / "bin"
        bin_dir.mkdir()
        for name, body in (("curl", CURL_STUB), ("docker", DOCKER_STUB)):
            (bin_dir / name).write_text(body)
            (bin_dir / name).chmod(0o755)
        shutil.copy(SCRIPT, work / SCRIPT.name)
        (work / ".env").write_text(env_text)
        result = subprocess.run(
            ["bash", f"./{SCRIPT.name}"],
            cwd=work,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                # One attempt, so proving the timeout works does not cost a minute of it.
                "SOTTO_ROTATE_HEALTH_ATTEMPTS": "1",
                **environ,
            },
        )
        return result, work

    def test_a_healthy_rotation_verifies_and_changes_only_the_tokens(self):
        result, work = self.run_script(HEALTHY_ENV)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.count("  ok "), 5, result.stdout)
        env = (work / ".env").read_text()
        self.assertIn("STRIPE_API_KEY=untouched", env, "it edits only what it was asked to")
        self.assertNotIn("old-metrics", env)
        self.assertNotIn("old-operator", env)

    def test_no_token_reaches_the_output(self):
        # Not even the new ones. The reason these need rotating at all is that the previous pair
        # was printed into a transcript.
        result, work = self.run_script(HEALTHY_ENV)
        combined = result.stdout + result.stderr
        for line in (work / ".env").read_text().splitlines():
            if "TOKEN=" in line:
                self.assertNotIn(line.split("=", 1)[1], combined)

    def test_it_refuses_an_env_file_that_says_two_things(self):
        # Compose reads the last occurrence and this script reads the first, so a duplicate lets
        # the rotation verify itself against a value that was never live.
        result, work = self.run_script(
            "SOTTO_DOMAIN=example.test\n"
            "SOTTO_ORGANISATION_DELETION_METRICS_TOKEN=first\n"
            "SOTTO_ORGANISATION_DELETION_METRICS_TOKEN=last\n"
            "SOTTO_ORGANISATION_DELETION_OPERATOR_TOKEN=o\n"
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("appears 2 times", result.stderr)
        backups = list(work.glob(".env.before-rotation-*"))
        self.assertEqual(backups, [], "a refused run leaves no spare copy of the secrets")

    def test_an_unreachable_server_fails_rather_than_reporting_success(self):
        result, work = self.run_script(HEALTHY_ENV, STUB_UNREACHABLE="1")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("  ok ", result.stdout)
        self.assertTrue(list(work.glob(".env.before-rotation-*")), "the rollback file survives")

    def test_it_leaves_no_temporary_holding_a_token(self):
        # Both kinds hold one: the header being sent, and the half-written .env on its way in.
        for environ in ({}, {"STUB_UNREACHABLE": "1"}):
            with self.subTest(environ=environ):
                _, work = self.run_script(HEALTHY_ENV, **environ)
                strays = list(work.glob(".sotto-rotate-hdr.*")) + list(work.glob(".env.rotating.*"))
                self.assertEqual(strays, [])

    def test_it_will_not_guess_where_the_deployment_is(self):
        result, _ = self.run_script(
            "SOTTO_ORGANISATION_DELETION_METRICS_TOKEN=m\n"
            "SOTTO_ORGANISATION_DELETION_OPERATOR_TOKEN=o\n"
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("cannot tell where this deployment is served", result.stderr)


if __name__ == "__main__":
    unittest.main()
