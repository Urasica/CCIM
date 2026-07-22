$ErrorActionPreference = "Stop"

$taskUvPath = $env:CCIM_UV_EXE
if ([string]::IsNullOrWhiteSpace($taskUvPath)) {
    $taskUvCommand = Get-Command uv -ErrorAction SilentlyContinue
    if ($null -eq $taskUvCommand) {
        throw "uv executable not found; add uv to PATH or set CCIM_UV_EXE"
    }
    $taskUvPath = $taskUvCommand.Source
} else {
    $taskUvPath = (Resolve-Path -LiteralPath $taskUvPath).Path
}

& $taskUvPath lock --check
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $taskUvPath sync --locked --all-extras --all-groups
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $taskUvPath run --no-sync ruff check src tests tools/admin_ui tests/compare scripts
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $taskUvPath run --no-sync python scripts/check_markdown_links.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $taskUvPath run --no-sync pytest tests/unit -q
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $taskUvPath run --no-sync pytest tests/integration -m "not integration and not ollama" -q
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $taskUvPath run --no-sync python tests/compare/check_task2_semantics.py tests/fixtures/task2_golden
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

git diff --check
exit $LASTEXITCODE
