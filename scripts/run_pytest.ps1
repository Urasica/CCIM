param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$PytestArgs = @("tests/unit", "-q")
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$VenvCfg = Join-Path $RepoRoot ".venv\pyvenv.cfg"
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$SitePackages = Join-Path $RepoRoot ".venv\Lib\site-packages"
$SrcDir = Join-Path $RepoRoot "src"

function Get-BasePython {
    param([string]$PyvenvCfgPath, [string]$FallbackPython)

    if (Test-Path $PyvenvCfgPath) {
        $homeLine = Get-Content $PyvenvCfgPath | Where-Object { $_ -like "home = *" } | Select-Object -First 1
        if ($homeLine) {
            $homeDir = $homeLine.Substring("home = ".Length).Trim()
            if ($homeDir) {
                $candidate = Join-Path $homeDir "python.exe"
                if (Test-Path $candidate) {
                    return $candidate
                }
            }
        }
    }

    if (Test-Path $FallbackPython) {
        return $FallbackPython
    }

    throw "Python interpreter not found. Checked pyvenv.cfg and $FallbackPython"
}

$PythonExe = Get-BasePython -PyvenvCfgPath $VenvCfg -FallbackPython $VenvPython

if (-not (Test-Path $SitePackages)) {
    throw "site-packages not found: $SitePackages"
}
if (-not (Test-Path $SrcDir)) {
    throw "src directory not found: $SrcDir"
}

$PytestArgsJson = ConvertTo-Json -Compress $PytestArgs
$Bootstrap = @"
import json
import os
import sys

repo_root = r"$RepoRoot"
site_packages = r"$SitePackages"
src_dir = r"$SrcDir"

os.chdir(repo_root)
sys.path.insert(0, site_packages)
sys.path.insert(0, src_dir)

import pytest

args = json.loads(r'''$PytestArgsJson''')
raise SystemExit(pytest.main(args))
"@

$Bootstrap | & $PythonExe -
