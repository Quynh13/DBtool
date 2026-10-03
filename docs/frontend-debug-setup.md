# Frontend and Debug Development Setup

The validated Django MVP is frozen at tag `django-mvp-2026-10-04` on `main`.
Develop the new frontend and database-debug UI on `develop/frontend-debug`.
Do not deploy this branch until the existing flows and the new debug flow pass.

The current Django application keeps running unchanged. The local Tool DB
snapshot is `local-test/baseline-2026-10-04/db.sqlite3`; the development copy
is `local-test/frontend-debug/db.sqlite3`. Both are ignored by Git. The
snapshot is local only and is **not** a backup of a Docker volume or Target DB.
The running server's Docker image is older (it has no `dbtool` migrations).
Its Tool DB was backed up separately at
`local-test/legacy-container-2026-10-04/db.sqlite3`; do not restore that
older database over the Django MVP snapshot.

1. Copy `frontend-debug.env.example` to `.env.frontend-debug` and supply a
   **dedicated test Target DB**. Do not copy credentials into tracked files.
   The template keeps Tool DB migrations on the development SQLite copy.
2. Run `uv sync --locked`.
3. Run `uv --no-cache run --env-file .env.frontend-debug python manage.py check`
   and `uv --no-cache run --env-file .env.frontend-debug python manage.py migrate --check`.
4. Run `uv --no-cache run --env-file .env.frontend-debug python manage.py runserver 127.0.0.1:8001`.

When a new Tool DB migration is intentionally added, run `migrate` with
`.env.frontend-debug` only. Target DB remains unmanaged by Django. Keep
frontend and backend source separate, but preserve backend authorization,
validation, MutationService, audit, and rollback. First move existing flows
to a minimal API-driven frontend; then build the live debug experience; only
after both pass should UI/UX expansion and release be considered.
