"""No-container-rebuild path for pin-verified Flask static assets.

This is intentionally conservative: only known static file extensions can use
it, and only apps which already run with read-only local_apps and opt into
LiveAssets. All other changes follow the normal Supervisor build path.
"""
import json
import os
from pathlib import Path
import shutil

import yaml

from .source import MARKER


ASSET_SUFFIXES = {".css", ".js", ".png", ".jpg", ".jpeg", ".webp",
                  ".svg", ".ico", ".woff", ".woff2", ".webmanifest", ".json"}
EXCLUDED_FILES = {"app/static/assetlinks.json"}
NEXT = ".devmgr-static-next"
PREVIOUS = ".devmgr-static-previous"


def allowed_paths(paths):
    return bool(paths) and all(
        path.startswith("app/static/")
        and Path(path).suffix.lower() in ASSET_SUFFIXES
        and path not in EXCLUDED_FILES
        for path in paths
    )


def live_capable(folder, current):
    """Require a running image built from our live-static contract."""
    folder = Path(folder)
    config = folder / "config.yaml"
    contract = folder / "app" / "live_assets.py"
    static = folder / "app" / "static"
    if not config.is_file() or not contract.is_file() or not static.is_dir():
        return False
    try:
        definition = yaml.safe_load(config.read_text())
        marker = json.loads((folder / MARKER).read_text())
    except (OSError, ValueError, yaml.YAMLError):
        return False
    mounts = definition.get("map", []) if isinstance(definition, dict) else []
    readonly_source = any(
        isinstance(row, dict) and row.get("type") in ("local_apps", "addons")
        and row.get("read_only", True) is True
        for row in mounts
    )
    return (readonly_source
            and marker.get("sha") == current["sha"]
            and marker.get("slug") == current["slug"])


def _sync_parent(folder):
    fd = os.open(str(folder), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_marker(folder, identity):
    marker = Path(folder) / MARKER
    pending = marker.with_suffix(".tmp")
    with pending.open("w") as stream:
        json.dump(identity, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(pending, marker)
    _sync_parent(folder)


def publish(folder, staging, candidate):
    """Stage complete static assets, swap folders, write marker LAST.

    Never overwrite a prior interrupted static transaction: it is a recovery
    checkpoint. A failed copy leaves the existing static files untouched.
    """
    folder, staging = Path(folder), Path(staging)
    app = folder / "app"
    static, next_dir, backup = app / "static", app / NEXT, app / PREVIOUS
    source = staging / "app" / "static"
    if not source.is_dir() or source.is_symlink() or not static.is_dir():
        raise ValueError("Live frontend source is missing; use a full rebuild")
    if next_dir.exists() or backup.exists():
        raise ValueError("Previous live update needs recovery")
    shutil.copytree(source, next_dir, symlinks=False)
    try:
        os.replace(static, backup)
        try:
            os.replace(next_dir, static)
            _sync_parent(app)
            _write_marker(folder, candidate)
        except BaseException:
            if static.is_dir():
                shutil.rmtree(static)
            os.replace(backup, static)
            _sync_parent(app)
            raise
    finally:
        if next_dir.exists():
            shutil.rmtree(next_dir)


def finish(folder):
    backup = Path(folder) / "app" / PREVIOUS
    if backup.exists():
        shutil.rmtree(backup)


def restore(folder, identity):
    """Undo an interrupted live update without touching a running container."""
    folder = Path(folder)
    app = folder / "app"
    backup, static = app / PREVIOUS, app / "static"
    if not backup.is_dir():
        raise ValueError("No live frontend checkpoint. Inspect the source before recovery")
    if static.is_dir():
        shutil.rmtree(static)
    os.replace(backup, static)
    _write_marker(folder, identity)
    pending = app / NEXT
    if pending.exists():
        shutil.rmtree(pending)
    _sync_parent(app)
