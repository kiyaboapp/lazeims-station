# lazeims-station

LAZEIMS offline LAN **station** — a small FastAPI + SQLite server that runs on an
ordinary PC at a marking centre so several Data Enterers on the same LAN can
transcribe attendance and enter marks with **no internet**. Entry + sync only;
it never creates exams or processes results.

Validation rules are imported from `lazeims-common` (never re-implemented).

## One-click run

```bash
./start.sh        # Linux/macOS
start.bat         # Windows (double-click)
```

The launcher creates a local `.venv`, installs pinned deps (from a bundled
`wheelhouse/` when offline), runs SQLite migrations, and starts the server on
`0.0.0.0:8080`, printing the LAN URL. Later launches need no network.

## Package import

Central generates a scope-only package bundle (`{manifest, seed}`). Import it:

```bash
curl -X POST http://127.0.0.1:8080/api/import -H 'Content-Type: application/json' -d @package.json
```

Import verifies the package targets this station/exam, that the rules/software
versions are supported, and that the seed matches the manifest
`configuration_hash` (integrity — no shared secret needed), then seeds SQLite in
one transaction. Wrong-target/version packages are rejected with a stable code.

## Local auth

- **Data Enterer:** PIN + initials.
- **Station Exam Admin:** password.

Only salted Argon2 hashes (shipped inside the package) are stored. Sessions are
signed, HttpOnly cookies; role/scope derive from the local user row.

## Tests

```bash
pytest
```
