$ErrorActionPreference = "Stop"

Write-Host "[1/6] Ruff lint"
# uv経由で起動し、.venv未作成のクリーン環境でも再現できるようにする。
uv run python scripts\export-openapi.py frontend\src\api\openapi.json
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Push-Location frontend
try {
    npm run api:generate
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
    Pop-Location
}

uv run ruff check backend tests
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "[2/6] Ruff format"
uv run ruff format --check backend tests
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "[3/6] mypy"
uv run mypy backend
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "[4/6] Backend tests and coverage"
uv run pytest --cov=backend --cov-report=term-missing --cov-report=xml --cov-report=html
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "[5/6] Frontend quality"
Push-Location frontend
try {
    npm run check
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
    Pop-Location
}

Write-Host "[6/6] Git whitespace"
git diff --check
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
