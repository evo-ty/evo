#!/usr/bin/env python3
"""Discover and install Python dependencies for a EVO HUB workspace.

This helper only uses the Python standard library. It supports common dependency
manifests and falls back to AST-based import discovery when a project does not
ship requirements.txt.
"""
from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

IMPORT_TO_PACKAGE = {
    "PIL": "Pillow",
    "bs4": "beautifulsoup4",
    "cv2": "opencv-python",
    "dotenv": "python-dotenv",
    "flask": "Flask",
    "flask_cors": "flask-cors",
    "flask_socketio": "Flask-SocketIO",
    "jwt": "PyJWT",
    "Crypto": "pycryptodome",
    "sklearn": "scikit-learn",
    "telegram": "python-telegram-bot",
    "discord": "discord.py",
    "socketio": "python-socketio",
    "yaml": "PyYAML",
    "dateutil": "python-dateutil",
    "googleapiclient": "google-api-python-client",
    "google": "google-api-python-client",
    "lxml": "lxml",
    "numpy": "numpy",
    "pandas": "pandas",
    "psutil": "psutil",
    "requests": "requests",
    "selenium": "selenium",
    "sqlalchemy": "SQLAlchemy",
    "uvicorn": "uvicorn",
    "fastapi": "fastapi",
}

SKIP_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", ".env",
    ".packages", ".evo", "node_modules", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".zip-staging-",
}


def log(message: str) -> None:
    print(f"[EVO-DEPS] {message}", flush=True)


def iter_python_files(root: Path):
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".zip-staging-")]
        for name in files:
            if name.endswith(".py"):
                yield Path(current) / name


def local_module_names(root: Path) -> set[str]:
    names: set[str] = set()
    for path in iter_python_files(root):
        names.add(path.stem)
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        if "__init__.py" in files:
            names.add(Path(current).name)
    return names


def imported_modules(root: Path) -> set[str]:
    modules: set[str] = set()
    for path in iter_python_files(root):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"), filename=str(path))
        except (OSError, SyntaxError, ValueError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules.add(node.module.split(".", 1)[0])
    return modules


def versioned_spec(name: str, value) -> str:
    if isinstance(value, str):
        value = value.strip()
        if not value or value == "*":
            return name
        if value.startswith(("=", "!", "~", ">", "<")):
            return name + value
        return name
    if isinstance(value, dict):
        version = str(value.get("version") or "").strip()
        extras = value.get("extras") or []
        extra_text = "[" + ",".join(str(x) for x in extras) + "]" if extras else ""
        if version and version != "*":
            if not version.startswith(("=", "!", "~", ">", "<")):
                version = ">=" + version
            return name + extra_text + version
        return name + extra_text
    return name


def add_toml_packages(specs: list[str], section) -> None:
    if not isinstance(section, dict):
        return
    for name, value in section.items():
        if name.startswith("_"):
            continue
        specs.append(versioned_spec(str(name), value))


def discover_plan(root: Path):
    manifests: list[Path] = []
    specs: list[str] = []
    install_project = False

    direct = root / "requirements.txt"
    if direct.is_file():
        manifests.append(direct)
    for path in sorted(root.glob("requirements*.txt")):
        if path.is_file() and path not in manifests:
            manifests.append(path)
    req_dir = root / "requirements"
    if req_dir.is_dir():
        manifests.extend(path for path in sorted(req_dir.glob("*.txt")) if path.is_file())

    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            project = data.get("project", {})
            specs.extend(str(x) for x in project.get("dependencies", []) if str(x).strip())
            for optional in (project.get("optional-dependencies") or {}).values():
                specs.extend(str(x) for x in optional if str(x).strip())
            specs.extend(str(x) for x in (data.get("build-system", {}) or {}).get("requires", []) if str(x).strip())
            install_project = bool(project.get("name"))
        except (OSError, tomllib.TOMLDecodeError):
            log("Could not parse pyproject.toml; continuing with import discovery.")

    pipfile = root / "Pipfile"
    if pipfile.is_file():
        try:
            data = tomllib.loads(pipfile.read_text(encoding="utf-8"))
            add_toml_packages(specs, data.get("packages"))
            add_toml_packages(specs, data.get("dev-packages"))
        except (OSError, tomllib.TOMLDecodeError):
            log("Could not parse Pipfile; continuing with import discovery.")

    lockfile = root / "Pipfile.lock"
    if lockfile.is_file():
        try:
            data = json.loads(lockfile.read_text(encoding="utf-8"))
            add_toml_packages(specs, data.get("default"))
            add_toml_packages(specs, data.get("develop"))
        except (OSError, json.JSONDecodeError):
            log("Could not parse Pipfile.lock; continuing with import discovery.")

    if (root / "setup.py").is_file() or (root / "setup.cfg").is_file():
        install_project = True

    stdlib = set(getattr(sys, "stdlib_module_names", set()))
    local = local_module_names(root)
    inferred = []
    for module in sorted(imported_modules(root)):
        if not module or module in stdlib or module in local or module.startswith("_"):
            continue
        inferred.append(IMPORT_TO_PACKAGE.get(module, module))

    all_specs = []
    seen = set()
    for spec in specs + inferred:
        spec = str(spec).strip()
        if spec and spec not in seen:
            seen.add(spec)
            all_specs.append(spec)
    return manifests, all_specs, install_project


def run_pip(python: str, args: list[str], root: Path, timeout: int) -> bool:
    command = [python, "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "--no-cache-dir"] + args
    log("Running: " + " ".join(command[3:]))
    try:
        result = subprocess.run(command, cwd=root, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        log("pip command timed out: " + " ".join(args[:2]))
        return False
    return result.returncode == 0


def prepare_venv(root: Path, venv: Path, fallback_dir: Path | None = None):
    python = venv / "bin" / "python"
    if not python.is_file():
        log("Creating isolated Python environment.")
        result = subprocess.run([sys.executable, "-m", "venv", str(venv)], cwd=root, text=True)
        if result.returncode != 0:
            return None, None
    if not python.is_file():
        return None, None
    try:
        probe = subprocess.run([str(python), "-m", "pip", "--version"], cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        probe = None
    if probe is not None and probe.returncode == 0:
        return str(python), None
    try:
        subprocess.run([str(python), "-m", "ensurepip", "--upgrade"], cwd=root, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        probe = subprocess.run([str(python), "-m", "pip", "--version"], cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if probe.returncode == 0:
            return str(python), None
    except OSError:
        pass
    packages = fallback_dir or (root / ".packages")
    packages.mkdir(parents=True, exist_ok=True)
    return sys.executable, str(packages)


def write_env(path: Path, python: str, pythonpath: str | None) -> None:
    lines = [f"EVO_PYTHON={python}"]
    if pythonpath:
        lines.append(f"EVO_PYTHONPATH={pythonpath}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--venv", required=True)
    parser.add_argument("--env-file")
    parser.add_argument("--fallback-dir")
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    venv = Path(args.venv).resolve()
    if not root.is_dir() or root == Path("/"):
        log("Invalid workspace root.")
        return 2

    fallback_dir = Path(args.fallback_dir).resolve() if args.fallback_dir else None
    python, fallback_path = prepare_venv(root, venv, fallback_dir)
    if not python:
        log("Could not prepare the isolated Python environment.")
        return 2
    manifests, specs, install_project = discover_plan(root)
    log(f"Found {len(manifests)} requirement manifest(s) and {len(specs)} package spec(s) from imports/configuration.")
    failed = False
    for manifest in manifests:
        args_list = ["--target", fallback_path, "-r", str(manifest)] if fallback_path else ["-r", str(manifest)]
        if not run_pip(python, args_list, root, args.timeout):
            failed = True
            log(f"Dependency manifest failed: {manifest.name}")
    if install_project:
        args_list = ["--target", fallback_path, "."] if fallback_path else ["."]
        if not run_pip(python, args_list, root, args.timeout):
            failed = True
            log("Installing the local Python project failed; continuing with discovered packages.")
    for spec in specs:
        args_list = ["--target", fallback_path, spec] if fallback_path else [spec]
        if not run_pip(python, args_list, root, args.timeout):
            failed = True
            log(f"Could not install inferred package: {spec}")

    if args.env_file:
        write_env(Path(args.env_file), python, fallback_path)
    if failed:
        log("One or more dependencies could not be installed. Check the log for exact package names.")
        return 1
    log("All discovered Python dependencies are ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
