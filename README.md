# AutoCurator

AutoCurator finds a curated GitHub issue to work on, with lightweight local state and interactive save/skip actions.

## Setup

1. Create and activate your virtual environment.
2. Install dependencies:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

## Environment

Create a local `.env` file (already supported by the app):

```env
GITHUB_TOKEN=your_github_token_here
```

Check token/auth and API rate-limit status:

```powershell
.\.venv\Scripts\python.exe autocurator.py auth
```

## Troubleshooting

- `Auth status : token invalid (401)`
	- Your token is expired/revoked/incorrect. Regenerate it in GitHub and update `.env`.
	- Verify: `.\.venv\Scripts\python.exe autocurator.py auth`
- `Rate limited by GitHub. Add GITHUB_TOKEN.`
	- Set `GITHUB_TOKEN` in `.env`, then run `autocurator.py auth` to confirm authenticated status.
	- Verify: `.\.venv\Scripts\python.exe autocurator.py auth`
- `GitHub token loaded: no`
	- Ensure `.env` is in the project root and contains `GITHUB_TOKEN=...` (no quotes needed).
	- Verify load: `.\.venv\Scripts\python.exe -c "import autocurator; print(bool(autocurator.GITHUB_TOKEN))"`
- Token changed but command still shows old status
	- Restart the terminal/session and rerun the command.
	- Re-check: `.\.venv\Scripts\python.exe autocurator.py auth`

Quick full health check:

```powershell
.\.venv\Scripts\python.exe autocurator.py auth; .\.venv\Scripts\ruff.exe check .; .\.venv\Scripts\pytest.exe -q
```

## Usage

```powershell
.\.venv\Scripts\python.exe autocurator.py next
.\.venv\Scripts\python.exe autocurator.py saved
.\.venv\Scripts\python.exe autocurator.py readme
.\.venv\Scripts\python.exe autocurator.py config
.\.venv\Scripts\python.exe autocurator.py config --reset
.\.venv\Scripts\python.exe autocurator.py config --set min_stars 10 --set updated_within_days 90
.\.venv\Scripts\python.exe autocurator.py diagnose
.\.venv\Scripts\python.exe autocurator.py autotune
.\.venv\Scripts\python.exe autocurator.py autotune --dry-run
```

## Quality checks

```powershell
.\.venv\Scripts\black.exe .
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\pytest.exe -q
```

Or run the VS Code task: `Quality: format + lint + test`.
