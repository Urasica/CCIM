"""Environment file and command helpers for the admin UI."""

from __future__ import annotations

import os
import re
import shutil

from fastapi import HTTPException

from .config import ALLOWED_ENV_KEYS, DEFAULT_UV_EXE, DEFAULTS, ENV_PATH


def read_env_lines() -> list[str]:
    if not ENV_PATH.exists():
        return []
    return ENV_PATH.read_text(encoding="utf-8").splitlines()


def read_env_values() -> dict[str, str]:
    values = dict(DEFAULTS)
    for line in read_env_lines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if key in ALLOWED_ENV_KEYS:
            values[key] = value.strip()
    return values


def read_all_env_values() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in read_env_lines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def effective_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(read_all_env_values())
    env.update(read_env_values())
    return env


def uv_command() -> str:
    configured = os.environ.get("CCIM_UV_EXE", "").strip()
    if configured:
        return configured
    found = shutil.which("uv")
    if found:
        return found
    if DEFAULT_UV_EXE.exists():
        return str(DEFAULT_UV_EXE)
    return "uv"


def mask_secret_url(value: str) -> str:
    return re.sub(r":([^:@/]+)@", ":***@", value)


def write_env_values(new_values: dict[str, str]) -> None:
    unknown = sorted(set(new_values) - set(ALLOWED_ENV_KEYS))
    if unknown:
        raise HTTPException(status_code=400, detail=f"unsupported keys: {', '.join(unknown)}")

    merged = read_env_values()
    for key, value in new_values.items():
        merged[key] = str(value)

    lines = read_env_lines()
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out.append(line)
            continue
        key, _ = stripped.split("=", 1)
        key = key.strip()
        if key in ALLOWED_ENV_KEYS:
            out.append(f"{key}={merged[key]}")
            seen.add(key)
        else:
            out.append(line)

    if out and out[-1].strip():
        out.append("")
    for key in ALLOWED_ENV_KEYS:
        if key not in seen:
            out.append(f"{key}={merged[key]}")

    ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")
