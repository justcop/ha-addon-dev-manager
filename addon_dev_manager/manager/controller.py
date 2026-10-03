"""Serial deployment transactions with durable journals and Git-based recovery."""

from copy import deepcopy
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
from urllib.error import URLError
from urllib.request import urlopen, build_opener, ProxyHandler, HTTPRedirectHandler
import ipaddress

import yaml

from .source import GitSource, MARKER, CONFIG_NAMES, targets_from_options
from .supervisor import SupervisorError, UncertainMutation

LOG = logging.getLogger(__name__)


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    path = Path(path)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as stream:
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)
    fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Controller:
    def __init__(self, data_dir, addons_dir, options, supervisor):
        self.data_dir, self.addons_dir = Path(data_dir), Path(addons_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.addons_dir.mkdir(parents=True, exist_ok=True)
        self.options = deepcopy(options)
        self.supervisor = supervisor
        self.targets = targets_from_options(options)
        self.source = GitSource(self.data_dir / "git", options.get("github_token", ""))
        self.state_path = self.data_dir / "state.json"
        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text())
        else:
            self.state = dict(current={}, transactions={}, history=[], sequence=0)
        self.lock = threading.RLock()
        self.busy = False
        self.job = dict(status="idle", phase="Ready", events=[])
        self._validate_bindings(self.targets)

    def _validate_bindings(self, targets):
        for target in targets:
            existing = self.state["current"].get(target["id"])
            if existing and any(existing[key] != target[key] for key in ("repository", "path")):
                raise ValueError("A deployed ID cannot change repository or path. Use a new ID.")

    def save(self):
        with self.lock:
            atomic_json(self.state_path, self.state)

    def event(self, message):
        with self.lock:
            self.job["phase"] = message
            self.job["events"].append(dict(time=now(), message=message))
            self.job["events"] = self.job["events"][-150:]
        LOG.info(message)

    def target(self, ident):
        for t in self.targets:
            if t["id"] == ident:
                return deepcopy(t)
        raise ValueError("Unknown configured add-on")

    def snapshot(self):
        with self.lock:
            return dict(targets=deepcopy(self.targets), current=deepcopy(self.state["current"]),
                        transactions=deepcopy(self.state["transactions"]),
                        history=deepcopy(self.state["history"]), job=deepcopy(self.job), busy=self.busy,
                        settings={"update_on_start": self.options.get("update_on_start", True),
                                  "stop_timeout": self.options.get("stop_timeout", 60),
                                  "health_timeout": self.options.get("health_timeout", 120),
                                  "token_configured": bool(self.source.token)})

    def configure(self, rows, update_on_start):
        with self.lock:
            if self.busy or self.state["transactions"]:
                raise ValueError("Finish or recover the active deployment before changing repositories")
            options = deepcopy(self.options)
            options.update(repositories=rows, update_on_start=update_on_start)
            targets = targets_from_options(options)
            self._validate_bindings(targets)
            # Supervisor owns options.json, not the controller. Persist through its API.
            self.supervisor.post("/addons/self/options", {"options": options})
            self.options, self.targets = options, targets

    def submit(self, action, ident=None, sha=None):
        with self.lock:
            if self.busy:
                raise ValueError("Another operation is already running")
            if action not in ("deploy_all", "startup", "deploy", "check", "start", "stop", "restart", "rollback", "recover"):
                raise ValueError("Unknown action")
            if action not in ("deploy_all", "startup"):
                self.target(ident)
            if self.state["transactions"] and action not in ("recover", "rollback"):
                raise ValueError("Recover interrupted deployments before performing other operations")
            self.busy = True
            self.job = dict(status="running", phase="Starting", action=action, started=now(), events=[])
        threading.Thread(target=self._worker, args=(action, ident, sha), daemon=True).start()

    def _worker(self, action, ident, sha):
        try:
            if action in ("deploy_all", "startup"):
                self.deploy_batch([t for t in self.targets if t["enabled"]
                                   and (action != "startup" or t["update_on_start"])])
            elif action == "deploy":
                self.deploy_batch([self.target(ident)], sha)
            elif action == "check":
                latest = self.source.fetch(self.target(ident))
                self.event("Latest commit: " + latest[:12])
            elif action in ("recover", "rollback"):
                self.recover(self.target(ident)) if ident in self.state["transactions"] else self.rollback(self.target(ident))
            else:
                target = self.target(ident)
                current = self.state["current"].get(ident)
                if not current:
                    raise ValueError("Deploy this add-on first")
                self.assert_owned(target, current)
                self.supervisor.ensure_idle(current["slug"])
                self.event(action.title() + " " + ident)
                self.supervisor.post(f"/addons/{current['slug']}/{action}")
                self.supervisor.wait_state(current["slug"], "stopped" if action == "stop" else "started", 120)
            with self.lock:
                self.job.update(status="success", phase="Complete", finished=now())
        except Exception as exc:
            self.event(str(exc))
            with self.lock:
                self.job.update(status="failed", error=str(exc), finished=now())
            LOG.exception("Controller operation failed")
        finally:
            with self.lock:
                self.busy = False

    def assert_owned(self, target, current=None):
        path = self.addons_dir / ("devmgr_" + target["id"])
        if path.is_symlink():
            raise ValueError("Managed add-on directory must not be a symlink")
        if path.exists():
            marker_path = path / MARKER
            if marker_path.is_symlink() or not marker_path.is_file():
                tx = self.state["transactions"].get(target["id"])
                if tx and tx["phase"] == "publishing" and not any(path.iterdir()):
                    # Interrupted between mkdir and writing the ownership marker.
                    return path
                raise ValueError("Refusing to overwrite an unmanaged add-on directory")
            marker = json.loads(marker_path.read_text())
            if any(marker.get(key) != target[key] for key in ("id", "repository", "path")):
                raise ValueError("Managed directory ownership does not match configured repository")
            if current and any(marker.get(key) != current[key] for key in ("slug", "sha", "version")):
                raise ValueError("Managed add-on identity does not match deployment history")
        elif current:
            raise ValueError("Managed source directory is missing. Use Recover to restore it.")
        return path

    def record(self, target, identity, status, message=""):
        with self.lock:
            self.state["sequence"] += 1
            self.state["history"].insert(0, dict(number=self.state["sequence"], time=now(),
                                                 status=status, message=message, **identity))
            self.state["history"] = self.state["history"][:200]
            self.save()

    def _publish(self, target, staging):
        """Only replace source we own. Publish config last to avoid partial discovery."""
        destination = self.addons_dir / ("devmgr_" + target["id"])
        if destination.is_symlink():
            raise ValueError("Refusing to follow an add-on directory symlink")
        if destination.exists():
            self.assert_owned(target)
            shutil.rmtree(destination)
        destination.mkdir()
        # Put the ownership marker first so interrupted copies remain recoverable.
        shutil.copy2(staging / MARKER, destination / MARKER)
        for entry in staging.iterdir():
            if entry.name in CONFIG_NAMES or entry.name == MARKER:
                continue
            if entry.is_dir():
                shutil.copytree(entry, destination / entry.name)
            else:
                shutil.copy2(entry, destination / entry.name)
        config = next(staging / name for name in CONFIG_NAMES if (staging / name).exists())
        tmp = destination / ".config-pending"
        shutil.copy2(config, tmp)
        os.replace(tmp, destination / config.name)

    def _stop(self, target, tx):
        slug = tx["candidate"]["slug"]
        if not tx["installed"]:
            return
        self.supervisor.ensure_idle(slug)
        if tx["watchdog"]:
            # Otherwise Supervisor can restart the target during file replacement.
            self.supervisor.post(f"/addons/{slug}/options", {"watchdog": False})
        self.event("Stopping " + target["id"])
        self.supervisor.post(f"/addons/{slug}/stop")
        self.supervisor.wait_state(slug, "stopped", self.options.get("stop_timeout", 60))

    def _journal(self, target, candidate, installed):
        info = self.supervisor.info(candidate["slug"]) if installed else {}
        tx = dict(target=target, candidate=candidate,
                  previous=deepcopy(self.state["current"].get(target["id"])),
                  installed=installed, was_started=info.get("state") in ("started", "startup"),
                  watchdog=info.get("watchdog", False), phase="prepared", time=now())
        with self.lock:
            self.state["transactions"][target["id"]] = tx
            self.save()
        return tx

    def phase(self, target, value):
        with self.lock:
            self.state["transactions"][target["id"]]["phase"] = value
            self.save()

    def _build(self, identity):
        slug = identity["slug"]
        installed = self.supervisor.installed()
        if slug not in installed:
            self.supervisor.post(f"/store/addons/{slug}/install", {})
        elif self.supervisor.info(slug)["version"] != identity["version"]:
            # Supervisor rejects rebuild when store and installed versions differ.
            self.supervisor.post(f"/store/addons/{slug}/update", {"backup": False})
        else:
            self.supervisor.post(f"/addons/{slug}/rebuild", {})
        info = self.supervisor.info(slug)
        if info.get("version") != identity["version"]:
            raise SupervisorError("Supervisor did not install the requested code version")
        validation = self.supervisor.post(f"/addons/{slug}/options/validate", {})
        if validation.get("valid") is not True:
            # Supervisor's detailed schema errors can contain settings/secrets.
            raise SupervisorError("Existing add-on settings are incompatible with the selected version. Check Home Assistant configuration before recovery.")

    def _prepare_definition(self, target, staging, candidate, current):
        """Keep an unchanged installed definition so code-only rebuilds need no reload."""
        if not current:
            return True
        directory = self.addons_dir / ("devmgr_" + target["id"])
        old_configs = [directory / n for n in CONFIG_NAMES if (directory / n).is_file()]
        new_configs = [staging / n for n in CONFIG_NAMES if (staging / n).is_file()]
        if len(old_configs) != 1 or len(new_configs) != 1 or old_configs[0].name != new_configs[0].name:
            return True
        old, new = yaml.safe_load(old_configs[0].read_text()), yaml.safe_load(new_configs[0].read_text())
        old.pop("version", None)
        new.pop("version", None)
        if old != new:
            return True
        # These files influence store metadata or build configuration, beyond
        # code copied by Docker. Asset additions/removals also invalidate caches.
        for filename in ("build.yaml", "build.yml", "build.json", "apparmor.txt"):
            a, b = directory / filename, staging / filename
            if (a.read_bytes() if a.is_file() else None) != (b.read_bytes() if b.is_file() else None):
                return True
        def translations(path):
            return {str(p.relative_to(path)): p.read_bytes() for p in (path / "translations").rglob("*") if p.is_file()}
        if translations(directory) != translations(staging):
            return True
        for filename in ("icon.png", "logo.png", "DOCS.md", "CHANGELOG.md", "README.md"):
            if (directory / filename).is_file() != (staging / filename).is_file():
                return True
        new["version"] = current["version"]
        cfg_path = new_configs[0]
        cfg_path.write_text(json.dumps(new, indent=2) if cfg_path.suffix == ".json" else yaml.safe_dump(new, sort_keys=False))
        candidate["version"] = current["version"]
        (staging / MARKER).write_text(json.dumps(candidate))
        return False

    def health(self, target, slug):
        timeout = self.options.get("health_timeout", 120)
        deadline = time.monotonic() + timeout
        stable_since = None
        opener = build_opener(ProxyHandler({}), NoRedirect())
        while time.monotonic() < deadline:
            info = self.supervisor.info(slug)
            healthy = info.get("state") == "started"
            if healthy and target["health_port"]:
                parsed_ip = ipaddress.ip_address(info.get("ip_address", ""))
                ip = f"[{parsed_ip}]" if parsed_ip.version == 6 else str(parsed_ip)
                address = f"http://{ip}:{target['health_port']}{target['health_path']}"
                try:
                    with opener.open(address, timeout=3) as response:
                        healthy = 200 <= response.status < 300
                except (URLError, OSError):
                    healthy = False
            if healthy:
                stable_since = time.monotonic() if stable_since is None else stable_since
                if time.monotonic() - stable_since >= 5:
                    return
            else:
                stable_since = None
            time.sleep(1)
        raise SupervisorError("Startup health check failed. Recovery is available; application data has not been rolled back.")

    def _finish(self, target, candidate, tx):
        if tx["watchdog"]:
            self.supervisor.post(f"/addons/{candidate['slug']}/options", {"watchdog": True})
        with self.lock:
            self.state["current"][target["id"]] = candidate
            self.state["transactions"].pop(target["id"], None)
            self.record(target, candidate, "success")

    def deploy_batch(self, targets, requested_sha=None):
        if self.state["transactions"]:
            raise ValueError("Recover interrupted deployments first")
        if not targets:
            self.event("No enabled repositories configured")
            return
        prepared = []
        with tempfile.TemporaryDirectory(dir=self.data_dir, prefix="stage-") as temporary:
            installed = self.supervisor.installed()
            used_slugs = set()
            # Complete every download and preflight before stopping any application.
            for target in targets:
                self.assert_owned(target, self.state["current"].get(target["id"]))
                self.event("Checking " + target["id"])
                sha = requested_sha or self.source.fetch(target)
                current = self.state["current"].get(target["id"])
                if current and current["slug"] in installed and installed[current["slug"]].get("version") == current["version"]:
                    if current["sha"] == sha:
                        self.event(target["id"] + " is already at " + sha[:12])
                        continue
                    if current.get("tree") == self.source.tree_hash(target, sha):
                        self.event(target["id"] + " source is unchanged at " + sha[:12])
                        continue
                staging = Path(temporary) / target["id"]
                candidate = self.source.stage(target, sha, staging)
                slug = candidate["slug"]
                if current and current["slug"] != slug:
                    raise ValueError("An add-on cannot change slug under a deployed ID")
                if slug in used_slugs or any(c["slug"] == slug and ident != target["id"]
                                            for ident, c in self.state["current"].items()):
                    raise ValueError("Two configured entries refer to the same local add-on slug")
                used_slugs.add(slug)
                if slug in installed and not current:
                    raise ValueError("A local add-on with this slug already exists and is not managed by this controller")
                # Existing repository versions can collide through hardware or ports.
                conflicts = [s for s in installed if s != slug and s.endswith("_" + slug.removeprefix("local_"))]
                if any(self.supervisor.info(s).get("state") != "stopped" for s in conflicts):
                    raise ValueError("Stop the existing repository-installed add-on before its first local deployment")
                needs_reload = self._prepare_definition(target, staging, candidate, current)
                # A missing installation must be rediscovered, even with an
                # otherwise unchanged manifest.
                needs_reload = (needs_reload or slug not in installed
                                or installed[slug].get("version") != candidate["version"])
                prepared.append((target, staging, candidate, needs_reload))
            if not prepared:
                self.event("No new commits to deploy")
                return
            active = []
            try:
                for target, staging, candidate, needs_reload in prepared:
                    tx = self._journal(target, candidate, candidate["slug"] in installed)
                    active.append((target, candidate, tx))
                    self._stop(target, tx)
                    self.phase(target, "publishing")
                    self._publish(target, staging)
                    self.phase(target, "published")
                if any(needs_reload for _, _, _, needs_reload in prepared):
                    self.event("Refreshing Supervisor definitions once for this batch")
                    self.supervisor.post("/store/reload", {})
                else:
                    self.event("Definitions unchanged. Skipping repository refresh.")
                for target, candidate, tx in active:
                    self.phase(target, "building")
                    self.event("Building " + target["id"] + " at " + candidate["sha"][:12])
                    self._build(candidate)
                    self.phase(target, "starting")
                    self.supervisor.post(f"/addons/{candidate['slug']}/start")
                    self.health(target, candidate["slug"])
                    self._finish(target, candidate, tx)
                    self.event("Deployed " + target["id"])
            except Exception as exc:
                # Durable journals deliberately survive. Blind rollback after a
                # timeout could race Supervisor, or reverse a database migration.
                for target, candidate, _tx in active:
                    if target["id"] in self.state["transactions"]:
                        self.record(target, candidate, "needs_recovery", str(exc))
                raise

    def recover(self, target):
        tx = self.state["transactions"].get(target["id"])
        if not tx:
            raise ValueError("No interrupted deployment to recover")
        target = tx["target"]
        slug = tx["candidate"]["slug"]
        self.supervisor.ensure_idle(slug)
        previous = tx["previous"]
        # First installation: retry the candidate. Existing deployment: restore
        # the exact prior commit. Neither action fetches GitHub.
        identity = previous or tx["candidate"]
        self.event("Recovering " + target["id"] + " to " + identity["sha"][:12])
        installed = slug in self.supervisor.installed()
        if installed:
            self.supervisor.post(f"/addons/{slug}/options", {"watchdog": False})
            self.supervisor.post(f"/addons/{slug}/stop")
            self.supervisor.wait_state(slug, "stopped", self.options.get("stop_timeout", 60))
        # Ownership is validated if any partially-published directory remains.
        self.assert_owned(target)
        with tempfile.TemporaryDirectory(dir=self.data_dir, prefix="recover-") as temporary:
            staging = Path(temporary) / "source"
            rebuilt_identity = self.source.stage(target, identity["sha"], staging)
            # A prior code-only deployment may deliberately share the version
            # of its last definition update. Restore that exact container version.
            rebuilt_identity["version"] = identity["version"]
            config_path = next(staging / n for n in CONFIG_NAMES if (staging / n).is_file())
            config = yaml.safe_load(config_path.read_text())
            config["version"] = identity["version"]
            config_path.write_text(json.dumps(config, indent=2) if config_path.suffix == ".json"
                                   else yaml.safe_dump(config, sort_keys=False))
            (staging / MARKER).write_text(json.dumps(rebuilt_identity))
            self._publish(target, staging)
            self.supervisor.post("/store/reload", {})
            self._build(rebuilt_identity)
            if tx["was_started"] or not previous:
                self.supervisor.post(f"/addons/{slug}/start")
                self.health(target, slug)
            self._finish(target, rebuilt_identity, tx)

    def rollback(self, target):
        current = self.state["current"].get(target["id"])
        if not current:
            raise ValueError("No version is currently deployed")
        previous = next((h for h in self.state["history"]
                         if h["id"] == target["id"] and h["status"] == "success" and h["sha"] != current["sha"]), None)
        if not previous:
            raise ValueError("No previous successful deployment is available")
        self.deploy_batch([target], previous["sha"])
