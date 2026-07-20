# Deploying CiteVyn on Fly.io

The runbook for the hosted demo at **https://citevyn.stackclimb.com**.
Follow it top to bottom for a first deploy; each later section stands alone
for day-2 operations.

**Config lives in [`fly.toml`](../fly.toml)** at the repo root — read its
comments alongside this document, they explain *why* each setting is what it
is. This runbook explains *what to do*.

> **This is not the only deployment path.** `infra/docker/` (compose + Caddy +
> local Postgres/Redis) is the self-hosted single-VM path, and
> [`RUNBOOK.md`](RUNBOOK.md) documents it. Where the two differ, this file
> says so explicitly. Do not mix the two sets of commands.

---

## 0. What this deployment is

| Piece | Where it runs | Tier |
|---|---|---|
| API (FastAPI/uvicorn) | Fly.io, **one** `shared-cpu-1x` machine, 256 MB, scale-to-zero | Fly free allowance |
| Postgres + pgvector | **Neon**, managed | Free (0.5 GB; the DB measures ~18 MB) |
| Redis | **Upstash**, managed | Free (500K commands/month) |
| TLS + certificate | **Fly's edge proxy** (`fly certs`) | Free |
| DNS | **Cloudflare**, DNS-only (grey cloud) | Free |
| Ingestion worker | one-shot `fly console` command, on demand | — |

Three things about this shape are load-bearing and are *decisions*, not
defaults:

1. **No Caddy.** Fly's proxy already terminates TLS and issues/renews the
   certificate. A second proxy would burn RAM on a machine that has ~150 MB
   spare and would add a hop to the client-IP chain. `infra/docker/Caddyfile`
   is not used here.
2. **Redis is required, not optional.** The machine auto-stops when idle, so
   the in-process rate limiter would hand out a fresh 30 q/h allowance on
   every wake. Only an external store makes the limit mean anything.
3. **Exactly one subdomain level** — `citevyn.stackclimb.com`, never
   `api.citevyn.stackclimb.com`. See §5.3.

---

## 1. Prerequisites

```bash
fly version            # flyctl installed
fly auth login
git status             # clean tree on the commit you intend to ship
```

You will also need accounts on **Neon** and **Upstash** (both free, both
sign-in-with-GitHub), and access to the `stackclimb.com` zone in Cloudflare.

### 1a. Create the Fly app (first deploy only)

Everything below resolves the app from `app = "citevyn"` in `fly.toml`, so the
app has to exist first. Skipping this is not subtle — the first `fly secrets
set` fails with `Could not find App "citevyn"` — but it is easy to miss when
following the runbook top to bottom:

```bash
fly apps create citevyn      # name must match `app` in fly.toml
fly apps list                # confirm it is there
```

Use `fly apps create`, **not** `fly launch`: `launch` is the interactive
scaffolder and will offer to overwrite the `fly.toml` in this repo, along with
provisioning a Fly Postgres/Redis you are deliberately not using (Neon and
Upstash are free; Fly's are not).

---

## 2. Create the managed data resources

### 2.1 Neon (Postgres + pgvector)

1. Create a project. **Pick the region that matches `primary_region` in
   `fly.toml` (`iad`)** — every request makes several DB round-trips, and a
   cross-continent hop dominates the latency budget. If you choose a
   different Neon region, change `primary_region` to match.
2. Database name: `citevyn` (any name works; it just has to match the DSN).
3. Enable pgvector **once**, in the Neon SQL editor:

   ```sql
   CREATE EXTENSION IF NOT EXISTS vector;
   ```

   Do this *before* the first deploy. The migrations create
   `vector(1536)` columns and will fail without the extension.
4. Copy the **pooled** connection string from the Neon dashboard and rewrite
   the scheme for SQLAlchemy + psycopg 3:

   ```
   Neon gives you:  postgresql://<user>:<password>@<host>/citevyn?sslmode=require
   CiteVyn wants:   postgresql+psycopg://<user>:<password>@<host>/citevyn?sslmode=require
   ```

   Keep `sslmode=require` — Neon refuses plaintext, and dropping it is how
   you get an opaque connection error on the first query. Use the **pooled**
   endpoint (the host containing `-pooler`): the machine restarts on every
   wake and re-establishes its pool each time.

### 2.2 Upstash (Redis)

1. Create a Redis database in the **same region** (`us-east-1` for `iad`).
2. Copy the connection string. Use the **TLS** URL — it starts `rediss://`
   (two `s`), not `redis://`. Upstash only accepts TLS on the public
   endpoint.
3. Free tier is 500K commands/month. The limiter spends a handful of
   commands per question, so the demo is nowhere near it; the number to watch
   is a runaway client, which is what the rate limiter itself is for.

---

## 3. Set the secrets

`fly.toml`'s `[env]` block holds only non-secret configuration. Everything
with a credential in it goes through `fly secrets`, which stores values
encrypted and injects them into both the app machine and the release machine.

> **Never commit a real value, and never paste one into this file.** The list
> below is *names only*. Generate the two API keys with
> `openssl rand -base64 32` and store them in your password manager.

```bash
fly secrets set \
  CITEVYN_DATABASE_URL=... \      # the Neon DSN from §2.1 (postgresql+psycopg://…)
  CITEVYN_REDIS_URL=... \         # the Upstash rediss:// URL from §2.2
  CITEVYN_DEMO_API_KEY=... \      # 32+ random chars — the bearer for every /v1/* route
  CITEVYN_ADMIN_API_KEY=... \     # 32+ random chars — DIFFERENT from the demo key
  CITEVYN_GEMINI_API_KEY=...      # LLM *and* embeddings both read this key
```

Notes on the list:

- **`CITEVYN_DEMO_API_KEY` and `CITEVYN_ADMIN_API_KEY` are not optional.**
  With `CITEVYN_ENVIRONMENT=production` (set in `fly.toml`) the config guards
  reject the publicly-known defaults `local-demo-key` / `local-admin-key`,
  and anything under 16 characters, *at parse time* — so a missing key fails
  the release command rather than quietly shipping an unauthenticated demo.
- **`CITEVYN_GEMINI_API_KEY` covers both roles.** `fly.toml` sets
  `CITEVYN_LLM_PROVIDER=gemini` and `CITEVYN_EMBEDDING_PROVIDER=gemini`, and
  both read this one key. The same guards refuse to boot with
  `CITEVYN_LLM_PROVIDER=stub` in production, so there is no way to
  accidentally serve the deterministic dev answers from the live demo.
- **If you switch embeddings to OpenRouter**, you need
  `CITEVYN_OPENROUTER_API_KEY` *and* an OpenAI-shaped
  `CITEVYN_EMBEDDING_MODEL` (e.g. `openai/text-embedding-3-small`) — the
  config rejects a `gemini-*` model name under the OpenRouter provider,
  because that combination POSTs a Gemini model id to an OpenAI-compatible
  endpoint and fails confusingly upstream.

Verify (this prints names and digests, never values):

```bash
fly secrets list
```

---

## 4. Deploy, migrate, seed, promote

### 4.1 Deploy

```bash
fly deploy --build-arg VERSION=$(git describe --tags --always)
```

What happens, in order:

1. Fly builds `infra/docker/Dockerfile.api` from the repo root.
2. Fly starts a **release machine** from the new image and runs
   `python -m alembic --config /db/alembic.ini upgrade head` (the
   `release_command` in `fly.toml`). Migrations therefore run *before* any
   traffic reaches the new code, and **a failure here aborts the deploy** —
   the old machine keeps serving.
3. The app machine is replaced and the health check on `GET /health` must
   pass before Fly considers the release good.

Watch it:

```bash
fly logs
fly status
```

### 4.2 Seed and ingest the corpus

Migrations create the schema; they do not create data. Seeding is a separate,
idempotent step you run once (and again after any corpus edit). Open a shell
on the machine:

```bash
fly ssh console        # shell on the RUNNING machine
```

(`fly console` is a different command — it starts a *throwaway* machine from
the same image, which also works for seeding since it gets the same secrets
and reaches the same Neon database. Use `fly ssh console` when you want to
inspect the machine that is actually serving.)

Then, inside the container:

```bash
python -m seed.seed_users      # admin + demo users (idempotent)
python -m seed.seed_catalog    # ingests app/worker/sources/*.md into index v1
```

`seed.*` (not `db.seed.*`) because `/db` is the package root on `PYTHONPATH`
in the image. `seed_catalog` **is** an ingest of the shipped corpus — it does
not carry its own copy — so it makes real embedding calls with your Gemini
key and fails loudly rather than seeding an empty index.

To ingest into a *candidate* index instead (the normal path for a corpus
update, RUNBOOK §3.7):

```bash
cd /app   # REQUIRED: the backend is a uv *virtual* project, so `app` is not
          # installed into site-packages — it is importable only from /app.
          # Without this you get ModuleNotFoundError: No module named 'app'.
python -m app.worker.cli run   # writes a candidate IndexVersion, does not serve it
```

### 4.3 Promote the index

A candidate index is not served until it is promoted. From your laptop:

```bash
curl -sS -X POST \
  -H "X-Admin-API-Key: $CITEVYN_ADMIN_API_KEY" \
  https://citevyn.stackclimb.com/v1/admin/index_versions/<index_version>/promote
```

> The path is `/v1/admin/index_versions/<v>/promote` — verified against the
> running app's OpenAPI, not from memory. An earlier draft of this runbook said
> `/internal/v1/indexes/...`, which does not exist and 404s.

**Promotion does NOT gate on evaluation quality.** `promote_version`
(`backend/app/services/index_versions.py`) demotes the current active index and
activates the candidate — that is all. There is no pass-rate check anywhere in
the promote path, so *you* are the gate: run `make golden` (and the judged eval
if the change is risky) BEFORE promoting, because promotion is the moment bad
retrieval reaches users. `CITEVYN_INDEX_PROMOTION_MIN_PASS_RATE` exists as a
setting but nothing reads it.

### 4.4 Confirm it is actually working

```bash
curl -sS https://citevyn.stackclimb.com/health                 # liveness, no DB
curl -sS https://citevyn.stackclimb.com/health/dependencies    # 503 if Postgres is unreachable
curl -sS https://citevyn.stackclimb.com/health/index           # vector_arm must NOT be "dead"
```

`vector_arm.status: "dead"` means the corpus was seeded with the stub
embedder and every embedding is NULL — semantic search is off and answers
degrade to lexical matching. Fix the embedding provider/key and re-seed.

Then ask a real question through the UI and confirm it comes back **grounded
and cited**. A 200 is not a passing demo.

---

## 5. DNS, TLS and the client-IP chain

### 5.1 Point the name at Fly

1. Get the app's hostname and addresses:

   ```bash
   fly ips list          # note the IPv4 (shared or dedicated) and IPv6
   ```

2. In Cloudflare, in the `stackclimb.com` zone, create:

   | Type | Name | Target | Proxy status |
   |---|---|---|---|
   | `CNAME` | `citevyn` | `citevyn.fly.dev` | **DNS only (grey cloud)** |

   A `CNAME` to the `.fly.dev` name is preferred over `A`/`AAAA` records: it
   keeps following Fly if the app's shared IP changes.

3. Issue the certificate:

   ```bash
   fly certs add citevyn.stackclimb.com
   fly certs show citevyn.stackclimb.com     # poll until it reports Ready
   ```

### 5.2 Why the cloud must stay grey

**Cloudflare proxying (orange cloud) must be OFF.** Two reasons, and the
second is the one that bites silently:

1. **Certificate issuance.** Fly validates the domain over the public HTTP/
   TLS path. With Cloudflare proxying in front, Cloudflare answers the
   challenge and Fly's `fly certs add` sits in `Awaiting certificates`
   indefinitely.
2. **The client-IP chain.** The rate limiter reads
   `CITEVYN_RATE_LIMIT_CLIENT_IP_HEADER`, set to **`Fly-Client-IP`** in
   `fly.toml`. That header is trustworthy *only because the app is reachable
   solely through Fly's proxy*, which overwrites it on every inbound request
   — a client cannot forge it. Put Cloudflare in front and Fly's proxy now
   sees **Cloudflare's edge** as the client, so `Fly-Client-IP` becomes a
   per-datacentre bucket and thousands of unrelated users share one rate
   limit.

   If you ever *do* turn proxying on, the correct header becomes
   **`CF-Connecting-IP`**, and it must change in the same commit that turns
   the orange cloud on:

   ```toml
   CITEVYN_RATE_LIMIT_CLIENT_IP_HEADER = "CF-Connecting-IP"
   ```

   Note the tradeoff you take on: `CF-Connecting-IP` is only trustworthy if
   the origin cannot be reached except through Cloudflare. On Fly the app
   stays reachable at `citevyn.fly.dev`, so anyone hitting that name directly
   can set the header themselves. Locking that down (Fly private networking,
   or an edge token) is a prerequisite for the switch, not a follow-up.

### 5.3 The one-subdomain-level constraint

Cloudflare's free **Universal SSL covers `stackclimb.com` and `*.stackclimb.com`
— one level only.** A deeper name such as `api.citevyn.stackclimb.com` is not
covered by that wildcard and needs **Advanced Certificate Manager, $10/month**.

So: **`citevyn.stackclimb.com`, and nothing deeper.** If you later need a
second service, use another one-level name (`citevyn-api.stackclimb.com`),
never a nested one. This constraint costs nothing to respect and $120/year to
break.

> This applies to any certificate Cloudflare issues for the zone. Fly issues
> the cert for the grey-cloud setup described above, but the constraint still
> governs the naming scheme — keep it one level so the zone stays free
> whichever side terminates TLS.

---

## 6. Rollback

**Do not run `infra/docker/scripts/rollback.sh` against this deployment.**
That script is the compose path: it checks out the target tag's source tree
and re-deploys via `refresh.sh` on a host that builds its own images. There is
no such host here. What transfers is its **contract**, and that contract is
what actually matters:

1. **A rollback does not reverse migrations.** They are forward-only. If the
   bad release migrated the schema, the live database is stamped at a revision
   the old image cannot resolve, and its `alembic upgrade head` dies with
   `Can't locate revision identified by '00NN'` — *in the release machine,
   mid-deploy*. `rollback.sh` refuses this case up front rather than
   discovering it halfway. **You must make the same check by hand here**:
   compare the migrations in `db/` between the deployed commit and the target
   before rolling back.
2. **A rollback does not restore data.** Recover data from a dump taken while
   the target release was live — [RUNBOOK §4.2](RUNBOOK.md#42-restore).
3. **A rollback does not reset the answer cache.** `answer_policy_version` is
   part of the cache key, so rolling back restores the *old* value and brings
   previously-poisoned answers back into scope for the rest of the TTL. If
   the release you are reverting bumped it, pin a **third**, unused value in
   `fly.toml` before rolling back so the cache is cold in both directions.
4. **A rollback does not demote an index.** Index rollback is the admin
   promote API (§4.3), separately.

### 6.1 The Fly-native rollback

```bash
fly releases                       # find the last-known-good version number
fly deploy --image <previous-image-ref>   # redeploy that exact image
```

Prefer redeploying the *image* over rebuilding from an older tag: it is the
artefact that was actually verified, and it skips a build that could resolve
differently today. Note the release command still runs against the *current*
database — which is precisely why check (1) above is not optional.

### 6.2 Backups

Neon's free tier keeps a short point-in-time-restore window; use the Neon
console's branch/restore for a bad-data incident. For an off-provider copy,
`pg_dump` the Neon DSN from your laptop on the same cadence RUNBOOK §4.1
prescribes (7 days retained, no rotation built in).

---

## 7. Uptime monitoring

**Set up an external monitor that polls
`https://citevyn.stackclimb.com/health` every 5 minutes.** UptimeRobot,
BetterStack and Cronitor all have free tiers that do exactly this; any of
them is fine. Alert to email.

Use `/health`, not `/health/dependencies`: this is an *is-the-demo-reachable*
signal, and the dependency probe would page you for a transient Neon blip.
Check `/health/dependencies` yourself when the monitor fires.

**Why this matters more than error tracking for a demo.** An error tracker
tells you that a request that *reached your code* went wrong. Almost every
way this deployment dies does not reach your code at all:

- the certificate failed to renew,
- the DNS record was edited or the orange cloud got switched on,
- Neon suspended the project or the free storage filled,
- the Upstash monthly command budget ran out,
- the machine is OOM-killed on boot and never serves a request,
- the Fly free allowance lapsed.

Every one of those is invisible to in-app error reporting and every one is
caught by an external GET returning something other than 200. And the failure
mode of a demo is not "an error was logged" — it is **someone opened the link
you sent and saw nothing**, with no one watching. A 5-minute external poll is
the cheapest thing that turns that into an email.

The poll has a second, free benefit: it keeps the machine warm enough that
most human visitors miss the cold start, without setting
`min_machines_running = 1`.

---

## 8. Cost ceiling

`fly.toml` sets `CITEVYN_COST_HARD_DAILY_USD=2` (the owner's ceiling) with a
soft warning at `1`. The budget is computed by **summing `provider_calls`
since midnight UTC**, so it survives the machine stopping and starting — which
matters enormously here, where auto-stop destroys the process routinely. An
in-process counter would grant a fresh allowance on every wake, and the 30 q/h
per-user rate limit is anti-nuisance, **not** a spend control.

`CITEVYN_COST_BUDGET_FAIL_CLOSED` stays at its default `true`: if the meter
cannot be read we do not know what has been spent, and an unreadable meter
must not become an unmetered spending window. Also set a hard cap on the
provider side (Google AI Studio billing) — the app-side budget is a control,
not a guarantee.

See [COST_CONTROLS.md](COST_CONTROLS.md) for the full model.

---

## 9. Quick reference

```bash
fly status                    # machine state, health checks, current release
fly logs                      # stream logs
fly ssh console               # shell on the machine (seed / ingest / inspect)
fly secrets list              # names + digests, never values
fly certs show citevyn.stackclimb.com
fly releases                  # deploy history, for rollback targets
fly scale show                # confirm 256 MB / shared-cpu-1x
```
