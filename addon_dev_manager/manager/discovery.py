"""GitHub repository discovery for Home Assistant add-on directories."""

import base64
import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

import yaml

from .source import ID, SLUG, repository_url

API = "https://api.github.com"
CONFIG_NAMES = ("config.yaml", "config.yml", "config.json")


def _full_name(value):
    normalised = repository_url(value)
    return normalised.removeprefix("https://github.com/").removesuffix(".git")


def _github_json(path, token=""):
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "ha-addon-dev-manager",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    request = Request(API + path, headers=headers)
    try:
        with urlopen(request, timeout=20) as response:
            return json.load(response)
    except HTTPError as exc:
        if exc.code == 404:
            raise ValueError("Repository, branch or file was not found. Check the repository and GitHub token.") from None
        if exc.code in (401, 403):
            raise ValueError("GitHub denied access. Check the token and its repository permissions.") from None
        raise ValueError("GitHub repository discovery failed with HTTP " + str(exc.code)) from None
    except (URLError, TimeoutError, json.JSONDecodeError):
        raise ValueError("Unable to contact GitHub for repository discovery.") from None


def _manager_id(slug):
    value = re.sub(r"[^a-z0-9_]+", "_", slug.lower().replace("-", "_")).strip("_")
    if not value or not value[0].isalpha():
        value = "addon_" + value
    value = value[:40].rstrip("_")
    if not ID.fullmatch(value):
        raise ValueError("Add-on slug cannot be converted into a manager ID")
    return value


def _read_config(full_name, path, branch, token):
    encoded_path = quote(path, safe="/")
    payload = _github_json(
        f"/repos/{full_name}/contents/{encoded_path}?ref={quote(branch, safe='')}", token
    )
    if payload.get("type") != "file" or payload.get("encoding") != "base64":
        raise ValueError("Add-on configuration could not be read")
    raw = base64.b64decode(payload.get("content", ""), validate=False)
    if len(raw) > 1024 * 1024:
        raise ValueError("Add-on configuration is too large")
    config = yaml.safe_load(raw.decode("utf-8"))
    if not isinstance(config, dict):
        raise ValueError("Add-on configuration is not a mapping")
    return config


def discover_addons(repository, branch="", token=""):
    """Return valid add-on directories and the metadata needed by the manager."""
    full_name = _full_name(repository)
    meta = _github_json(f"/repos/{full_name}", token)
    selected_branch = branch.strip() if isinstance(branch, str) else ""
    selected_branch = selected_branch or meta.get("default_branch") or "main"
    if (len(selected_branch) > 200 or selected_branch.startswith("-")
            or any(c in selected_branch for c in ("\n", "\r", "\0"))):
        raise ValueError("Invalid branch")

    tree = _github_json(
        f"/repos/{full_name}/git/trees/{quote(selected_branch, safe='')}?recursive=1", token
    )
    if tree.get("truncated"):
        raise ValueError("Repository tree is too large for automatic discovery. Add the entry manually.")

    files = {
        item.get("path") for item in tree.get("tree", [])
        if item.get("type") == "blob" and isinstance(item.get("path"), str)
    }
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
            config = _read_config(full_name, config_path, selected_branch, token)
        except (ValueError, UnicodeDecodeError, yaml.YAMLError):
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

        ingress_port = config.get("ingress_port", 0)
        if type(ingress_port) is not int or not 0 <= ingress_port <= 65535:
            ingress_port = 0
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
            "health_port": ingress_port,
            "health_path": "/",
        })

    return {
        "repository": full_name,
        "branch": selected_branch,
        "addons": addons,
    }
