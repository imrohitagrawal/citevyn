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

### 1b. Allocate IP addresses (first deploy only)

**`fly apps create` does not allocate any IPs — only `fly launch` does.** This
is the one consequence of avoiding `launch` that bites, and it bites late: the
deploy succeeds, the machine passes its health check, and then *every* request
fails with `Could not resolve host: citevyn.fly.dev`, because the hostname has
no address records at all. It reads like a DNS or TLS problem and is neither.

```bash
fly ips allocate-v4 --shared   # FREE. A dedicated v4 is $2/mo and is not needed.
fly ips allocate-v6            # free
fly ips list                   # both should be listed before you deploy
```

`--shared` is deliberate: a shared IPv4 is fine for an HTTP service behind
Fly's proxy, and a dedicated one would quietly add $2/mo to a deployment whose
entire budget is a couple of dollars.

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

**Provision it through Fly, not by signing up to Upstash separately.** Fly
resells Upstash as a first-party integration, so there is no second account to
create and no separate dashboard:

```bash
fly redis create --name citevyn-redis --org personal --region iad \
    --no-replicas --disable-eviction
```

This command **requires a TTY** — piping input or wrapping it in `script` both
fail with `Error: prompt: non interactive`, so it cannot be automated. Two
answers matter:

* **ProdPack: no.** It is a $200/mo add-on and the prompt is easy to skim past.
* **Plan: Pay-as-you-go.** $0 base + $0.20 per 100K commands. At the demo's
  30 q/h ceiling that is cents a month. (Pass it interactively — `--plan
  "pay-as-you-go"` is rejected with `plan "pay-as-you-go" not found`; the flag
  string does not match what `fly redis plans` prints.)

Then read the URL back with `fly redis status citevyn-redis`.

> **Use the `redis://` URL exactly as Fly prints it — NOT `rediss://`.**
> A Fly-provisioned Upstash database lives on Fly's private 6PN network: the
> host resolves to an `fdaa:…` address that is unreachable from the public
> internet (verify with `nc -z fly-citevyn-redis.upstash.io 6379` from your
> laptop — it fails, by design). There is no public TLS endpoint to point
> `rediss://` at. The TLS advice applies only if you sign up to Upstash
> directly and use their public endpoint, which this runbook no longer does.

**Lua `EVAL` is supported** — verified against a live instance with the real
`_SLIDING_WINDOW_LUA` script from `app/core/rate_limit.py`, which returned
`{1, 1}` (allowed) then `{0, 1}` (denied) on a `limit=1` bucket. `SCRIPT LOAD`
works too. The rate limiter depends on this, so it was worth proving rather
than assuming.

> **`fly redis reset` is a breaking change.** It rotates the password and the
> running app immediately starts returning
> `503 rate_limiter_unavailable` until you follow it with a matching
> `fly secrets set CITEVYN_REDIS_URL=…` and give it ~30s to propagate. Do not
> paste the new URL into a chat, ticket or terminal transcript — read it with
> `fly redis status` and pipe it straight into `fly secrets set`.
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

**Promotion gates on evaluation quality, and on a fresh deployment that gate
will block you.** `promote_version`
(`backend/app/services/index_versions.py`) reads the newest *completed*
`EvaluationRun` for the candidate and refuses with **HTTP 409
`promotion_blocked`** unless the measured pass rate is at least
`CITEVYN_INDEX_PROMOTION_MIN_PASS_RATE` (default `0.95`; a rate exactly equal to
the threshold promotes). Refusing is also what happens when there is **no**
completed run, or when the run's metrics cannot be read — "unevaluated" is not
"passing".

**Produce the evidence, then promote.** Since #216 the worker can measure a
candidate index against the shipped corpus and write the `EvaluationRun` row the
gate reads:

```bash
fly ssh console -a citevyn -C "python -m app.worker.cli evaluate --index-version <candidate>"
```

Exit `0` means it measured at or above `CITEVYN_INDEX_PROMOTION_MIN_PASS_RATE`
and the promote below will succeed with **no** `force`. Exit `2` means the
candidate genuinely measured below threshold — read `failure_summary` on the run
(`GET /v1/admin/evaluations?index_version=<candidate>` to find the run, then `GET /v1/admin/evaluations/{run_id}` for its `failure_summary` — the list endpoint returns counts only) before doing anything
else, because that is the gate working, not the gate misfiring.

Run it AFTER ingesting into the candidate and BEFORE promoting. A promote with
no completed run still 409s — "unevaluated" is not "passing".

The audited override remains for the cases that legitimately have no evidence: a
bootstrap (the seed's `_activate` path), or an emergency index rollback (§6) that
cannot wait for a suite. It is no longer the ordinary path:

```bash
curl -sS -X POST \
  -H "X-Admin-API-Key: $CITEVYN_ADMIN_API_KEY" \
  "https://citevyn.stackclimb.com/v1/admin/index_versions/<index_version>/promote?force=true"
```

`force=true` promotes anyway and writes `force`, `measured_pass_rate`,
`threshold` and `evaluation_run_id` into the `promote_index` audit row, so the
override is evidence, not a hole. Before you use it, **run the evaluation
yourself** — `make golden` locally against the same corpus, and the judged eval
if the change is risky — because promotion is the moment bad retrieval reaches
users, and with `force` you are once again the gate. State the reason in your
deploy notes: "forced: no in-production evaluation runner; golden 50/50 locally
at &lt;commit&gt;".

The response body tells you which path you took:

```json
{"index_version": "v2", "status": "active", "already_active": false,
 "forced": true, "measured_pass_rate": null}
```

A 409 body names both numbers so you can see how far short the candidate fell:

```json
{"request_id": "...", "status": "error",
 "error": {"code": "promotion_blocked",
           "message": "index_version v2 not promoted: pass rate below the promotion gate (measured pass_rate 0.82, required >= 0.95)",
           "details": {"reason": "below_threshold", "measured_pass_rate": 0.82,
                       "threshold": 0.95, "evaluation_run_id": "..."}}}
```

Re-promoting the index that is already active is a no-op and is **never**
blocked — the idempotent path returns 200 before the gate runs.

**That no-op is not the dual-active repair**, and it is worth being exact about
this because the reverse is easy to assume. If the database has drifted into a
dual-active state (you will see `orchestrator_multiple_active_indexes` in the
logs), the thing that converges it is the demotion loop, and that loop only runs
when you promote a **different** version — which means it runs *below* the gate.
So the repair is gated like any other promotion, and on a stack with no
evaluation runs it will 409 until you pass `?force=true`. Mid-incident, that is
the command you want:

```bash
curl -X POST -H "X-API-Key: $CITEVYN_ADMIN_API_KEY" \
  "https://citevyn.stackclimb.com/v1/admin/index_versions/<index_version>/promote?force=true"
```

Promoting the row that is *already* active will return 200 and change nothing.

Only gate 1 of [RELEASE_PLAN §7](RELEASE_PLAN.md) is machine-enforced; citation
correctness, retrieval hit rate, guardrail failures and ingestion errors are
still yours to check.

### 4.4 Confirm it is actually working

```bash
curl -sS https://citevyn.stackclimb.com/health                 # liveness, no DB
curl -sS https://citevyn.stackclimb.com/health/dependencies    # 503 if Postgres is unreachable
curl -sS https://citevyn.stackclimb.com/health/index           # vector_arm must NOT be "dead"
```

`vector_arm.status: "dead"` means the corpus was seeded with the stub
embedder and every embedding is NULL — semantic search is off and answers
degrade to lexical matching. Fix the embedding provider/key and re-seed.

Then ask a real question and confirm it comes back **grounded and cited**. A
200 is not a passing demo.

The image serves the browser bundle at `/` (built in stage 0 of
`infra/docker/Dockerfile.api` and mounted by `_mount_frontend` in
`app/main.py`), so the UI and the API share one origin — no CORS, and the
deployment stays one subdomain deep. Open `https://citevyn.stackclimb.com/`
and ask there.

To check the same thing from a terminal, note two shapes that are easy to get
wrong (both cost a debugging round-trip the first time):

* auth is **`Authorization: Bearer <key>`**, not an `X-Demo-API-Key` header;
* the message field is **`message`**, not `content` (a wrong name gives a
  422 whose `details.errors[].input` is `<redacted>`, so the body you sent is
  deliberately not echoed back).

```bash
SID=$(curl -sS -X POST https://citevyn.stackclimb.com/v1/sessions \
        -H "Authorization: Bearer $CITEVYN_DEMO_API_KEY" \
        -H 'Content-Type: application/json' -d '{}' \
      | python3 -c 'import sys,json;print(json.load(sys.stdin)["session_id"])')

curl -sS -X POST "https://citevyn.stackclimb.com/v1/sessions/$SID/messages" \
  -H "Authorization: Bearer $CITEVYN_DEMO_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"message":"How does streaming work in the Claude API?"}'
```

Expect `retrieval_strategy: hybrid_reranked` and a non-empty `citations`
array. **A refusal is not automatically a bug** — the corpus is six documents,
and asking about something it does not cover (`prompt caching`, say) *should*
return "I couldn't find a grounded answer". Check the corpus with `grep -ri
"<term>" backend/app/worker/sources/` before treating a refusal as a
regression; refusing beats inventing, and that is the product working.

Two more routing facts worth knowing before you call something broken: a
question with no product noun ("what is prompt caching?") routes to
`domain: unsupported` by design, and the same question naming a product
("...in the Claude API") routes correctly. Test with the product named.

---

## 5. DNS, TLS and the client-IP chain

### 5.1 Point the name at Fly

1. Get the app's hostname and addresses:

   ```bash
   fly ips list          # note the IPv4 (shared or dedicated) and IPv6
   ```

2. In Cloudflare, in the `stackclimb.com` zone, create **both** records, using
   the addresses `fly ips list` just printed:

   | Type | Name | Target | Proxy status |
   |---|---|---|---|
   | `A` | `citevyn` | the shared IPv4, e.g. `66.241.124.66` | **DNS only (grey cloud)** |
   | `AAAA` | `citevyn` | the dedicated IPv6, e.g. `2a09:8280:1::151:b3cc:0` | **DNS only (grey cloud)** |

   These are what `fly certs add` itself asks for — run it (step 3) and it
   prints a "Recommended DNS setup" block naming exactly these two record
   types.

   > An earlier version of this runbook recommended a `CNAME` to
   > `citevyn.fly.dev` instead, on the reasoning that it would keep following
   > a changing shared IP. Fly's own instruction is `A` + `AAAA`, so follow
   > that. If you use a `CNAME`, add only the `A`-side equivalent — do not mix
   > a `CNAME` with `AAAA` records on the same name, which is invalid DNS.

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
   promote API (§4.3), separately — and it needs `?force=true`, because the
   previous-good index has no evaluation run either and the promotion gate
   will 409 you in the middle of an incident.

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
