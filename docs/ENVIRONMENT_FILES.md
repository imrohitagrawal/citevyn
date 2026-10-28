# Environment files: which one is read, and how to look after them

This repository is **public**. This page names files, variables and rules. It
never holds a value, and nothing you add to it may hold one either:
`backend/tests/test_environment_files_doc.py` fails the build if a secret-shaped
assignment appears here.

## Which file each launch path reads

| File | In git? | Read by | How it is read |
|---|---|---|---|
| `backend/.env` | No (ignored) | The backend started from `backend/`: `make test`, `make eval`, `uv run uvicorn ...` | `Settings` loads `.env` from the **current folder** (`env_file=".env"` in `backend/app/core/config.py`). Start the app anywhere else and this file is silently skipped |
| `infra/docker/.env` | No (ignored) | Everything that goes through Docker Compose: `make demo`, `make db-up`, `make deploy`, `make refresh` | Compose hands it to the `db`, `api`, `worker`, `backup` and `caddy` containers (`env_file:`), and also uses it to fill the `${VAR}` slots in `docker-compose.yml` |
| `frontend/.env.local` | No (ignored) | The Vite dev server (`npm run dev`) | Vite loads it. Only `VITE_*` names reach the browser bundle |
| `.env.example` | Yes | Nobody at run time | Template for `backend/.env` |
| `infra/docker/prod.env.example` | Yes | `make env-bootstrap` copies it when `infra/docker/.env` is missing | Template for `infra/docker/.env` |
| `frontend/.env.example` | Yes | Nobody at run time | Template for `frontend/.env.local` |

Production and CI read **no file**:

- **Production (Fly)** reads Fly secrets (`fly secrets list -a citevyn` shows the
  names) plus the `[env]` block in `fly.toml`. See [`DEPLOY_FLY.md`](DEPLOY_FLY.md).
- **CI** reads GitHub Actions secrets. Today that is only
  `CITEVYN_OPENROUTER_API_KEY`, used by the judged answer-quality eval.

## Which value wins

1. A variable set in the shell beats the file.
   `CITEVYN_LLM_PROVIDER=router make eval` uses `router` even if `backend/.env`
   says `stub`.
2. The file beats the default in `Settings`.
3. The default applies when neither sets it.

Every variable needs the `CITEVYN_` prefix. A setting whose field name already
starts with `citevyn_` is read as `CITEVYN_CITEVYN_...`; the short form is
silently ignored. `backend/tests/test_settings_env_vars_are_documented.py`
explains this trap.

## Why there are two backend files

They are kept apart on purpose:

- `backend/.env` is for development and tests. Keep `CITEVYN_LLM_PROVIDER=stub`
  and `CITEVYN_EMBEDDING_PROVIDER=stub` there, so routine runs cost nothing.
  Paid runs are opted into per command, with a shell override.
- `infra/docker/.env` runs the full stack with real providers.

The price of two files is drift. Nothing checks one against the other, so when
you rotate a key, change it in **every** file that holds it, then confirm with
the check below.

## Looking after them

- **Never commit one.** `.gitignore` ignores `.env` and `.env.*`, except
  `.env.example`. The guard test checks that `backend/.env`,
  `infra/docker/.env` and `frontend/.env.local` stay ignored.
- **Never print one.** That includes `cat`, `docker compose config` (it prints
  every key in full, see the header of `infra/docker/docker-compose.yml`),
  `printenv` inside a container, and any screenshot. Do not paste their contents
  into an issue, a PR, a CI log, a chat or a support thread.
- **Kept out of docker builds.** The build context is the repo root, and a
  docker pattern matches from the root only, so `.dockerignore` uses `**/.env`
  and `**/.env.*`. Before that fix, every nested file above reached the build:
  uploaded to Fly's remote builder on `fly deploy`, with `frontend/.env.local`
  inside the stage that bakes the browser bundle.
  `tests/shell/test_dockerignore_env.sh` builds a real context to prove they
  stay out.
- **Every container gets every secret.** Compose's `env_file:` gives the whole
  file to each service that lists it, including the internet-facing `caddy`.
  Narrowing this is tracked in #200.
- **No copies or backups inside the repo folder.** A `.env.bak` is git-ignored,
  but it is still a second copy of live keys that nobody rotates. Delete it.
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

1. **No reader.** Nothing loads it by name. The loaders are exact:
   `Settings` reads `.env` in the start folder; Compose reads the paths under
   `env_file:` and `infra/docker/.env`; Vite reads `.env`, `.env.local`,
   `.env.[mode]` and `.env.[mode].local` in `frontend/`. Also
   `git grep -F '<file name>'` finds no script or doc that uses it.
2. **Nothing unique.** Every value in it either exists in a file that is in use
   (compare by hash, as below) or is obsolete: a default, or a setting that has
   moved elsewhere.
3. **Not in a build.** `.dockerignore` keeps it out (it does, for any `.env*`
   name).
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

Remaining OpenRouter credit, with no model call and no cost:

```sh
K=$(grep '^CITEVYN_OPENROUTER_API_KEY=' backend/.env | cut -d= -f2- | tr -d '"')
curl -s -H "Authorization: Bearer $K" https://openrouter.ai/api/v1/key; unset K
```
