"""Git-backed version storage and constrained source materialisation."""

import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
from urllib.parse import urlsplit

import yaml

ID = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SHA = re.compile(r"^[0-9a-f]{40}$")
CONFIG_NAMES = ("config.yaml", "config.yml", "config.json")
MARKER = ".dev-manager.json"


def repository_url(value):
    if not isinstance(value, str):
        raise ValueError("Repository must be a GitHub repository URL or owner/name")
    if "://" not in value:
        value = "https://github.com/" + value
    u = urlsplit(value)
    if u.scheme != "https" or u.netloc != "github.com" or u.query or u.fragment:
        raise ValueError("Use an HTTPS github.com repository URL without credentials or fragments")
    name = u.path.strip("/").removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", name):
        raise ValueError("Repository must be github.com/owner/name")
    if any(part in (".", "..") for part in name.split("/")):
        raise ValueError("Invalid GitHub repository")
    return "https://github.com/" + name + ".git"


def source_path(value):
    if not isinstance(value, str) or "\\" in value or ":" in value:
        raise ValueError("Path must be a relative repository subdirectory")
    path = PurePosixPath(value or ".")
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("Path must remain inside the repository")
    return str(path)


def targets_from_options(options):
    rows = options.get("repositories", [])
    if not isinstance(rows, list) or len(rows) > 50:
        raise ValueError("Configure at most 50 repositories")
    targets, ids = [], set()
    for row in rows:
        if not isinstance(row, dict) or not ID.fullmatch(str(row.get("id", ""))):
            raise ValueError("Each repository needs a unique ID using lowercase letters, numbers and underscores")
        ident = row["id"]
        if ident in ids:
            raise ValueError("Duplicate repository ID: " + ident)
        ids.add(ident)
        branch = row.get("branch", "main")
        if (not isinstance(branch, str) or not branch or len(branch) > 200
                or branch.startswith("-") or any(c in branch for c in ("\n", "\r", "\0"))):
            raise ValueError("Invalid branch")
        health_path = row.get("health_path", "/")
        if (not isinstance(health_path, str) or not health_path.startswith("/")
                or health_path.startswith("//") or any(c in health_path for c in "\r\n")):
            raise ValueError("Health path must start with a single slash")
        port = row.get("health_port", 0)
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("Health port must be 0 to 65535")
        for key in ("enabled", "update_on_start"):
            if key in row and type(row[key]) is not bool:
                raise ValueError(key + " must be true or false")
        targets.append(dict(id=ident, repository=repository_url(row["repository"]),
                            branch=branch, path=source_path(row.get("path", ".")),
                            enabled=row.get("enabled", True),
                            update_on_start=row.get("update_on_start", True),
                            health_port=port, health_path=health_path))
    return targets


class GitSource:
    def __init__(self, root, token=""):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.token = token

    def run(self, target, *args, binary=False):
        import base64
        env = os.environ.copy()
        # Neither credential-bearing remote URLs nor persistent Git config.
        env.update(GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null",
                   GIT_CONFIG_COUNT="2", GIT_CONFIG_KEY_0="protocol.file.allow",
                   GIT_CONFIG_VALUE_0="never", GIT_CONFIG_KEY_1="protocol.ext.allow", GIT_CONFIG_VALUE_1="never")
        if self.token:
            header = base64.b64encode(("x-access-token:" + self.token).encode()).decode()
            env.update(GIT_CONFIG_COUNT="3", GIT_CONFIG_KEY_2="http.https://github.com/.extraheader",
                       GIT_CONFIG_VALUE_2="Authorization: Basic " + header)
        result = subprocess.run(["git", "--git-dir", str(self.root / (target["id"] + ".git")), *args],
                                capture_output=True, timeout=180, env=env)
        if result.returncode:
            # Git may echo server-controlled text and secrets. Never return stderr.
            raise RuntimeError("Git operation failed. Check the repository, branch and GitHub token.")
        return result.stdout if binary else result.stdout.decode("utf-8", errors="replace").strip()

    def fetch(self, target):
        repo = self.root / (target["id"] + ".git")
        if not repo.exists():
            subprocess.run(["git", "init", "--bare", str(repo)], check=True, capture_output=True)
            self.run(target, "remote", "add", "origin", target["repository"])
        if self.run(target, "remote", "get-url", "origin") != target["repository"]:
            raise ValueError("This ID already belongs to another repository. Use a new ID.")
        self.run(target, "check-ref-format", "--branch", target["branch"])
        self.run(target, "fetch", "--prune", "origin", "+refs/heads/*:refs/remotes/origin/*", "+refs/tags/*:refs/tags/*")
        return self.run(target, "rev-parse", "--verify", "refs/remotes/origin/" + target["branch"] + "^{commit}")

    def pin(self, target, sha):
        if not SHA.fullmatch(sha):
            raise ValueError("Invalid commit")
        self.run(target, "update-ref", "refs/deployments/" + sha, sha)

    def versions(self, target):
        # Include commits already fetched, plus tags, without a network round trip.
        lines = self.run(target, "log", "--all", "--date=iso-strict", "--format=%H%x09%aI%x09%s", "-80")
        versions = []
        for line in lines.splitlines():
            sha, date, subject = line.split("\t", 2)
            versions.append(dict(sha=sha, date=date, subject=subject))
        return versions

    def tree_hash(self, target, sha):
        if not SHA.fullmatch(sha):
            raise ValueError("Invalid commit")
        path = source_path(target["path"])
        tree = sha + "^{tree}" if path == "." else sha + ":" + path
        return self.run(target, "rev-parse", "--verify", tree)

    def stage(self, target, sha, destination):
        if not SHA.fullmatch(sha):
            raise ValueError("Select a full commit SHA from version history")
        self.run(target, "cat-file", "-e", sha + "^{commit}")
        path = source_path(target["path"])
        tree = sha if path == "." else sha + ":" + path
        archive = self.run(target, "archive", "--format=tar", tree, binary=True)
        if len(archive) > 256 * 1024 * 1024:
            raise ValueError("Add-on source exceeds the 256 MiB limit")
        destination = Path(destination)
        destination.mkdir(parents=True)
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
            size = 0
            for member in tar:
                p = PurePosixPath(member.name)
                if p.is_absolute() or ".." in p.parts or member.issym() or member.islnk():
                    raise ValueError("Source archives must not contain links or paths outside the add-on")
                if not (member.isdir() or member.isfile()):
                    raise ValueError("Unsupported source archive entry")
                if any(part == ".git" for part in p.parts):
                    raise ValueError("Nested Git repositories are unsupported")
                if len(p.parts) > 1 and p.name in CONFIG_NAMES:
                    raise ValueError("Selected directory contains nested config files. Select the individual add-on directory.")
                size += member.size
                if size > 256 * 1024 * 1024:
                    raise ValueError("Add-on source exceeds the 256 MiB limit")
                out = destination.joinpath(*p.parts)
                if member.isdir():
                    out.mkdir(parents=True, exist_ok=True)
                else:
                    out.parent.mkdir(parents=True, exist_ok=True)
                    with tar.extractfile(member) as src, out.open("wb") as dst:
                        shutil.copyfileobj(src, dst)
                    out.chmod(member.mode & 0o777)
        configs = [destination / name for name in CONFIG_NAMES if (destination / name).is_file()]
        if len(configs) != 1 or not (destination / "Dockerfile").is_file():
            raise ValueError("Select an add-on directory containing one config file and a Dockerfile")
        cfg_path = configs[0]
        if cfg_path.stat().st_size > 1024 * 1024:
            raise ValueError("Configuration file is too large")
        config = yaml.safe_load(cfg_path.read_text())
        if not isinstance(config, dict) or not SLUG.fullmatch(str(config.get("slug", ""))):
            raise ValueError("Invalid add-on slug")
        if config["slug"] == "addon_dev_manager":
            raise ValueError("The controller cannot manage itself")
        for required in ("name", "description", "version", "arch"):
            if required not in config:
                raise ValueError("Missing add-on configuration key: " + required)
        version = str(config["version"])
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,99}", version):
            raise ValueError("Unsupported add-on version")
        # Every source commit is a distinct Supervisor version, including when
        # upstream forgot to bump its version. Downgrades also use /update.
        config["version"] = version + "-dev." + sha[:12]
        config.pop("image", None)  # Always build the selected source.
        cfg_path.write_text(json.dumps(config, indent=2) if cfg_path.suffix == ".json"
                            else yaml.safe_dump(config, sort_keys=False))
        identity = dict(id=target["id"], repository=target["repository"], path=target["path"], sha=sha,
                        slug="local_" + config["slug"], version=config["version"],
                        source_version=version, name=str(config["name"]), tree=self.tree_hash(target, sha))
        (destination / MARKER).write_text(json.dumps(identity))
        self.pin(target, sha)
        return identity
