# Runbook — on-call operations

> **Audience:** on-call engineers. **Goal:** diagnose and recover
> the production deployment in under 10 minutes. If a step here
> doesn't work, escalate — do not improvise.

All commands assume you are on the production host with the repo
checked out at `~/citevyn/`. Adjust paths if your layout
differs.

---

## 0. TL;DR

```bash
# Health
curl -fsS https://citevyn.example.com/health || echo DEGRADED

# Logs
make logs

# Restart one service
docker compose -f infra/docker/docker-compose.yml --profile prod restart api

# Roll back a release
make rollback TAG=--previous

# Verify a release (deploy + functional verify + rollback drill)
VERSION=v0.10.0 PREV_VERSION=v0.9.0 make deploy-verify
```

---

## 1. Service map

| Container           | Role               | Port (host) | Restart policy  |
|---------------------|--------------------|-------------|-----------------|
| `citevyn-caddy`     | TLS / reverse proxy| 80, 443     | `unless-stopped`|
| `citevyn-api`       | FastAPI app        | (internal)  | `unless-stopped`|
| `citevyn-worker`    | Ingestion worker   | (none)      | `unless-stopped`|
| `citevyn-db`        | Postgres + pgvector| (none)      | `unless-stopped`|
| `citevyn-redis`     | Cache / rate-limit | (none)      | `unless-stopped`|

The api and worker share the same image family but different
CMDs; the api listens on the docker network, the worker is
headless. Caddy is the only public entry point.

---

## 2. Health checks

### 2.1 First-line: /health

```bash
curl -fsS https://citevyn.example.com/health
```

Expected: `{"status":"ok"}` with HTTP 200. **No database call** —
this is a pure liveness probe. If this fails, the api process is
down or Caddy is down.

### 2.2 Second-line: container state

```bash
docker compose -f infra/docker/docker-compose.yml --profile prod ps
```

All five services should be `running` (the `backup` profile is
not running by design). Look for `(health: starting)` or
`(unhealthy)` flags.

### 2.3 Third-line: per-service logs

```bash
docker compose -f infra/docker/docker-compose.yml --profile prod logs --tail=200 api
docker compose -f infra/docker/docker-compose.yml --profile prod logs --tail=200 worker
docker compose -f infra/docker/docker-compose.yml --profile prod logs --tail=200 caddy
```

Look for: stack traces, repeated 500s, repeated 502s (Caddy
upstream timeouts).

---

## 3. Common scenarios

### 3.1 /health returns 502 from Caddy

**Symptom:** `curl` returns 502; Caddy logs say
`dial tcp: lookup api on …: no such host`.

**Diagnosis:** The api container is down or stuck restarting.

```bash
docker compose -f infra/docker/docker-compose.yml --profile prod ps api
docker compose -f infra/docker/docker-compose.yml --profile prod logs --tail=200 api
```

**Fix:**

```bash
docker compose -f infra/docker/docker-compose.yml --profile prod restart api
```

If the container is crash-looping, check the bottom of the log
for the traceback. Common causes:
- Missing env var (`CITEVYN_ADMIN_API_KEY`, `CITEVYN_DATABASE_URL`).
- Postgres not yet healthy (`depends_on: db: service_healthy`
  usually catches this; if it doesn't, see §3.2).

### 3.2 Postgres won't start

**Symptom:** `docker compose ps db` shows `(unhealthy)` or
restarting. Logs show `FATAL: could not write to file …` or
`permission denied on /var/lib/postgresql/data`.

**Diagnosis:** Volume permissions. The named volume
`citevyn_pgdata` was created by a previous Postgres image
running as a different uid (often uid 999 in the pgvector base
image, vs. uid 70 inside the `postgres:16-alpine` family).

**Fix:** the volume's first init must be from a
`pgvector/pgvector:pg16` container. Re-create the volume only
as a last resort — it drops the database.

```bash
docker compose -f infra/docker/docker-compose.yml --profile prod stop db
docker compose -f infra/docker/docker-compose.yml --profile prod rm -f db
docker volume ls | grep citevyn_pgdata
# If the volume was initialised by a different image:
docker volume rm citevyn_pgdata
docker compose -f infra/docker/docker-compose.yml --profile prod up -d db
```

**WARNING:** the last `rm` drops the entire database. Only run
if the most recent backup is current. See [§4 Backup & restore](#4-backup--restore).

### 3.3 Rate limit returning 429 unexpectedly

**Symptom:** legitimate users get HTTP 429. Logs show
`X-RateLimit-Remaining: 0` for non-spammy clients.

**Diagnosis:** The sliding-window limit
(`CITEVYN_RATE_LIMIT_DEMO_USER_PER_HOUR` /
`CITEVYN_RATE_LIMIT_ADMIN_PER_HOUR`) is too low for the current
traffic. Or the window clock is wrong (NTP drift).

**Fix:** raise the limit in `infra/docker/.env` and refresh:

```bash
$EDITOR infra/docker/.env      # bump CITEVYN_RATE_LIMIT_DEMO_USER_PER_HOUR
make refresh
```

If the issue is a single user hammering the endpoint, find
them in the api logs and `grep` for their `X-API-Key`:

```bash
docker compose logs api | grep "user_id=alice" | tail -50
```

The 429 response includes `Retry-After` (seconds). Once the
window slides, the user regains quota.

### 3.4 Worker's `ingestion_jobs` queue is stuck

**Symptom:** `GET /v1/admin/jobs` shows jobs in `running` state
for more than 5 minutes. New jobs queue up.

**Diagnosis:** The worker process is alive but the running job
is wedged (e.g. the LLM provider timed out and the worker is
waiting on a stale HTTP connection).

**Fix:** Send the worker a SIGTERM; the signal handler drains
the in-flight job and exits. Compose restarts it automatically.

```bash
docker compose -f infra/docker/docker-compose.yml --profile prod restart worker
```

The wedged job will be marked `failed` on next startup (or by
the next worker's poll if `restart: on-failure` is configured
on the job row).

### 3.4a Embeddings: enabling the real provider (#51)

**Context:** By default `CITEVYN_EMBEDDING_PROVIDER=stub` (deterministic,
non-semantic). Semantic vector retrieval needs the real embedder.

**To enable:** set `CITEVYN_EMBEDDING_PROVIDER=gemini` and provide
`CITEVYN_GEMINI_API_KEY` (the same key as the LLM). The API and the worker
both fail fast at startup if the provider/key/dimension are misconfigured.

**Two operational gotchas:**

1. **Migration `0004` runs `CREATE EXTENSION IF NOT EXISTS vector`.** This needs
   a role with permission to create extensions. The `pgvector/pgvector:pg*`
   image ships it; a managed Postgres (RDS/Cloud SQL/Azure) may require the
   extension to be pre-allowlisted or a superuser-style grant before
   `alembic upgrade head`.
2. **Embeddings are model-specific — you MUST re-ingest after switching
   providers/models. This is not enforced at runtime.** An index built under the
   stub (or a different model) holds vectors in a different space; querying it with
   Gemini returns meaningless results **with no error**. There is currently **no
   runtime guardrail** for a same-dimension model swap — `IndexVersion.embedding_provider/model/dim`
   is recorded but not yet read (see ADR-0003, Tier 3 enforcement, deferred). So the
   discipline is manual: after enabling or changing the embedder, **re-run ingestion
   (`citevyn-worker run`) to rebuild the index, then promote it.** The dimension IS
   guarded — `CITEVYN_EMBEDDING_DIM` must stay 1536 (the `vector(1536)` column) or the
   app refuses to boot; changing it requires a new migration.

**Degraded mode:** if the embedding provider is transiently down, the vector arm
returns no hits (logged as `vector_retrieval_degraded_embedder_unavailable`) and
answers still come from exact-term + keyword retrieval — the request does not fail.

### 3.5 Caddy won't issue the certificate

**Symptom:** `curl https://citevyn.example.com/health` returns
"connection reset" or a 522 from Caddy. Caddy logs say
"acme: error presenting challenge".

**Diagnosis:** The ACME HTTP-01 challenge (port 80) is
unreachable from Let's Encrypt. Common causes:
- DNS for `CITEVYN_PUBLIC_HOST` doesn't resolve to this host.
- A firewall is blocking port 80.
- Another web server (nginx, Apache) is already on port 80.

**Fix:**

```bash
# Verify DNS
dig +short citevyn.example.com

# Verify port 80 is reachable from the internet
nc -z citevyn.example.com 80

# Verify nothing else is on port 80
ss -lntp | grep ':80 '
```

Once DNS and port 80 are clean, force Caddy to re-attempt the
challenge:

```bash
docker compose -f infra/docker/docker-compose.yml --profile prod exec caddy \
    caddy reload --config /etc/caddy/Caddyfile
```

The next request on :443 will trigger a new ACME attempt.

### 3.6 Database volume full

**Symptom:** Postgres logs `No space left on device`. The api
starts returning 500s on the first query.

**Diagnosis:** The host disk is full. The named volume
`citevyn_pgdata` lives in `/var/lib/docker/volumes/` by default.

**Fix:**

```bash
# See docker volume usage
docker system df -v

# If a stale container is keeping a deleted file alive
docker ps -a
# … and remove it:  docker rm <id>

# Long-term: enable log rotation on the host
$EDITOR /etc/docker/daemon.json
# Add:
#   { "log-driver": "json-file", "log-opts": { "max-size": "10m", "max-file": "5" } }
# Then:  sudo systemctl restart docker
```

---

### 3.7 Editing a source doc (corpus correction)

**Context:** The corpus under `backend/app/worker/sources/*.md` is the ground
truth every answer is generated from. Correcting a doc is a normal operation —
a definition is too narrow, a flag is wrong, a section is missing.

**Procedure:** edit the `.md` file, re-run ingestion, then promote:

```bash
python -m app.worker.cli run                          # rebuilds v-local from the edited corpus
python -m app.worker.cli evaluate --index-version v-local   # writes the EvaluationRun the gate reads
# then promote via POST /v1/admin/index_versions/{version}/promote
```

**The promote is gated (#210), and the evaluate step is what satisfies it
(#216).** Promotion requires a completed `EvaluationRun` for the candidate that
measured at least `CITEVYN_INDEX_PROMOTION_MIN_PASS_RATE` (default `0.95`).
`evaluate` measures the candidate against the shipped corpus and persists that
run, so a corrected corpus that still retrieves correctly promotes with **no**
`force`.

If `evaluate` exits `2` the candidate measured below threshold — read
`failure_summary` (`GET /v1/admin/evaluations?index_version=<candidate>`) and fix
the corpus rather than reaching for `force`. `?force=true` still promotes anyway
and records the override — `force`, `measured_pass_rate`, `threshold`,
`evaluation_run_id` — in the `promote_index` audit row; with `force` you are the
gate. Re-promoting the already-active index is a no-op and is never blocked.

**What happens automatically (no manual cache flush needed):**

- `IndexVersion.source_version_hash` is derived from the **bytes of the source
  docs** (`app.worker.cli.content_version_hash`), so any edit changes it. The
  answer-cache key includes that hash, so cached answers built from the old text
  stop being reachable. There is no constant to bump.
- A re-ingest **replaces** the source's chunks and exact terms rather than
  appending, so the old wording does not linger in the corpus next to the new.
- `Document.title` and `source_url` are refreshed from the allowlist, so an
  allowlist correction reaches rendered citations.

All three hold when re-ingesting **in place** (the default `--index-version
v-local`, which is what the worker image's `CMD` runs). Before this was fixed,
each of them silently required building a brand-new index version instead.

**The new fingerprint is published only after a clean, whole-corpus run.** If
any source fails, or you ingested a subset with `--source`, the hash stays where
it was. That is deliberate: publishing a hash the corpus does not yet match
would let a query be answered from the un-rebuilt chunks and then *cached under
the new key*, and because a retry re-hashes the same files the hash never moves
again — the stale answer would survive the correction until the TTL expires.
So after a partial failure, **fix the cause and re-run the full ingest**; the
correction is not live until a run completes cleanly.

**Verify the edit actually shipped:** ask the corrected question and confirm the
answer reflects the new text. If it still shows the old answer, check (a) that
the run reported no failed sources, (b) that the promote step ran — an
un-promoted candidate is not served — and (c) that the promote returned **200
and not 409 `promotion_blocked`**; a refused promote leaves the old index
serving and is easy to miss in a scrollback.

**Where else a correction has to land (#178).** A corpus edit reaches the *live*
index only through the re-ingest + promote above. Three other paths serve corpus
content, and they used to need hand-mirroring:

| Path | How the correction reaches it |
|---|---|
| Live index | `citevyn-worker run` + admin promote (above). Nothing else. |
| Fresh bootstrap (`make demo`, `scripts/smoke.sh`, `deploy.sh`) | Automatic. `db/seed/seed_catalog.py` *is* an ingest of `app/worker/sources/*.md` into `v1` — it no longer carries its own copy. |
| Hermetic test fixture (`tests/conftest.py`) | Manual, but enforced: `backend/tests/test_corpus_single_source.py` fails if the fixture still claims a command the corpus dropped. |
| Frontend offline KB (`frontend/src/data/knowledgeBase.ts`) | Manual, but enforced: `knowledgeBase.corpus.test.ts` fails the same way (the frontend workflow triggers on `backend/app/worker/sources/**`). |

If a corpus edit turns one of those guards red, the guard is right: update the
copy it names, or revert the corpus edit.

**The guards above only catch content the corpus LOSES.** A copy that says
*less* than the corpus contradicts nothing, so nothing goes red — which is
exactly how #170 shipped (`claude_code.md` had no installation content at all;
the fix added it in some places and not others). Containment cannot fix that:
the copies are deliberate abridgements. So each source doc's content digest is
pinned in `backend/tests/corpus_mirror_manifest.json`, and **any** edit — one
added sentence included — fails
`test_corpus_edits_are_reconciled_with_the_downstream_copies` until a human has
re-read the copies and re-pinned:

```bash
cd backend && uv run python -m tests.corpus_mirror --write
```

Re-pinning without reading the copies defeats the entire check. It is a review
checkpoint, not a formality.

**Note on `make demo` and semantic search.** Under the default stub embedder the
bootstrap seed deliberately leaves every embedding NULL and the index unstamped,
so `GET /health/index` reports `vector_arm.status: "dead"`. That is correct: the
stub's vectors are hash-bucketed and meaningless, and ranking by them would be
worse than not ranking at all. They are never *written* — the seeder builds its
runner with `write_vectors=False`, so there is no moment at which a live reader
can see them (the seed commits per source, and a re-seed runs against an
already-active `v1`). Set `CITEVYN_EMBEDDING_PROVIDER=gemini` (plus a key) and
re-seed to get a live, stamped vector arm.

## 4. Backup & restore

### 4.1 Backups (operator)

```bash
make backup
```

This invokes the `backup` profile — a one-shot `postgres:16-alpine`
container that runs `pg_dump --format=custom` against the live
database and writes to `./backups/citevyn-<UTC>.dump`. Keep at
least 7 days of dumps; the script does not rotate.

To schedule nightly backups, add a cron job on the host:

```cron
# /etc/cron.d/citevyn-backup
0 3 * * *  cd /opt/citevyn && make backup >> /var/log/citevyn/backup.log 2>&1
```

### 4.2 Restore

```bash
# Stop the api and worker so they don't try to write during restore
docker compose -f infra/docker/docker-compose.yml --profile prod stop api worker

# Restore. The dump file is read from the host bind-mount.
# (`make restore` delegates to infra/docker/scripts/restore.sh, which is the
#  same restore the release gate's data-recovery drill runs.)
make restore FILE=infra/docker/backups/citevyn-20260620T030000Z.dump

# Restart api + worker
docker compose -f infra/docker/docker-compose.yml --profile prod up -d api worker
```

The `restore` target runs `pg_restore --clean --if-exists` inside
the backup container. It drops and recreates the affected
tables; pre-existing data outside the dump is **not** lost
(pg_restore only touches objects present in the dump).

---

## 5. Release / rollback

### 5.1 Cut a new release (maintainer)

```bash
# 1. Bump version
$EDITOR backend/pyproject.toml        # version = "0.2.0"

# 2. Tag + push
git commit -am "chore: cut v0.2.0"
git tag -s v0.2.0 -m "v0.2.0 — production-ready"
git push --follow-tags
```

CI builds `citevyn/api:v0.2.0` and `citevyn/worker:v0.2.0`.

### 5.2 Roll forward

```bash
VERSION=v0.2.0 make refresh
```

This rebuilds locally, runs migrations, and brings the new
images up. Brief 502s on :443 are expected (~10s).

### 5.3 Roll back

**Use the script — it is the same path the release gate drills.**

```bash
make rollback TAG=v0.9.0        # explicit tag
make rollback TAG=--previous    # the tag before HEAD, resolved for you
```

`infra/docker/scripts/rollback.sh` refuses a stub `.env` and a dirty
tree, checks out the target tag, re-deploys via `refresh.sh`, and waits
for the api to report healthy. Preview with `--dry-run` (safe on a
dirty tree).

**It also refuses, up front, to attempt the impossible.** If the target
tag does not contain a migration the current release ships, the live
database is stamped at a revision that tag cannot resolve, and
`alembic upgrade head` dies with `Can't locate revision identified by
'0006'` mid-deploy. The script stops before the checkout and points
here. Recover the DATA instead, using a dump taken while the target was
live:

```bash
docker compose -f infra/docker/docker-compose.yml --profile prod stop api worker
./infra/docker/scripts/restore.sh infra/docker/backups/citevyn-<ts>.dump   # §4.2
./infra/docker/scripts/rollback.sh v0.9.0 --allow-migration-mismatch
```

`--allow-migration-mismatch` is the deliberate override. Use it only
after such a restore, or when you know the intervening migrations are
additive-only **and** the old code tolerates the current schema.

**Rolling back a SECOND time — you will need `--base-ref`.** A successful
rollback leaves you on a detached HEAD at the target tag. The migration
check compares the target against the *deployed* tree and uses `HEAD` as
that proxy, so from a detached HEAD it would be comparing against the
release you already rolled back to — while the database is still stamped
at the newest revision. The boundary would become invisible and the
check would wave through the very failure it exists to prevent. So it
refuses, and you name the deployed release yourself:

```bash
./infra/docker/scripts/rollback.sh <older-tag> --base-ref <currently-deployed-tag>
```

Or just `git checkout main` first, if the incident allows it.

Equivalent by hand:

```bash
git checkout v0.9.0                 # source tree at the previous tag
VERSION=v0.9.0 make refresh
```

The compose file re-builds the images from the old source. Migrations
that were forward-only in `v0.2.0` will NOT be rolled back — for
those, restore a backup (see §4.2). For pure application rollbacks
(no schema change), `make refresh` after the `git checkout` is
sufficient. After the incident, return with `git checkout main` (the
rollback leaves you on a detached HEAD).

### 5.4 Verify a release (the live gate)

Before tagging/announcing a release, run the one-command gate **on the
deploy host**:

```bash
VERSION=v0.10.0 PREV_VERSION=v0.9.0 make deploy-verify
```

Backup → deploy → functional verify (cited answer, refusal, exact
lookup, admin protected) → **two** rollback drills → roll forward →
re-verify, with a PASS/FAIL summary. Non-zero exit means **do not tag**.

The two drills, and what each proves (`RELEASE_PLAN` §10 blocker 9):

- **Drill A — data recovery.** Dump, stop the writers, `restore.sh`,
  bring the api back, re-verify. Always runs.
- **Drill B — code rollback to `PREV_VERSION`.** Runs only when that tag
  ships every migration the release does. When it does not, a code-only
  rollback is impossible (see §5.3), so the gate asserts `rollback.sh`
  refuses fast and then FAILS — unless you narrow the scope with
  `--data-rollback-only`, which makes the summary report blocker 9 as
  **PARTIAL**.

The summary always prints which of the two was proven. It never claims
a path it did not run.

It is **not** the full regression suite — `make ci`, `make golden`,
`make eval` and `make e2e` still run before the cut (see
`docs/DEMO_CHECKLIST.md`).

#### 5.3a Rolling back across an `answer_policy_version` bump

**Check this before rolling back — it has no migration and no error to
warn you.**

`CITEVYN_ANSWER_POLICY_VERSION` is part of the answer-cache key
pre-image, so bumping it invalidates every cached answer by design. It
gets bumped when a release makes previously-cached answers *wrong*
(v1 → v2 in #169: follow-up answers had been generated from a
concatenated query, so each was stored as a verbatim duplicate of the
previous turn's answer).

A rollback restores the OLD value — which brings those poisoned rows
back into key scope and re-serves them, for as long as
`CITEVYN_CACHE_TTL_SECONDS` (default 24h) has left to run. Nothing else
evicts them: their `source_version_hash` and `embedder_identity` are
still perfectly valid.

So when rolling back across a bump, roll the version *forward* instead
of letting it revert:

```bash
git checkout v0.9.0
# The bad release shipped v2; do NOT go back to v1 — pick a THIRD value
# so the cache is cold in both directions.
CITEVYN_ANSWER_POLICY_VERSION=v3 VERSION=v0.9.0 make refresh
```

Check which value you are leaving before you roll back:

```bash
grep -n 'answer_policy_version' backend/app/core/config.py   # the default
grep -rn 'CITEVYN_ANSWER_POLICY_VERSION' infra/docker/.env   # any override
```

The only cost of a third value is a cold answer cache, which refills on
demand. Re-serving a known-bad answer is much worse, and silent.

`infra/docker/scripts/rollback.sh` performs exactly the naive revert
described above and does not (yet) handle this — see its "What it does
NOT do" header, alongside the equivalent migration and index-promotion
caveats. Note the index-promotion one in particular: rolling an index back
is the admin promote API, and it needs `?force=true`, because the
previous-good index has no evaluation run and the #210 gate will refuse it.

---

## 6. Emergency contacts

- **On-call rotation:** see the GitHub repo's team settings.
- **Cloud provider support:** see `infra/docker/.env` (no
  cloud-specific keys are checked in; the LLM provider and the
  Postgres host are both cloud-managed).
- **Escalation:** open a GitHub issue with the `incident` label
  if the on-call cannot resolve within 30 minutes.

---

## 7. Post-incident

After any user-visible incident:

1. Write a post-mortem in `docs/postmortems/YYYY-MM-DD-<slug>.md`.
   The first section is the timeline; the rest is "what went
   well / poorly / where we got lucky".
2. Open follow-up issues for each action item. The label
   `postmortem-action` links them to the post-mortem.
3. Update this runbook if the playbook is now wrong.
