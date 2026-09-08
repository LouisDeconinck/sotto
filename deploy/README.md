# Deploying Sotto

One command brings up a complete hosted instance: Postgres, the sync server, and Caddy serving
the web app with automatic HTTPS. By default it pulls prebuilt multi-arch (amd64 + arm64) images
from GHCR, so the host never compiles anything - a 1 GB free-tier VM with Docker and ports 80/443
open is enough.

```text
internet ──▶ caddy (80/443, web app + API reverse proxy)
                 │ internal network only
                 ├──▶ server (axum, ciphertext-only API)
                 └──▶ ─┘ postgres (named volume)
```

The web app and API share **one origin** (`https://<SOTTO_DOMAIN>`), so the session cookie and
CSP stay same-origin and no CORS is involved. The server stores only ciphertext plus minimal
metadata - see [THREAT-MODEL.md](../THREAT-MODEL.md) - so the box hosts nothing that can decrypt
your secrets; still, treat it as production infrastructure.

## Prerequisites

1. **A host** with Docker + Docker Compose, ports 80 and 443 reachable from the internet.
2. **DNS**: an A (and/or AAAA) record for your domain pointing at the host. Caddy provisions the
   TLS certificate automatically once the name resolves.
3. **A GitHub OAuth app** (github.com → Settings → Developer settings → OAuth Apps → New) with
   the authorization callback URL set to exactly:

   ```text
   https://<your-domain>/auth/github/callback
   ```

## First deployment

```sh
git clone https://github.com/getsotto/sotto.git && cd sotto/deploy
cp .env.example .env
$EDITOR .env        # domain, a generated Postgres password, OAuth client id + secret

docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
```

Database migrations run automatically on server boot. Pin a released version with
`SOTTO_IMAGE_TAG=vX.Y.Z` in `.env` (default: `latest`). To build everything from source instead -
for unreleased changes, or if you'd rather not trust prebuilt images - use
`up -d --build`; that needs ~4 GB of RAM and takes several minutes the first time.

Organisation deletion ships disabled. `SOTTO_ORGANISATION_DELETION_WORKER_ENABLED=1` turns on both
halves of the server side at once - the lifecycle worker and the owner-facing deletion routes - and
`VITE_ORGANISATION_DELETION_ENABLED=true` turns on the client control. The default prebuilt images
and source builds keep every side unavailable. Enabling it is a deliberate procedure with
prerequisites, not a single switch: follow
[Enabling organisation deletion](#enabling-organisation-deletion) below.

New deletion requests use a 30-day recovery window by default. Set
`SOTTO_ORGANISATION_DELETION_RETENTION_DAYS` to an integer from 1 to 365 in `deploy/.env` to change
the window for new requests. The organisation stays frozen for the whole configured window.
Changing it never shortens an existing operation's stored `purge_after` deadline.
Choose a value that covers the managed-backup and export lifecycle: backups taken before purge can
retain ciphertext after the recovery window, and self-hosted operators must remove unmanaged copies.

On a 1 GB host, give the kernel some headroom before the first start:

```sh
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

Smoke test:

```sh
curl -fsS https://<your-domain>/health          # → ok    the process is up
curl -fsS https://<your-domain>/health/ready    # → ok    and it can reach Postgres
```

The second is the one that catches a first deployment wired to the wrong database: the process
starts and serves `/health` regardless, so only the readiness probe fails when `DATABASE_URL` is
wrong. See [Uptime monitoring](#uptime-monitoring).

Then open `https://<your-domain>` in a browser and sign in with GitHub. Point the CLI at your
instance with `sotto login --server https://<your-domain>`.

## Upgrading

```sh
git pull    # picks up compose/runbook changes
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
```

Migrations are forward-only and applied on boot. Check the release notes for anything flagged as
a compatibility break before upgrading past a minor version.

**Renamed settings are the one thing `git pull` cannot fix for you.** Your `.env` is deliberately
untracked, so a variable renamed upstream leaves the old name sitting in your file where nothing
reads it, and the new name unset. Compose substitutes an empty value, the server treats the setting
as absent, and the feature ships dark rather than failing loudly.

Running containers keep their environment until `up -d` recreates them, so the moment to check is
after `git pull` has refreshed `.env.example` and before `up -d`. In that window, compare the
setting names in your `.env` against the fresh template (only names reach the temporary files,
never values):

```sh
sed -n 's/^\([A-Z_][A-Z_0-9]*\)=.*/\1/p' .env | sort > /tmp/env.mine
sed -n 's/^\([A-Z_][A-Z_0-9]*\)=.*/\1/p' .env.example | sort > /tmp/env.upstream
diff /tmp/env.mine /tmp/env.upstream; rm -f /tmp/env.mine /tmp/env.upstream
```

Lines marked `<` are settings nothing reads any more, which usually means a rename to apply to your
`.env`; lines marked `>` are ones you have not set, most of which are optional. The known rename:
`STRIPE_SECRET_KEY` became `STRIPE_API_KEY`. Upgrading across it with the old name in place leaves
billing unconfigured, so `POST /billing/webhook` answers `503` instead of `401` and providers
eventually disable an endpoint that keeps failing. Apply the rename before `up -d`, not after.

## Backups

Postgres holds only ciphertext and metadata, but losing it loses your users' synced vaults.
[`backup.sh`](./backup.sh) takes a custom-format `pg_dump` inside the container, **verifies the
archive** (`pg_restore --list`) before anything leaves the box, and uploads it to whatever
object storage `SOTTO_BACKUP_BUCKET` names - the scheme picks the tool:

| `SOTTO_BACKUP_BUCKET` | Uploads with | Works for |
|---|---|---|
| `gs://<bucket>` | `gsutil` | Google Cloud Storage |
| `s3://<bucket>` | `aws s3 cp` | S3 and S3-compatibles |
| `<remote>:<path>` | `rclone` | 40+ backends: B2, SFTP, a NAS, … |

One-time setup, any provider:

1. **A bucket that deletes objects after ~30 days.**

   ```sh
   # Google Cloud:
   gcloud storage buckets create gs://<bucket> --location=<region>
   printf '{"rule":[{"action":{"type":"Delete"},"condition":{"age":30}}]}' > /tmp/lifecycle.json
   gcloud storage buckets update gs://<bucket> --lifecycle-file=/tmp/lifecycle.json

   # AWS:
   aws s3 mb s3://<bucket> --region <region>
   aws s3api put-bucket-lifecycle-configuration --bucket <bucket> --lifecycle-configuration \
     '{"Rules":[{"ID":"expire","Status":"Enabled","Filter":{},"Expiration":{"Days":30}}]}'
   ```

2. **Append-only credentials for the host.** The box should be able to add backups but never
   read or delete them - a compromised host then can't destroy or exfiltrate your history. Two
   subtleties make this stricter than it looks on GCS:

   - `roles/storage.objectCreator` alone cannot even upload: `gsutil cp` checks whether the
     destination is a "directory" first, and that check is a list operation. Pair it with
     `roles/storage.legacyBucketReader`, which adds exactly `storage.objects.list` and
     `storage.buckets.get` - object names and bucket metadata, nothing that reads contents. (`gcloud storage cp` does not help here - it stats the destination
     object before writing, which needs `storage.objects.get`, the very permission to withhold.)
   - On a bucket with fine-grained ACLs, the uploader is granted owner on every object it
     creates and can read its own uploads back regardless of IAM. Enable uniform bucket-level
     access so IAM alone decides.

   ```sh
   # Google Cloud:
   gcloud storage buckets update gs://<bucket> --uniform-bucket-level-access
   gcloud storage buckets add-iam-policy-binding gs://<bucket> \
     --member="serviceAccount:<vm-service-account>" --role="roles/storage.objectCreator"
   gcloud storage buckets add-iam-policy-binding gs://<bucket> \
     --member="serviceAccount:<vm-service-account>" --role="roles/storage.legacyBucketReader"
   ```

   The host can then list backup names but read none of them; the posture check at the end of
   this section proves it once everything is configured.

   On AWS, an IAM policy allowing only `s3:PutObject` on the bucket serves the same purpose.
   Restore rehearsals fetch the dump with your own credentials on another machine, never on the
   host - by design, the host can no longer read what it wrote.

3. **Point the script at it** (`deploy/.env`): `SOTTO_BACKUP_BUCKET=gs://<bucket>` (or the
   `s3://` / rclone form).

4. **Nightly cron**, with failures landing in a log you can check:

   ```sh
   crontab -e     # add:
   # 17 2 * * * cd $HOME/sotto/deploy && ./backup.sh >> $HOME/sotto-backup.log 2>&1
   ```

5. **Watch that it keeps running.** Cron reports to nobody, and a bucket that has stopped
   receiving objects looks exactly like a bucket nobody has checked. Installing step 4 and
   assuming it worked is how an instance ends up with months of no backups and no symptom.

   The check is one question, and it is the same on every object store: **is the newest object
   younger than 26 hours?** That threshold exceeds a full day on purpose, so a single missed night
   breaches it rather than hiding inside it. Run it anywhere except the host, because a host that
   has stopped taking backups cannot be relied on to report that it has. Have it ping a heartbeat
   URL when it passes, so any external checker (e.g. a free UptimeRobot heartbeat monitor) alerts
   you when the pings stop; alerting only on failure cannot report the checker's own death, which
   is the failure that hides longest.

   [`backup-freshness.yml`](../.github/workflows/backup-freshness.yml) is that check as a worked
   example, for `gs://` destinations, running daily on GitHub Actions. It lists object names and
   creation times, never fetches a backup, and the identity it uses holds no permission that would
   let it, so the append-only posture above is unaffected.

   Configure it with repository variables `SOTTO_BACKUP_BUCKET` (the same value as in `.env`,
   scheme included), `GCP_WORKLOAD_IDENTITY_PROVIDER` and `GCP_MONITOR_SERVICE_ACCOUNT`, plus a
   repository secret `BACKUP_HEARTBEAT_URL`. Until all three variables are set the job skips
   rather than failing.

   Grant the identity by federation rather than by issuing a key, so that no long-lived credential
   exists to leak or rotate:

   ```sh
   # Google Cloud:
   # Federating the identity is not enough on its own. Exchanging the OIDC token for one the
   # service account can use goes through this API, so a project that has never enabled it fails
   # at that last hop only, long after the pool and the binding both look correct.
   gcloud services enable iamcredentials.googleapis.com

   gcloud iam workload-identity-pools create github --location=global
   gcloud iam workload-identity-pools providers create-oidc github \
     --location=global --workload-identity-pool=github \
     --issuer-uri="https://token.actions.githubusercontent.com" \
     --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
     --attribute-condition="assertion.repository=='<owner>/<repo>'"

   gcloud iam service-accounts create sotto-backup-monitor
   gcloud storage buckets add-iam-policy-binding gs://<bucket> \
     --member="serviceAccount:sotto-backup-monitor@<project>.iam.gserviceaccount.com" \
     --role="roles/storage.legacyBucketReader"
   gcloud iam service-accounts add-iam-policy-binding \
     sotto-backup-monitor@<project>.iam.gserviceaccount.com \
     --role="roles/iam.workloadIdentityUser" \
     --member="principalSet://iam.googleapis.com/projects/<project-number>/locations/global/workloadIdentityPools/github/attribute.repository/<owner>/<repo>"
   ```

   **The attribute condition is the security boundary, not a filter.** Without it the provider
   trusts every GitHub Actions token in existence, so any repository anywhere could assume this
   identity. `legacyBucketReader` is deliberate too: it grants `storage.objects.list` and
   `storage.buckets.get` and nothing that reads an object, which is all a freshness check needs.

   **Any other store.** The workflow skips cleanly unless the destination is `gs://`, so nothing
   is ever half configured without saying so. Because `rclone` reaches every backend `backup.sh`
   can write to, one command answers the question for all of them, from any scheduler that can
   send a heartbeat afterwards:

   ```sh
   # Anything rclone reaches - gs://, s3://, B2, SFTP, a NAS:
   rclone lsjson --max-age 26h "$SOTTO_BACKUP_BUCKET" | jq -e 'length > 0'

   # AWS, without rclone:
   aws s3api list-objects-v2 --bucket <bucket> \
     --query 'max_by(Contents, &LastModified).LastModified'
   ```

   A non-zero exit is the alarm. On AWS the federated equivalent of the grant above is registering
   GitHub's OIDC issuer as an identity provider and giving the role a trust policy conditioned on
   the repository, with `s3:ListBucket` and nothing else.

**Restore** (into a running instance; drops and recreates objects from the dump). Fetch the
dump on your own machine, never the host - the append-only posture means the host cannot read
what it wrote. One consequence of uniform bucket-level access is easy to miss: it disables the
object ACLs that basic project roles relied on, so until you grant yourself read explicitly,
nobody at all can fetch a backup - project owner included. Grant it once:

```sh
# Google Cloud:
gcloud storage buckets add-iam-policy-binding gs://<bucket> \
  --member="user:<your-account>" --role="roles/storage.objectViewer"
```

Then fetch with your provider's tool (`gsutil cp` / `aws s3 cp` / `rclone copyto`) and restore:

```sh
docker compose -f docker-compose.prod.yml exec -T postgres \
  pg_restore -U sotto -d sotto --clean --if-exists < sotto-<stamp>.dump
docker compose -f docker-compose.prod.yml restart server
```

Run one backup by hand now and verify all three properties of the append-only posture from the
host - the upload must succeed and both refusals must appear, because an unverified posture and a
working one look identical from the outside:

```sh
./backup.sh                                      # upload succeeds
gsutil cat gs://<bucket>/sotto-<stamp>.dump      # AccessDenied: needs storage.objects.get
gsutil rm gs://<bucket>/sotto-<stamp>.dump       # AccessDenied: needs storage.objects.delete
```

Then rehearse the restore once against a scratch database - a backup that has never been restored
is a hope, not a backup.

## Access logs

Caddy writes JSON access logs to the `caddy_logs` volume (`/var/log/caddy/access.log` in the
container), rotated at 50 MiB, 10 files kept, 90 days retained (the `log` block in the
`Caddyfile`). Credential headers (`Cookie`, `Authorization`, `Set-Cookie`) are **deleted from
every entry by an explicit filter in the `Caddyfile`** - not left to Caddy's default redaction
- so no session material ever reaches disk. Request paths and statuses are logged.

The number that matters for a hosted instance - free-tier limit hits (HTTP 402, one per person
who wanted more than the free tier allows):

```sh
docker compose -f docker-compose.prod.yml exec caddy \
  sh -c 'grep -c "\"status\":402" /var/log/caddy/access.log'
```

## Uptime monitoring

Point the monitor at **`GET /health/ready`**, not at `/health`. Both are unauthenticated and both
return `ok` with a `200`, and the difference only shows up on the day it matters:

| Path            | Answers                                   | Reports a database outage |
| --------------- | ----------------------------------------- | ------------------------- |
| `/health`       | is this process running?                  | no                        |
| `/health/ready` | can this instance serve a request?        | yes, `503 unavailable`    |

Postgres holds every secret, session and project, so an outage there fails every real request.
`/health` never touches it, and a checker watching that path stays green for the whole outage. The
readiness probe makes an explicit round trip to the database and returns `503` with the body
`unavailable` when it cannot.

Configure any external checker against `https://<SOTTO_DOMAIN>/health/ready` (e.g. a free
UptimeRobot monitor, 5-minute interval, keyword `ok`). Alerting from *outside* the box is the
point: a dead VM cannot report itself.

Two properties are worth knowing before tuning the interval. The verdict is cached for a second
and concurrent checks share one query, so probing more often costs the database nothing extra but
also tells you nothing extra. And the check gives up after five seconds, so a database that hangs
rather than refuses is reported as unavailable rather than holding the request open.

`/health` is still there and still worth a second monitor if you want to tell "the box is gone"
apart from "the box is up and the database is not". Keep it pointed at `/health` for that
distinction to mean anything.

## Restore verification

A backup nobody has restored is a hope. `backup.sh` validates each archive with
`pg_restore --list` before upload, which proves the file is not truncated and nothing more. It
cannot tell you the bytes survived the trip to the bucket, and it cannot tell you that what
comes back is a database this code could run on. Only restoring one answers those.

`.github/workflows/backup-restore.yml` does it monthly: fetch the newest object, restore it into
a throwaway Postgres that dies with the runner, and check what came back. Never onto the host,
which by design cannot read what it wrote.

What it asserts, which is the rehearsal of 2026-08-31 written down:

- `pg_restore` completes with no errors;
- every migration the dump recorded is marked successful, and none is a version this checkout
  does not carry. A deployment **behind** the branch passes: production is often a release or
  two back, and failing every month in between would train everyone to ignore the job. A
  deployment **ahead** fails, because a dump carrying schema this code does not know is not one
  this code can be restored onto;
- the organisation-deletion tables are present, named because deletion is the one operation
  Sotto cannot undo from inside the product, so this backup is the only thing behind it;
- the tables that must never be empty are not. A dump of the wrong database, or one taken after
  a truncation, passes every structural check ever written and fails this one.

Nothing the job prints is data. This repository is public, so its workflow logs are public, and
the dump is production. Counts, table names and migration versions are safe to say out loud;
`pg_restore`'s error log is withheld even on failure, because it can quote the SQL it choked on.

### Configuration

Restoring needs to **read** objects, which the daily freshness check deliberately cannot do, so
it uses its own identity rather than widening that one:

```sh
gcloud iam service-accounts create sotto-backup-restorer \
  --display-name "Reads backups for monthly restore verification"

gcloud storage buckets add-iam-policy-binding gs://sotto-backups-prod \
  --member "serviceAccount:sotto-backup-restorer@<project>.iam.gserviceaccount.com" \
  --role roles/storage.objectViewer

# Bound to a GitHub environment, not to the whole repository. This is the only identity in the
# project that can read a backup, so a workflow that has not declared `environment:
# backup-restore` cannot assume it, and the environment can additionally require approval.
gcloud iam service-accounts add-iam-policy-binding \
  sotto-backup-restorer@<project>.iam.gserviceaccount.com \
  --role roles/iam.workloadIdentityUser \
  --member "principal://iam.googleapis.com/projects/<number>/locations/global/workloadIdentityPools/github/subject/repo:<owner>/<repo>:environment:backup-restore"
```

Create the `backup-restore` environment in the repository settings, then set the variable
`GCP_RESTORE_SERVICE_ACCOUNT` and, optionally, the secret `RESTORE_HEARTBEAT_URL`. The job
reuses `SOTTO_BACKUP_BUCKET` and `GCP_WORKLOAD_IDENTITY_PROVIDER` from the freshness check.

Note that the provider's attribute condition remains the security boundary: without it, the
provider trusts every GitHub Actions token in existence.

### Doing it by hand, on any object store

The workflow implements `gs://` because that is what the hosted deployment uses. The drill is
the same everywhere and is worth running by hand once, whatever you store backups in:

```sh
# Fetch the newest dump. Use whatever your store speaks; rclone speaks most of them.
rclone copy "$SOTTO_BACKUP_BUCKET/$(rclone lsf "$SOTTO_BACKUP_BUCKET" | sort | tail -1)" .

# Restore into a scratch database, never the live one.
createdb restored
pg_restore -d restored --no-owner --no-privileges <dump>

# Check what came back.
scripts/check-restore --database-url postgres://localhost/restored
```

Record the result in `deploy/rehearsals/` the way the existing entries do. A drill nobody writes
down is one nobody can prove happened, which is how the nightly backup went uninstalled for two
months with the runbook describing it the whole time.

## Status history

A status page needs history, and history cannot be backfilled: every day nothing samples the
deployment is a permanent gap in the record. So the collector starts before the page exists.

`.github/workflows/status-collector.yml` samples the public surface roughly every ten minutes
and appends what it saw to an orphan `status-history` branch, which shares no history with
`main` and so never appears in a source diff. Two files accumulate there:

- `summary.json`, the current state of each component and a per-day tally over ninety days,
  which is what a page renders;
- `samples/<date>.jsonl`, one line per observation, so the tallies can be recomputed if the
  aggregation ever turns out to be wrong. A published uptime figure nobody can recheck is a
  figure nobody should have to take on trust.

Both age out at the same ninety days, which is the point: the audit trail covers exactly the
window the summary publishes, and nothing is kept that no longer backs a number anyone can see.
If you want a longer record than you publish, the sample files are plain JSONL and copying them
somewhere else before they age out is the whole of what that takes.

Set the repository variable `SOTTO_PUBLIC_URL` to the deployment to watch. Without it the job
skips rather than probing a default, so a fork cannot point it at somebody else's deployment.

**In a fork, one line has to change as well.** The job carries
`if: github.repository == 'getsotto/sotto'`, so a fork of this repository collects nothing and
does so silently, which is a poor way to find out. That guard exists because a fork inherits
both the schedule and the write permission, and neither probing another project's deployment
nor pushing history into its own branch is something a fork should start doing by being made.
Change the name to your own repository, or drop the line if you are happy for every fork of
your fork to sample too. The alternative is to skip the workflow entirely and run the script
from your own scheduler, below.
Set the secret `STATUS_HEARTBEAT_URL` too, from any external checker: the collector pings it
after each round of samples is pushed, and the checker alerting when those pings stop is the
only thing that can notice this job dying or running green while sampling nothing. Allow that
monitor a generous grace period, hours rather than minutes, because GitHub queues scheduled
workflows rather than guaranteeing them and a run arriving late is not a run that failed.

Two things to get right before setting the variable, because both write a wrong answer into a
record that is meant to be permanent:

- **Point it at the origin the deployment actually serves**, with the scheme it serves on. No
  probe follows redirects, so a `www` host or an `http` URL that the deployment folds onto its
  canonical origin would otherwise read as every component being down at once. A round where
  every probe was redirected is treated as a wrong setting rather than an outage: nothing is
  written and the run fails, which withholds the heartbeat and makes the mistake noticeable
  instead of accruing invented downtime. A deployment that is genuinely gone refuses
  connections rather than redirecting them, so a real outage is still recorded. A single
  redirected component among working ones is recorded too, since only unanimity is
  unambiguous.
- **Wait until the deployment serves `/health/ready`**, which means version 0.7.0 or later.
  Before that the path falls through to the single-page app, and the API row records real
  downtime for a deployment that is working.

Every probe is unauthenticated and asks only what a visitor could ask:

| Component   | Probe                                          | Healthy answer                   |
| ----------- | ---------------------------------------------- | -------------------------------- |
| API         | `GET /health/ready`                            | `200` with the body `ok`         |
| Web app     | `GET /`                                        | `200` and an HTML content type   |
| Sign in     | `GET /auth/github/login` with a loopback callback | a redirect to `github.com`    |
| Billing     | `POST /billing/webhook` with no signature      | `401`                            |
| Secret sync | not yet probed                                 | -                                |

Two of those distinguish "not configured" from "broken", because they are not the same thing
and only one of them belongs in an uptime figure. A `503` from sign-in or billing means the
deployment has no OAuth or no Stripe credentials, which is a choice; it is recorded as
unconfigured and left out of the tally, so a self-hoster running neither does not watch their
published uptime fall for features they decided not to run. A `503` from `/health/ready` is
the opposite: it has exactly one cause, an unreachable database, and it counts as downtime.

The billing probe deliberately sends an unsigned payload and requires a `401`. A `200` there
would mean signature verification is not happening, so that case is recorded as down rather
than as a passing request.

Secret sync is listed but not measured. It needs a throwaway organisation holding junk secrets
and a machine token to read them, and neither exists yet; shipping a probe that has never run
would repeat the mistake this whole effort was built to catch. It appears as a row so a page
can say plainly that it is not being watched, rather than implying by omission that everything
is covered.

The job records and never alerts. A component being down leaves the workflow green, because
paging belongs to an external monitor that survives this repository being unreachable, and a
workflow that went red on every blip would train everyone to ignore the failure that matters
here, which is the collector itself dying.

The sign-in probe is the one that writes: starting the OAuth flow records a short-lived login
row, which the same endpoint clears on its next call. That is deliberate, since it exercises
the write path rather than only a read, but it is worth knowing that this probe is not purely
an observer.

### Running it somewhere other than GitHub Actions

Nothing about the check needs GitHub. `scripts/status-probe --base-url <url> --data-dir <dir>`
is Python 3 with no dependencies beyond the standard library, and the data directory is a
directory of files. Run it from cron, a systemd timer, or any other scheduler:

```sh
*/10 * * * * /path/to/scripts/status-probe --base-url https://example.com --data-dir /var/lib/sotto-status
```

Serve or sync that directory however suits you: a static host, an object store, a commit to
any git host. The workflow adds three things and no more, so anything that does them is
equivalent: it runs the script on a schedule, keeps the output somewhere durable, and pings a
heartbeat afterwards so the check being dead is noticeable.

The verdict logic is covered by `scripts/tests/test_status_probe.py`.

One caveat if you keep the history in git, as the bundled workflow does: the samples age out
with the summary, but the commits do not. At this interval that is roughly fifty thousand
commits a year on a branch nothing else reads. Deleting the branch is a safe reset if it ever
becomes awkward, since the next run recreates it, at the cost of the history it held.

## Organisation-deletion metrics

The deletion worker stores aggregate lifecycle counters in Postgres. Their fixed vocabulary, alert
conditions, and the protected Prometheus scrape are documented in
[DELETION-METRICS.md](DELETION-METRICS.md). Set `SOTTO_ORGANISATION_DELETION_METRICS_TOKEN` only
when the monitoring system is ready to send the bearer token securely.

The operator observation endpoint is separately protected by
`SOTTO_ORGANISATION_DELETION_OPERATOR_TOKEN`. Leave it blank until the deletion runbook has been
rehearsed and the authenticated observation procedure is ready. Never reuse the metrics token for
this write-capable operational control.

Both endpoints are independent of the deletion flags: configuring either token does not enable
deletion, and neither is enabled by turning deletion on.

## Enabling organisation deletion

Deletion is irreversible once purge begins, so treat enablement as a release of its own. Work
through it on a staging deployment first, then repeat it on production with the same pinned image
tag. The full operator procedure, including the rehearsal record you must complete, is in
[ORGANISATION-DELETION-RUNBOOK.md](ORGANISATION-DELETION-RUNBOOK.md).

**Prerequisites** - all of these before either flag changes:

1. A managed backup or export lifecycle covering the configured recovery window, with a restore
   into an isolated scratch database rehearsed and recorded.
2. `SOTTO_ORGANISATION_DELETION_METRICS_TOKEN` set from the deployment secret store, the
   [alert rules](ORGANISATION-DELETION-ALERTS.yml) loaded, and one notification tested.
3. `SOTTO_ORGANISATION_DELETION_OPERATOR_TOKEN` set from the deployment secret store, with the
   authenticated observation procedure reviewed and rehearsed.
4. Billing configured and verified end to end: the provider's API version, restricted key, and
   webhook endpoint matching [Billing](#billing-optional).
5. `SOTTO_IMAGE_TAG` pinned to a released version, so the server and web images cannot skew.

**Enablement**, on staging first:

```sh
# in deploy/.env
SOTTO_ORGANISATION_DELETION_WORKER_ENABLED=1
```

```sh
docker compose -f docker-compose.prod.yml up -d
```

That enables the whole server side, the deletion routes and the lifecycle worker together, and it
works with prebuilt images. Everything in the verification list below goes through the API, so the
server side can be enabled and verified on its own.

The client control is a separate step with a trap in it. `VITE_ORGANISATION_DELETION_ENABLED` is
compiled into the web bundle when the image is built, and the published images are built with it
`false`, so setting it in `deploy/.env` changes nothing while you pull prebuilt images. To show the
control in the web app, set it in `deploy/.env` and rebuild the web image from source, which routes
the value through the compose build argument:

```sh
docker compose -f docker-compose.prod.yml up -d --build caddy
```

or publish your own web image with that build argument set and pin the deployment to it. A source
build needs the RAM headroom noted under [First deployment](#first-deployment).

**Verify**, before repeating any of this on production:

- `https://<SOTTO_DOMAIN>/health/ready` returns `ok`, which is the check to make here rather than
  `/health`: everything below this line is stored in Postgres, and `/health` answers the same
  whether the deployment can reach it or not (see [Uptime monitoring](#uptime-monitoring));
- the protected metrics endpoint answers `200` with its token and `401` with a missing or wrong
  one; a `503` instead means its token is not configured, so prerequisite 2 is unmet;
- the operator observation endpoint answers `401` for a missing token, for a wrong one, and for
  the metrics token, which it must never accept; a `503` instead means prerequisite 3 is unmet;
- an owner on a disposable test organisation can complete the confirmation flow, see the recovery
  window, and cancel it again;
- the audit trail and server logs show the request, cancellation, and operator observation, with
  no bearer token or provider text in them.

**Turning it back off** stops new requests and idles the worker, but does not restore an
organisation whose purge has already begun. Existing operations keep their stored `purge_after`
deadline; the routes return `404` again and the worker stops advancing the queue, leaving frozen
organisations frozen until deletion is re-enabled or an operator recovers them through the runbook.

## Database security

The default `docker-compose.prod.yml` keeps Postgres on the **internal compose network only** - it
is never published to a port, so the server↔database link never leaves the host and the plaintext
connection (`DATABASE_URL` carries no `sslmode`) is not exposed. That is the recommended topology.

If you instead point `DATABASE_URL` at a **remote or managed Postgres**, the link now crosses a
network, so encrypt it. The server binary is built with system TLS (native-tls), so it is enough to
ask for it in the connection string:

```sh
# require encryption:
DATABASE_URL=postgres://user:pass@db.example.com:5432/sotto?sslmode=require
# or verify the server certificate against a CA (strongest):
DATABASE_URL=postgres://user:pass@db.example.com:5432/sotto?sslmode=verify-full&sslrootcert=/path/to/ca.pem
```

Even without TLS the database only ever holds ciphertext and the key-wrapping graph - secret names
and values are encrypted client-side and are never decryptable server-side (see
[THREAT-MODEL.md](../THREAT-MODEL.md)). TLS to the database protects the **metadata** (emails, the
sharing graph, timestamps) in transit, and is a hard requirement for any deployment where that link
is not a trusted private network.

## Rate limiting & perimeter

Abuse control lives at the edge, where the real client IP is visible. The deploy Caddy image is an
[xcaddy](https://github.com/caddyserver/xcaddy) build bundling the
[caddy-ratelimit](https://github.com/mholt/caddy-ratelimit) plugin (pinned in
`deploy/Dockerfile.web`), and the `Caddyfile` applies a per-client-IP limit to the **unauthenticated**
endpoints - the OAuth login/callback and the public share fetch, the only API surface with no
credential wall. Authenticated sync is intentionally left unthrottled at the edge: it is bearer-gated
and includes high-frequency CI polling that a per-IP cap could wrongly block when a whole team shares
one office/NAT egress IP. Tune the threshold (or split it into per-endpoint zones) in the `Caddyfile`.

Two honest limits, consistent with the [threat model](../THREAT-MODEL.md) (availability is an
accepted residual risk, and self-hosting is the escape hatch):

- **Per-IP, not global.** A distributed flood from many source addresses is not stopped by this;
  put a CDN/WAF in front if you need volumetric protection.
- **This lives in *this* Caddy.** If you front the server with your own proxy, or expose
  `sotto-server` directly, the server does **not** self-throttle - supply equivalent rate limiting
  at your own edge.

## Billing (optional)

The server ships with Stripe billing dark: without the `STRIPE_*` variables, billing endpoints
return 503 and orgs are tiered manually. To turn it on:

1. In the Stripe dashboard: create a Product with one monthly Price (the flat per-org Team
   subscription) and note the `price_…` id.
2. Add a webhook endpoint for `https://<SOTTO_DOMAIN>/billing/webhook`, set its API version to
   `2026-07-29.dahlia`, and subscribe it to `checkout.session.completed`,
   `customer.subscription.updated`, and `customer.subscription.deleted`; note its `whsec_…`
   signing secret. The endpoint version must match the server's pinned Stripe version.
3. Fill `STRIPE_API_KEY`, `STRIPE_WEBHOOK_SECRET`, and `STRIPE_PRICE_ID` in `.env`, then
   `docker compose -f docker-compose.prod.yml up -d --force-recreate server`.

Card data never touches the server - checkout and subscription management happen on
Stripe-hosted pages, and the webhook only assigns the org's tier.

## Operations

```sh
docker compose -f docker-compose.prod.yml logs -f server   # API logs
docker compose -f docker-compose.prod.yml logs -f caddy    # access/TLS logs
docker compose -f docker-compose.prod.yml ps               # health at a glance
```

- Postgres is **not** exposed outside the compose network; only Caddy publishes ports.
- Certificates and Caddy state persist in the `caddy_data` volume; database data in `pgdata`.
- The API route list lives in the repo-root [`Caddyfile`](../Caddyfile) (baked into the web
  image at build time) - pulling the matching image version picks up route changes automatically.
- To try it without a public domain, set `SOTTO_DOMAIN=localhost`: Caddy serves a self-signed
  certificate (`curl -k https://localhost/health`). GitHub login still requires a callback URL
  reachable by your browser.
- Organisation-deletion incidents follow the
  [`ORGANISATION-DELETION-RUNBOOK.md`](ORGANISATION-DELETION-RUNBOOK.md). It forbids direct SQL
  lifecycle changes and requires an isolated restore rehearsal before enablement.
