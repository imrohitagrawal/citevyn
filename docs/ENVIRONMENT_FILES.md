# Environment files: which one is read, and how to look after them

This repository is **public**. This page names files, variables and rules. It
never holds a value, and nothing you add to it may hold one either:
`backend/tests/test_environment_files_doc.py` fails the build on the common
shapes: a name ending in KEY, SECRET, TOKEN or PASSWORD given a value, or a URL
that carries a password.
It is a net, not a proof: a bare key or a JSON value can still slip past it, so
review additions to this page by eye too.

## Which file each command reads

The backend's `Settings` reads **no file**: only the process environment
(`env_file=None` in `backend/app/core/config.py`), as on Fly. The Makefile
hands `backend/.env` to the development commands through
`scripts/with_env.py`, whichever folder they run in. Choose another file with
`DEV_ENV_FILE=path` (it must exist). If `backend/.env` is missing (CI, a fresh
clone), one line says so and the command runs on the process environment. A
bare `uvicorn` or `alembic` reads no file; use the `make` targets.

| File | In git? | Read by | How |
|---|---|---|---|
| `backend/.env` | No (ignored) | `make api`, `make worker`, `make migrate`, `make seed`, `make demo`, `make ci-smoke` (when present), `make eval`, `make golden`, `make golden-smoke`, `make test-pg`; every backend step of `make smoke` / `make e2e` (migrate, seed, server); `make budget` when the key is not in `infra/docker/.env`. **Not** `make test` or `make coverage`, which load no file, as in CI | `scripts/with_env.py` loads it into the command's environment (the Makefile's `DEV_ENV_FILE`); `scripts/check_budget.sh` sources it for the OpenRouter key |
| `.env` (repo root) | No (ignored) | Nobody. Retired: `make migrate`/`seed`/`demo` used to read it, because `Settings` read `.env` from the folder a command ran in | If you still have one, move what you need into `backend/.env`, then delete it after the four checks below |
| `infra/docker/.env` | No (ignored) | Everything through Docker Compose (`make db-up`, `make deploy`, `make refresh`, the `db`/`api`/`worker`/`backup`/`caddy` containers); `_env_guard.sh` (deploy, refresh, backup, restore, rollback); `deploy_verify.sh`; `make budget`; `make demo-frontend` (the client token it bakes into the bundle) | Compose `env_file:` passes it to each container and fills the `${VAR}` slots in `docker-compose.yml`; the scripts source it |
| `frontend/.env.local` | No (ignored) | `npm run dev` (also the dev server Playwright starts), and every `vite build`: `npm run build`, `make demo-frontend` | Vite loads it. Only `VITE_*` names reach the browser bundle, and **whatever it sets is baked into `frontend/dist`** unless the command sets the same name itself |
| `.env.example` | Yes | Nobody at run time | Template for `backend/.env` |
| `infra/docker/prod.env.example` | Yes | `make env-bootstrap` copies it when `infra/docker/.env` is missing | Template for `infra/docker/.env` |
| `frontend/.env.example` | Yes | Nobody at run time | Template for `frontend/.env.local` |

Outside your machine:

- **Production (Fly)** reads Fly secrets (`fly secrets list -a citevyn` shows the
  names) and the `[env]` block in `fly.toml`. No `.env` file. See
  [`DEPLOY_FLY.md`](DEPLOY_FLY.md).
- **CI** adds one secret of its own, `CITEVYN_OPENROUTER_API_KEY`, for the judged
  answer-quality eval (plus GitHub's automatic `GITHUB_TOKEN`). The compose smoke
  job also writes and reads an `infra/docker/.env`, made by `make env-bootstrap`
  from `prod.env.example` with development-only values.

## Which value wins

For a command the Makefile runs through `scripts/with_env.py`:

1. A variable set in the shell beats the file.
   `CITEVYN_LLM_PROVIDER=router make eval` uses `router` even if `backend/.env`
   says `stub`. The same goes for what a recipe sets itself: `make migrate` and
   `make seed` set `CITEVYN_DATABASE_URL` from `DB_URL`, so a database URL in
   `backend/.env` does not change which database they write to.
2. The file beats the default in `Settings`.
3. The default applies when neither sets it.

The file is parsed by python-dotenv, as `Settings` parsed it before (quotes,
`export`, `${NAME}` expansion, the last of two equal names wins). A line it
cannot parse, or a name with no `=`, stops the command and names the line
number; nothing from the file is printed.

Inside a compose container, a value in the service's `environment:` block beats
`env_file:`. `docker-compose.yml` pins `CITEVYN_ENVIRONMENT` that way, and fills
`CITEVYN_LLM_PROVIDER` from `${CITEVYN_LLM_PROVIDER:-stub}`.

Backend variables need the `CITEVYN_` prefix (`POSTGRES_*` in
`infra/docker/.env` are for the Postgres image, not the app). A setting whose
field name already starts with `citevyn_` is read as `CITEVYN_CITEVYN_...`; the
short form is silently ignored. `backend/tests/test_settings_env_vars_are_documented.py`
explains this trap.

## Why there is more than one backend file

- `backend/.env` is for development. Keep `CITEVYN_LLM_PROVIDER=stub`
  and `CITEVYN_EMBEDDING_PROVIDER=stub` there, so routine runs cost nothing.
  Paid runs are opted into per command, with a shell override. `make seed`,
  `make demo` and `make smoke` ingest the corpus with the embedder this file
  names (the seed prints it first), so a real provider here makes them spend
  money.
- `infra/docker/.env` runs the full stack with real providers.

The price of several files is drift. Nothing checks one against another, so when
you rotate a key, change it in **every** file that holds it, then confirm with
the check below.

## Looking after them

- **Never commit one.** `.gitignore` ignores every `.env`-prefixed name (`.env`,
  `.env.local`, `.env~`, `.env-old`, `.envrc`, ...) except `.env.example`. The
  guard test checks that each file above stays ignored.
- **Never print one.** That includes `cat`, `docker compose config` (it prints
  every key in full, see the header of `infra/docker/docker-compose.yml`),
  `printenv` inside a container, and any screenshot. Do not paste their contents
  into an issue, a PR, a CI log, a chat or a support thread.
- **Kept out of docker builds.** The build context is the repo root, and a
  docker pattern matches from the root only, so `.dockerignore` uses
  `**/.env*`. The old bare `.env` and `.env.*` rules excluded only the root files. What
  that let through depends on the builder (measured 2026-10-29 with Docker
  29.7, placeholder files, the old rules and the same `COPY` lines as
  `infra/docker/Dockerfile.api`):
  - **BuildKit** (the default) uploads only the paths the Dockerfile copies.
    `backend/.env` and `infra/docker/.env` were not sent.
    `frontend/.env.local` was, because the bundle stage copies all of
    `frontend/`; any `VITE_*` name in it that the frontend code reads and the
    Dockerfile does not set itself would have been baked into the bundle.
    Today that file holds only `VITE_API_LIVE`, which the Dockerfile sets, so
    nothing from it reached a bundle.
  - **The legacy builder** (`DOCKER_BUILDKIT=0`) sends the whole context, so
    all three files went to the daemon.

  No final image held `backend/.env` or `infra/docker/.env`: the images copy
  only `backend/app`, `db` and the built bundle. Neither of `fly deploy`'s
  paths was measured: `--local-only` (flyctl drives local Docker) or the remote
  builder (Fly's own, or Depot; see [`DEPLOY_FLY.md`](DEPLOY_FLY.md)). The
  `**/` rule still matters: it keeps the
  files out of a legacy build and out of any future `COPY` of a whole folder.
  `tests/shell/test_dockerignore_env.sh` builds a real context to prove they
  stay out.
- **Every container gets every secret.** Compose's `env_file:` gives the whole
  file to each service that lists it, including the internet-facing `caddy`.
  Narrowing this is tracked in #555.
- **No copies or backups inside the repo folder.** Git ignores them only when
  the name starts with `.env` (`backend/env.bak` or `infra/docker/prod.env` would
  be committable), and even ignored, each is a second copy of live keys that
  nobody rotates. Delete them.
- **Use model names the price list knows.** The cost meter prices exact model
  ids (`backend/app/cost/pricing.py`). An alias such as `gemini-flash-latest`
  is recorded as unpriced (#492).
- **Rotate on any leak.** Rotate at the provider first, then update every file
  and secret that holds the key (the table above lists where to look).
- **Templates hold placeholders only.** Every `Settings` field must be declared
  in an env example, or exempted by name
  (`backend/tests/test_settings_env_vars_are_documented.py`).

## Deciding a file is unused before deleting it

A deleted env file cannot be recovered; it is not in git. Delete one only when
all four checks hold:

1. **No reader.** No command in the table above reads it, and
   `git grep -F '<file name>'` finds no script or doc that names it. The loaders
   match exact names: the Makefile loads `DEV_ENV_FILE` (default `backend/.env`);
   Compose reads its `env_file:` paths and `infra/docker/.env`; Vite reads
   `.env`, `.env.local`, `.env.[mode]` and `.env.[mode].local` in `frontend/`.
2. **Nothing unique.** Every value in it either exists in a file that is in use
   (compare by hash, as below) or is obsolete: a default, or a setting that has
   moved elsewhere.
3. **Not in a build.** `.dockerignore` keeps every `.env`-prefixed name out.
4. **Not the only record of a password something still needs**, such as the
   password an existing Postgres volume was created with.

## Checking without printing anything

Which variables a file sets (names only):

```sh
sed -E 's/=.*//' backend/.env | grep -vE '^\s*(#|$)'
```

Whether two files hold the same key, without showing it. Equal output means the
same value:

```sh
for f in backend/.env infra/docker/.env; do
  printf '%s ' "$f"; grep '^CITEVYN_OPENROUTER_API_KEY=' "$f" | cut -d= -f2- | shasum -a 256 | cut -c1-10
done
```

Keep that output on your machine; do not paste it anywhere.

Remaining OpenRouter credit, free, with the key kept off the command line:

```sh
make budget
```
