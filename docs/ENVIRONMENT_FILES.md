# Environment files: which one is read, and how to look after them

This repository is **public**. This page names files, variables and rules. It
never holds a value, and nothing you add to it may hold one either:
`backend/tests/test_environment_files_doc.py` fails the build on the common
shapes: a name ending in KEY, SECRET, TOKEN or PASSWORD given a value, or a URL
that carries a password.
It is a net, not a proof: a bare key or a JSON value can still slip past it, so
review additions to this page by eye too.

## Which file each command reads

The backend's `Settings` reads `.env` from **the folder the command runs in**
(`env_file=".env"` in `backend/app/core/config.py`). `uv run --project backend`
does not change that folder. So which backend file is read depends on the
command, not on where the code lives.

| File | In git? | Read by | How |
|---|---|---|---|
| `backend/.env` | No (ignored) | `make test`, `make eval`, `uvicorn` started from `backend/`; the server that `make smoke` / `make e2e` start; `make budget` when the key is not in `infra/docker/.env` | `Settings` (these commands `cd backend` first); `scripts/check_budget.sh` sources it for the OpenRouter key |
| `.env` (repo root) | No (ignored) | `make migrate`, `make seed`, `make demo`, `make ci-smoke`; the migrate and seed steps of `make smoke` / `make e2e` | `Settings`, because these run `uv run --project backend ...` from the repo root, so they do not read `backend/.env`. `make smoke` mixes the two: it seeds with the root file and serves with `backend/.env`, so keep their embedder settings the same. If the root file is absent, `Settings` defaults apply (stub providers) |
| `infra/docker/.env` | No (ignored) | Everything through Docker Compose (`make db-up`, `make deploy`, `make refresh`, the `db`/`api`/`worker`/`backup`/`caddy` containers); `_env_guard.sh` (deploy, refresh, backup, restore, rollback); `deploy_verify.sh`; `make budget`; `make demo-frontend` (the client token it bakes into the bundle) | Compose `env_file:` passes it to each container and fills the `${VAR}` slots in `docker-compose.yml`; the scripts source it |
| `frontend/.env.local` | No (ignored) | `npm run dev` (also the dev server Playwright starts), and every `vite build`: `npm run build`, `make demo-frontend` | Vite loads it. Only `VITE_*` names reach the browser bundle, and **whatever it sets is baked into `frontend/dist`** unless the command sets the same name itself |
| `.env.example` | Yes | Nobody at run time | Template for `backend/.env` (and a root `.env`) |
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

For `Settings` (any command that runs the backend directly):

1. A variable set in the shell beats the file.
   `CITEVYN_LLM_PROVIDER=router make eval` uses `router` even if `backend/.env`
   says `stub`.
2. The file beats the default in `Settings`.
3. The default applies when neither sets it.

Inside a compose container, a value in the service's `environment:` block beats
`env_file:`. `docker-compose.yml` pins `CITEVYN_ENVIRONMENT` that way, and fills
`CITEVYN_LLM_PROVIDER` from `${CITEVYN_LLM_PROVIDER:-stub}`.

Backend variables need the `CITEVYN_` prefix (`POSTGRES_*` in
`infra/docker/.env` are for the Postgres image, not the app). A setting whose
field name already starts with `citevyn_` is read as `CITEVYN_CITEVYN_...`; the
short form is silently ignored. `backend/tests/test_settings_env_vars_are_documented.py`
explains this trap.

## Why there is more than one backend file

- `backend/.env` is for development and tests. Keep `CITEVYN_LLM_PROVIDER=stub`
  and `CITEVYN_EMBEDDING_PROVIDER=stub` there, so routine runs cost nothing.
  Paid runs are opted into per command, with a shell override.
- `infra/docker/.env` runs the full stack with real providers.
- A root `.env` is optional. Create one only if `make migrate`/`seed`/`demo`
  need settings beyond the database URL; a real provider there makes
  `make seed` (an ingest) spend money.

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
  `**/.env*`. Before that fix every nested file above reached the build context:
  sent to the build daemon (local Docker with `fly deploy --local-only`, Fly's
  remote builder otherwise). No final image held one of the files, because the
  images copy only `backend/app`, `db` and the built bundle; but the bundle
  stage copied `frontend/`, so any `VITE_*` value in `frontend/.env.local` that
  the Dockerfile does not set itself would have been baked into it.
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
   match exact names: `Settings` reads `.env` in the folder a command runs in;
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
