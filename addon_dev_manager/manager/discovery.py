"""Discover Home Assistant add-ons using Git itself, not the GitHub REST API."""

import base64
import os
import re
import subprocess
import tempfile
from pathlib import Path

import yaml

from .source import ID, SLUG, repository_url

CONFIG_NAMES = ("config.yaml", "config.yml", "config.json")


def _full_name(value):
    normalised = repository_url(value)
    return normalised.removeprefix("https://github.com/").removesuffix(".git")


def _git_env(token=""):
    env = os.environ.copy()
    env.update(
        GIT_TERMINAL_PROMPT="0",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_CONFIG_COUNT="2",
        GIT_CONFIG_KEY_0="protocol.file.allow",
        GIT_CONFIG_VALUE_0="never",
        GIT_CONFIG_KEY_1="protocol.ext.allow",
        GIT_CONFIG_VALUE_1="never",
    )
    if token:
        header = base64.b64encode(("x-access-token:" + token).encode()).decode()
        env.update(
            GIT_CONFIG_COUNT="3",
            GIT_CONFIG_KEY_2="http.https://github.com/.extraheader",
            GIT_CONFIG_VALUE_2="Authorization: Basic " + header,
        )
    return env


def _run_git(git_dir, token, *args, binary=False):
    try:
        result = subprocess.run(
            ["git", "--git-dir", str(git_dir), *args],
            capture_output=True,
            timeout=90,
            env=_git_env(token),
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("Unable to run Git repository discovery.") from None
    if result.returncode:
        raise ValueError("Git repository discovery failed. Check the repository, branch and GitHub token.")
    return result.stdout if binary else result.stdout.decode("utf-8", errors="replace").strip()


def _manager_id(slug):
    value = re.sub(r"[^a-z0-9_]+", "_", slug.lower().replace("-", "_")).strip("_")
    if not value or not value[0].isalpha():
        value = "addon_" + value
    value = value[:40].rstrip("_")
    if not ID.fullmatch(value):
        raise ValueError("Add-on slug cannot be converted into a manager ID")
    return value


def _valid_branch(value):
    branch = value.strip() if isinstance(value, str) else ""
    branch = branch or "main"
    if (len(branch) > 200 or branch.startswith("-")
            or any(c in branch for c in ("\n", "\r", "\0"))):
        raise ValueError("Invalid branch")
    return branch


def discover_addons(repository, branch="", token=""):
    """Return valid add-on directories and metadata from one Git fetch."""
    full_name = _full_name(repository)
    selected_branch = _valid_branch(branch)
    remote = repository_url(full_name)

    with tempfile.TemporaryDirectory(prefix="devmgr-discovery-") as temporary:
        git_dir = Path(temporary) / "repo.git"
        init = subprocess.run(["git", "init", "--bare", str(git_dir)],
                              capture_output=True, timeout=30, env=_git_env(token))
        if init.returncode:
            raise ValueError("Unable to initialise repository discovery.")

        _run_git(git_dir, token, "remote", "add", "origin", remote)
        _run_git(
            git_dir, token, "fetch", "--depth=1", "--no-tags", "origin",
            f"refs/heads/{selected_branch}:refs/remotes/origin/{selected_branch}",
        )
        ref = "refs/remotes/origin/" + selected_branch
        files = set(filter(None, _run_git(git_dir, token, "ls-tree", "-r", "--name-only", ref).splitlines()))

        config_paths = []
        for path in files:
            if path in CONFIG_NAMES or any(path.endswith("/" + name) for name in CONFIG_NAMES):
                directory = path.rsplit("/", 1)[0] if "/" in path else "."
                dockerfile = "Dockerfile" if directory == "." else directory + "/Dockerfile"
                if dockerfile in files:
                    config_paths.append((directory, path))

        addons = []
        used_ids = set()
        for directory, config_path in sorted(config_paths):
            try:
                raw = _run_git(git_dir, token, "show", f"{ref}:{config_path}", binary=True)
                if len(raw) > 1024 * 1024:
                    continue
                config = yaml.safe_load(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError, yaml.YAMLError):
                continue
            if not isinstance(config, dict):
                continue

            slug = str(config.get("slug", ""))
            name = str(config.get("name", "")).strip()
            if not name or not SLUG.fullmatch(slug):
                continue
            if not all(key in config for key in ("description", "version", "arch")):
                continue

            ident = _manager_id(slug)
            base_ident = ident
            suffix = 2
            while ident in used_ids:
                tail = "_" + str(suffix)
                ident = base_ident[:40-len(tail)] + tail
                suffix += 1
            used_ids.add(ident)

            addons.append({
                "id": ident,
                "name": name,
                "slug": slug,
                "description": str(config.get("description", "")),
                "version": str(config.get("version", "")),
                "arch": config.get("arch", []),
                "repository": full_name,
                "branch": selected_branch,
                "path": directory,
                "health_port": 0,
                "health_path": "/",
            })

    return {
        "repository": full_name,
        "branch": selected_branch,
        "addons": addons,
    }
