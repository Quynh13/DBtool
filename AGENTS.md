# Repository Guidelines

## Project Structure & Module Organization

- `config/` contains Django settings, root URLs, ASGI, and WSGI entry points.
- `dbtool/` implements PostgreSQL introspection, safety and semantic policy, generic CRUD, review forms, and editor templates under `dbtool/templates/dbtool/`.
- `audit/` owns `OperationLog`, snapshot encoding, admin views, and rollback support.
- Tests live in `dbtool/tests.py` and `audit/tests.py`; migrations stay inside each app's `migrations/` directory.
- SQLite is the Tool DB. The configured PostgreSQL database is the external Target DB and must not receive Django-managed schema migrations.

## Build, Test, and Development Commands

This project requires Python 3.13 and uses `uv` with the committed lockfile.

```powershell
uv sync --locked
uv run --env-file .env python manage.py migrate
uv run --env-file .env python manage.py runserver
uv run --env-file .env python manage.py test
uv run --env-file .env python manage.py check
docker compose up --build
```

`migrate` updates only the SQLite Tool DB. Docker runs migrations and `collectstatic` before starting Gunicorn.

## Coding Style & Naming Conventions

Use four-space indentation and conventional Python/Django naming: `snake_case` for functions and modules, `PascalCase` for classes, and descriptive template names. Keep views thin and reuse existing services. No formatter or linter is configured, so keep imports organized, avoid unrelated formatting changes, and run `manage.py check` before submission.

## Architecture and Safety Rules

Treat introspected PostgreSQL metadata as authoritative for physical constraints and semantic profiles as additional restrictions. Unknown or incomplete semantics must fail closed. Route all Target DB writes through `MutationService`; preserve stale-row checks, signed identities, snapshots, `OperationLog`, and rollback protections. Never add per-table Django models or mutate the Target schema. Do not expose credentials in logs, tests, screenshots, or commits.

## Testing Guidelines

Use Django `SimpleTestCase` for pure logic and `TestCase` for Tool DB behavior. Name tests `test_<expected_behavior>`. Mock `SchemaIntrospector` and Target DB repositories where practical; tests must not mutate a real PostgreSQL target. Add regression coverage for safety decisions, semantic status, mutation validation, routes, and redirects.

## Commit & Pull Request Guidelines

History uses short, focused subjects such as `add gitignore`; continue with concise imperative messages. Keep commits scoped to one behavior. Pull requests should describe user-visible changes, safety implications, migrations, and test commands run. Link relevant issues and include screenshots for editor or review-flow changes.
