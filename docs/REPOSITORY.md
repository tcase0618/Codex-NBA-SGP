# Repository and runtime boundaries

`Codex-NBA-SGP` contains the complete application source, frontend assets, metric catalog, tests, acquisition/review/training scripts and documentation. It is an independent Git repository; sibling Case Capital projects are not included.

Local SQLite files, provider-response archives, generated reports, trained model artifacts, temporary files, credentials and Python environments are not source files and are excluded. They remain intact on the original machine. A fresh clone intentionally starts without private runtime data or pretrained production models. Historical counts and experiments in the engineering report describe the original dated research environment, not data included in a fresh clone.

## Fresh clone

```powershell
git clone https://github.com/tcase0618/Codex-NBA-SGP.git
cd Codex-NBA-SGP
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[test]'
.\.venv\Scripts\python.exe -m nba_quant serve --port 8020
```

The empty terminal is valid. For an explicitly synthetic demonstration, run `python -m nba_quant demo` using the selected environment. Real-data operation requires importing verified canonical datasets or acquiring supported public sources; no missing history, price or trained model is invented.

For optional CPU ML, create `.venv-ml` and install `.[ml,test]`. Start-NBAQuant.ps1 prefers that interpreter when present. The Windows dependency snapshot is docs/requirements-ml-windows.lock.txt; it records one measured environment rather than promising identical compatibility on every OS.

PDF authoring scripts require `.[reports]`. Some dated analysis/export scripts refer to their original output archives and require those inputs before running. They are historical workflows, not automatic fresh-clone startup commands. Run the general scripts' `--help` where supported and consult the engineering report for core training/serving commands.

## Existing local state

To move the existing private environment to another trusted machine, copy the required SQLite database and relevant output archives/model directories separately over a private channel. Stop the server or use SQLite's backup API to obtain a consistent database copy; do not assume copying an active database without its WAL is a valid backup. Preserve original source hashes, observation timestamps and model receipts. Set NBA_QUANT_DB and NBA_QUANT_ML_ROOT as process environment variables when using nondefault paths. Never load untrusted AutoGluon pickle artifacts.

The application reads process environment variables; `.env.example` is reference documentation, not an automatic dotenv loader. The Odds API credential is optional. Do not place real credentials in tracked files.

## Validation

```powershell
python -m pytest -q
node --check nba_quant/static/app.js
python scripts/check_dashboard.py
```

CI runs those checks on Windows and Linux using fixture data. It does not acquire live odds, train heavy models, place bets, prove real-world predictive accuracy, or deploy production services. The optional actual-package ML acceptance script is `scripts/check_ml_runtime.py`; its generated fixture results are software evidence only.

Model outputs remain shadow research until the documented prospective validation gates are met. There is no order execution or guaranteed SGP hit rate.
