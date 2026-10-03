"""Behavioural deployment tests against a separate Supervisor contract model."""

from copy import deepcopy
import json
from pathlib import Path
import subprocess

import pytest
import yaml

from manager.controller import Controller
from manager.source import MARKER, targets_from_options
from manager.supervisor import SupervisorError, UncertainMutation


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], stderr=subprocess.DEVNULL).decode().strip()


def row(ident="demo", path="app"):
    return dict(id=ident, repository="justcop/example", branch="main", path=path,
                enabled=True, update_on_start=True, health_port=0, health_path="/")


class ModelSupervisor:
    """Models source discovery, version rules, lifecycle and persisted options."""
    def __init__(self, addons):
        self.addons = addons
        self.apps, self.definitions, self.calls = {}, {}, []
        self.fail_path = None
        self.busy_slug = None

    def installed(self):
        return deepcopy(self.apps)

    def info(self, slug):
        return deepcopy(self.apps[slug])

    def ensure_idle(self, slug):
        if self.busy_slug == slug:
            raise SupervisorError("Supervisor is still working")

    def wait_state(self, slug, desired, timeout):
        if self.apps[slug]["state"] != desired:
            raise SupervisorError("Stop confirmation timed out")
        return self.info(slug)

    def get(self, path):
        if path == "/auth/list":
            return {"users": [{"username": "justin", "is_active": True, "group_ids": ["system-admin"]}]}
        raise AssertionError(path)

    def post(self, path, data=None):
        self.calls.append((path, deepcopy(data)))
        if path == self.fail_path:
            raise SupervisorError("Injected failure")
        if path == "/addons/self/options":
            return {}
        if path == "/store/reload":
            self.definitions = {}
            for config in self.addons.rglob("config.yaml"):
                cfg = yaml.safe_load(config.read_text())
                slug = "local_" + cfg["slug"]
                assert slug not in self.definitions, "duplicate add-on discovery"
                self.definitions[slug] = cfg
            return {}
        if path.endswith("/options/validate"):
            return {"valid": True}
        parts = path.strip("/").split("/")
        slug, action = parts[-2:]
        if action in ("install", "update", "rebuild"):
            definition = self.definitions[slug]
            previous = self.apps.get(slug)
            if action == "rebuild":
                assert previous["version"] == definition["version"], "Supervisor rejects changed-version rebuild"
            if action == "update":
                assert previous["version"] != definition["version"], "Supervisor rejects same-version update"
            options = deepcopy(definition.get("options", {}))
            options.update(previous.get("options", {}) if previous else {})
            self.apps[slug] = dict(slug=slug, name=definition["name"], version=definition["version"],
                                   options=options, state="stopped", watchdog=previous.get("watchdog", False) if previous else False)
        elif action == "options":
            self.apps[slug].update(data)
        elif action in ("start", "restart"):
            self.apps[slug]["state"] = "started"
        elif action == "stop":
            self.apps[slug]["state"] = "stopped"
        else:
            raise AssertionError(path)
        return {}


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git(upstream, "init", "-b", "main")
    git(upstream, "config", "user.name", "Test")
    git(upstream, "config", "user.email", "test@example.com")
    app = upstream / "app"
    app.mkdir()
    cfg = dict(name="Test add-on", version="1.0.0", slug="demo", description="Test", arch=["amd64"], options={"volume": 20}, schema={"volume": "int"}, image="ghcr.io/example/demo")
    (app / "config.yaml").write_text(yaml.safe_dump(cfg))
    (app / "Dockerfile").write_text("FROM alpine:3.22\nCOPY run.sh /run.sh\nCMD [\"/run.sh\"]\n")
    (app / "run.sh").write_text("#!/bin/sh\necho first\n")
    (app / "run.sh").chmod(0o755)
    git(upstream, "add", ".")
    git(upstream, "commit", "-m", "First version")
    first = git(upstream, "rev-parse", "HEAD")
    cfg["options"]["new_setting"] = True
    cfg["schema"]["new_setting"] = "bool"
    (app / "config.yaml").write_text(yaml.safe_dump(cfg))
    (app / "run.sh").write_text("#!/bin/sh\necho second\n")
    git(upstream, "add", ".")
    git(upstream, "commit", "-m", "Second code version without version bump")
    second = git(upstream, "rev-parse", "HEAD")
    data, addons = tmp_path / "data", tmp_path / "addons"
    addons.mkdir()
    options = dict(repositories=[row()], github_token="private-secret", update_on_start=True)
    sup = ModelSupervisor(addons)
    controller = Controller(data, addons, options, sup)
    target = controller.target("demo")
    mirror = controller.source.root / "demo.git"
    subprocess.run(["git", "clone", "--bare", str(upstream), str(mirror)], check=True, capture_output=True)
    controller.source.run(target, "remote", "set-url", "origin", target["repository"])
    monkeypatch.setattr(controller.source, "fetch", lambda t: second)
    monkeypatch.setattr(controller, "health", lambda t, slug: sup.wait_state(slug, "started", 1))
    return controller, sup, target, first, second, upstream


def test_install_builds_local_source_and_does_not_pull_image(deployment):
    c, sup, t, first, second, _ = deployment
    c.deploy_batch([t], first)
    current = c.state["current"]["demo"]
    assert current["sha"] == first
    assert current["version"] == "1.0.0-dev." + first[:12]
    assert "image" not in sup.definitions["local_demo"]
    assert sup.apps["local_demo"]["state"] == "started"
    assert not c.state["transactions"]
    assert (c.addons_dir / "devmgr_demo/run.sh").stat().st_mode & 0o111
    assert c.state["history"][0]["number"] == 1


def test_update_same_upstream_version_preserves_user_options_and_refreshes_schema(deployment):
    c, sup, t, first, second, _ = deployment
    c.deploy_batch([t], first)
    sup.apps["local_demo"].update(options={"volume": 37}, watchdog=True)
    sup.calls.clear()
    c.deploy_batch([t], second)
    assert sup.apps["local_demo"]["options"] == {"volume": 37, "new_setting": True}
    assert sup.apps["local_demo"]["watchdog"] is True
    assert "new_setting" in sup.definitions["local_demo"]["schema"]
    calls = [path for path, _ in sup.calls]
    assert calls.index("/addons/local_demo/stop") < calls.index("/store/reload") < calls.index("/store/addons/local_demo/update")
    assert "/addons/local_demo/rebuild" not in calls
    assert json.loads((c.addons_dir / "devmgr_demo" / MARKER).read_text())["sha"] == second


def test_unchanged_commits_do_not_stop_refresh_or_rebuild(deployment):
    c, sup, t, first, _, _ = deployment
    c.deploy_batch([t], first)
    sup.calls.clear()
    c.deploy_batch([t], first)
    assert sup.calls == []


def test_all_downloads_and_manifests_are_validated_before_any_stop(deployment):
    c, sup, t, first, _, _ = deployment
    c.deploy_batch([t], first)
    invalid = dict(t, id="other", path="missing")
    # Use the same object database for a second target, but retain separate ownership.
    import shutil
    shutil.copytree(c.source.root / "demo.git", c.source.root / "other.git")
    sup.calls.clear()
    with pytest.raises(RuntimeError):
        c.deploy_batch([t, invalid])
    assert sup.calls == []
    assert not c.state["transactions"]


def test_build_failure_journal_survives_restart_and_recovers_previous_commit(deployment, monkeypatch):
    c, sup, t, first, second, _ = deployment
    c.deploy_batch([t], first)
    sup.fail_path = "/store/addons/local_demo/update"
    with pytest.raises(SupervisorError):
        c.deploy_batch([t], second)
    assert c.state["transactions"]["demo"]["phase"] == "building"
    assert sup.apps["local_demo"]["state"] == "stopped"
    restored = Controller(c.data_dir, c.addons_dir, c.options, sup)
    monkeypatch.setattr(restored, "health", lambda t, slug: None)
    monkeypatch.setattr(restored.source, "fetch", lambda _: pytest.fail("Recovery must work offline"))
    sup.fail_path = None
    restored.recover(t)
    assert restored.state["current"]["demo"]["sha"] == first
    assert not restored.state["transactions"]
    assert "first" in (c.addons_dir / "devmgr_demo/run.sh").read_text()
    assert sup.apps["local_demo"]["state"] == "started"


def test_uncertain_update_does_not_race_supervisor_with_automatic_rollback(deployment, monkeypatch):
    c, sup, t, first, second, _ = deployment
    c.deploy_batch([t], first)
    original = sup.post
    def post(path, data=None):
        if path.endswith("/update"):
            sup.busy_slug = "local_demo"
            raise UncertainMutation("Connection lost")
        return original(path, data)
    monkeypatch.setattr(sup, "post", post)
    with pytest.raises(UncertainMutation):
        c.deploy_batch([t], second)
    assert c.state["transactions"]["demo"]["candidate"]["sha"] == second
    with pytest.raises(SupervisorError, match="still working"):
        c.recover(t)


def test_stop_confirmation_failure_never_replaces_files(deployment, monkeypatch):
    c, sup, t, first, second, _ = deployment
    c.deploy_batch([t], first)
    monkeypatch.setattr(sup, "wait_state", lambda *a: (_ for _ in ()).throw(SupervisorError("Stop confirmation timed out")))
    with pytest.raises(SupervisorError):
        c.deploy_batch([t], second)
    assert "first" in (c.addons_dir / "devmgr_demo/run.sh").read_text()
    assert c.state["transactions"]["demo"]["phase"] == "prepared"


def test_failed_health_does_not_blindly_reverse_possible_data_migrations(deployment, monkeypatch):
    c, sup, t, first, second, _ = deployment
    c.deploy_batch([t], first)
    monkeypatch.setattr(c, "health", lambda *a: (_ for _ in ()).throw(SupervisorError("Health failed")))
    with pytest.raises(SupervisorError):
        c.deploy_batch([t], second)
    assert c.state["transactions"]["demo"]["phase"] == "starting"
    assert sup.apps["local_demo"]["version"].endswith(second[:12])
    assert c.state["history"][0]["status"] == "needs_recovery"


def test_rollback_uses_previous_successful_commit(deployment):
    c, sup, t, first, second, _ = deployment
    c.deploy_batch([t], first)
    c.deploy_batch([t], second)
    c.rollback(t)
    assert c.state["current"]["demo"]["sha"] == first
    assert sup.apps["local_demo"]["version"].endswith(first[:12])


def test_first_install_failure_can_retry_candidate_without_network(deployment):
    c, sup, t, first, _, _ = deployment
    sup.fail_path = "/store/addons/local_demo/install"
    with pytest.raises(SupervisorError):
        c.deploy_batch([t], first)
    sup.fail_path = None
    c.recover(t)
    assert c.state["current"]["demo"]["sha"] == first


def test_existing_unmanaged_local_addon_is_not_taken_over(deployment):
    c, sup, t, first, _, _ = deployment
    sup.apps["local_demo"] = dict(version="0.9", state="started")
    with pytest.raises(ValueError, match="not managed"):
        c.deploy_batch([t], first)
    assert not sup.calls


def test_running_repository_installation_must_be_stopped_first(deployment):
    c, sup, t, first, _, _ = deployment
    sup.apps["abcd_demo"] = dict(version="0.9", state="started")
    with pytest.raises(ValueError, match="Stop the existing"):
        c.deploy_batch([t], first)
    assert not sup.calls


def test_owned_directory_cannot_be_repointed_or_follow_symlink(deployment, tmp_path):
    c, _, t, first, _, _ = deployment
    outside = tmp_path / "precious"
    outside.mkdir()
    (outside / "keep").write_text("safe")
    (c.addons_dir / "devmgr_demo").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        c.deploy_batch([t], first)
    assert (outside / "keep").read_text() == "safe"


def test_configuration_persists_through_supervisor_and_never_returns_token(deployment):
    c, sup, _, _, _, _ = deployment
    c.configure([dict(row(), branch="feature")], False)
    assert sup.calls[-1][0] == "/addons/self/options"
    assert sup.calls[-1][1]["options"]["github_token"] == "private-secret"
    assert "private-secret" not in json.dumps(c.snapshot())
    assert c.targets[0]["branch"] == "feature"


def test_deployed_id_cannot_change_repository_or_path(deployment):
    c, _, t, first, _, _ = deployment
    c.deploy_batch([t], first)
    with pytest.raises(ValueError, match="cannot change"):
        c.configure([dict(row(), repository="other/repo")], True)


def test_busy_controller_and_pending_transactions_block_changes(deployment):
    c, _, _, _, _, _ = deployment
    c.busy = True
    with pytest.raises(ValueError, match="already running"):
        c.submit("deploy_all")
    with pytest.raises(ValueError, match="Finish or recover"):
        c.configure([], True)


def test_startup_filters_entries_but_manual_update_all_does_not(deployment, monkeypatch):
    c, _, _, _, _, _ = deployment
    c.targets = [dict(c.targets[0], update_on_start=False), dict(c.targets[0], id="off", enabled=False)]
    deployed = []
    monkeypatch.setattr(c, "deploy_batch", lambda targets: deployed.extend(t["id"] for t in targets))
    c._worker("startup", None, None)
    assert deployed == []
    c._worker("deploy_all", None, None)
    assert deployed == ["demo"]


def test_batch_reloads_definitions_once_for_multiple_addons(deployment):
    c, sup, t, _, _, upstream = deployment
    import shutil
    second_app = upstream / "other"
    shutil.copytree(upstream / "app", second_app)
    cfg = yaml.safe_load((second_app / "config.yaml").read_text())
    cfg["slug"] = "other"
    (second_app / "config.yaml").write_text(yaml.safe_dump(cfg))
    git(upstream, "add", ".")
    git(upstream, "commit", "-m", "Second add-on")
    sha = git(upstream, "rev-parse", "HEAD")
    subprocess.run(["git", "--git-dir", str(c.source.root / "demo.git"), "-c", "protocol.file.allow=always", "fetch", str(upstream), "main"], check=True, capture_output=True)
    shutil.copytree(c.source.root / "demo.git", c.source.root / "other.git")
    other = dict(t, id="other", path="other")
    c.deploy_batch([t, other], sha)
    assert [p for p, _ in sup.calls].count("/store/reload") == 1
    assert set(sup.apps) == {"local_demo", "local_other"}


def test_duplicate_slug_in_batch_fails_before_mutating_any_addon(deployment):
    c, sup, t, first, _, _ = deployment
    import shutil
    shutil.copytree(c.source.root / "demo.git", c.source.root / "other.git")
    with pytest.raises(ValueError, match="same local"):
        c.deploy_batch([t, dict(t, id="other")], first)
    assert not sup.calls


def code_commit(c, upstream, source_version="1.0.0"):
    (upstream / "app/run.sh").write_text("#!/bin/sh\necho third\n")
    cfg = yaml.safe_load((upstream / "app/config.yaml").read_text())
    cfg["version"] = source_version
    (upstream / "app/config.yaml").write_text(yaml.safe_dump(cfg))
    git(upstream, "add", ".")
    git(upstream, "commit", "-m", "Code only change")
    sha = git(upstream, "rev-parse", "HEAD")
    subprocess.run(["git", "--git-dir", str(c.source.root / "demo.git"), "-c", "protocol.file.allow=always", "fetch", str(upstream), "main"], check=True, capture_output=True)
    return sha


@pytest.mark.parametrize("source_version", ["1.0.0", "1.0.1"])
def test_code_only_update_skips_all_repository_refreshes(deployment, source_version):
    c, sup, t, _, second, upstream = deployment
    c.deploy_batch([t], second)
    original_container_version = sup.apps["local_demo"]["version"]
    third = code_commit(c, upstream, source_version)
    sup.calls.clear()
    c.deploy_batch([t], third)
    paths = [p for p, _ in sup.calls]
    assert "/store/reload" not in paths
    assert "/addons/local_demo/rebuild" in paths
    assert sup.apps["local_demo"]["version"] == original_container_version
    assert c.state["current"]["demo"]["source_version"] == source_version
    assert c.state["current"]["demo"]["sha"] == third
    assert "third" in (c.addons_dir / "devmgr_demo/run.sh").read_text()


def test_code_only_failure_recovers_exact_prior_definition_version(deployment):
    c, sup, t, _, second, upstream = deployment
    c.deploy_batch([t], second)
    third = code_commit(c, upstream)
    c.deploy_batch([t], third)
    cfg = yaml.safe_load((upstream / "app/config.yaml").read_text())
    cfg["ports"] = {"8099/tcp": 8099}
    (upstream / "app/config.yaml").write_text(yaml.safe_dump(cfg))
    git(upstream, "add", ".")
    git(upstream, "commit", "-m", "Container definition change")
    fourth = git(upstream, "rev-parse", "HEAD")
    subprocess.run(["git", "--git-dir", str(c.source.root / "demo.git"), "-c", "protocol.file.allow=always", "fetch", str(upstream), "main"], check=True, capture_output=True)
    old_version = c.state["current"]["demo"]["version"]
    sup.fail_path = "/store/addons/local_demo/update"
    with pytest.raises(SupervisorError):
        c.deploy_batch([t], fourth)
    sup.fail_path = None
    c.recover(t)
    assert c.state["current"]["demo"]["sha"] == third
    assert c.state["current"]["demo"]["version"] == old_version
    assert sup.apps["local_demo"]["version"] == old_version


def test_translations_or_build_definition_changes_require_metadata_refresh(deployment):
    c, sup, t, _, second, upstream = deployment
    c.deploy_batch([t], second)
    translations = upstream / "app/translations"
    translations.mkdir()
    (translations / "en.yaml").write_text("configuration:\n  volume:\n    name: Volume\n")
    git(upstream, "add", ".")
    git(upstream, "commit", "-m", "New configuration labels")
    sha = git(upstream, "rev-parse", "HEAD")
    subprocess.run(["git", "--git-dir", str(c.source.root / "demo.git"), "-c", "protocol.file.allow=always", "fetch", str(upstream), "main"], check=True, capture_output=True)
    sup.calls.clear()
    c.deploy_batch([t], sha)
    assert "/store/reload" in [p for p, _ in sup.calls]


def test_recovery_of_interrupted_empty_directory_publication(deployment):
    c, sup, t, first, second, _ = deployment
    c.deploy_batch([t], first)
    previous = c.state["current"]["demo"]
    candidate = dict(previous, sha=second)
    c._journal(t, candidate, True)
    c.phase(t, "publishing")
    import shutil
    shutil.rmtree(c.addons_dir / "devmgr_demo")
    (c.addons_dir / "devmgr_demo").mkdir()
    c.recover(t)
    assert c.state["current"]["demo"]["sha"] == first
    assert "first" in (c.addons_dir / "devmgr_demo/run.sh").read_text()


def test_incompatible_persisted_options_are_detected_before_start(deployment, monkeypatch):
    c, sup, t, first, _, _ = deployment
    original = sup.post
    def post(path, data=None):
        if path.endswith("/options/validate"):
            return {"valid": False, "message": "secret-may-be-present"}
        return original(path, data)
    monkeypatch.setattr(sup, "post", post)
    with pytest.raises(SupervisorError, match="incompatible") as exc:
        c.deploy_batch([t], first)
    assert "secret-may-be-present" not in str(exc.value)
    assert sup.apps["local_demo"]["state"] == "stopped"
    assert c.state["transactions"]


def test_changes_to_other_addons_in_shared_repository_skip_rebuild(deployment):
    c, sup, t, _, second, upstream = deployment
    c.deploy_batch([t], second)
    (upstream / "unrelated.md").write_text("A different project changed")
    git(upstream, "add", ".")
    git(upstream, "commit", "-m", "Change outside this add-on")
    sha = git(upstream, "rev-parse", "HEAD")
    subprocess.run(["git", "--git-dir", str(c.source.root / "demo.git"), "-c", "protocol.file.allow=always", "fetch", str(upstream), "main"], check=True, capture_output=True)
    sup.calls.clear()
    c.deploy_batch([t], sha)
    assert sup.calls == []
    assert c.state["current"]["demo"]["sha"] == second
