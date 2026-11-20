"""Real Git and model-Supervisor tests for no-rebuild frontend updates."""
import json
import subprocess
from pathlib import Path

import pytest
import yaml

from manager import live_static
from test_deployments import git


def new_commit(controller, target, upstream, message):
    git(upstream, "add", ".")
    git(upstream, "commit", "-m", message)
    sha = git(upstream, "rev-parse", "HEAD")
    subprocess.run(["git", "--git-dir", str(controller.source.root / (target["id"] + ".git")),
                    "-c", "protocol.file.allow=always", "fetch",
                    str(upstream), "main"], check=True, capture_output=True)
    return sha


def enable_live_assets(deployment):
    controller, sup, target, _first, second, upstream = deployment
    controller.deploy_batch([target], second)
    app = upstream / "app"
    config = yaml.safe_load((app / "config.yaml").read_text())
    config["map"] = [{"type": "local_apps", "read_only": True}]
    (app / "config.yaml").write_text(yaml.safe_dump(config))
    (app / "app").mkdir()\n    (app / "app" / "live_assets.py").write_text("# opt-in to serving live static assets\n")
    static = app / "app" / "static"
    static.mkdir()
    (static / "style.css").write_text("body{color:red}")
    base_sha = new_commit(controller, target, upstream, "Enable read-only live assets")
    controller.deploy_batch([target], base_sha)
    return controller, sup, target, upstream, base_sha


def test_static_css_deploys_live_without_stopping_or_rebuilding(deployment):
    c, sup, target, upstream, base = enable_live_assets(deployment)
    (upstream / "app/app/static/style.css").write_text("body{color:blue}")
    next_sha = new_commit(c, target, upstream, "Change CSS only")
    sup.calls.clear()
    c.deploy_batch([target], next_sha)
    calls = [p for p, _ in sup.calls]
    assert "/store/reload" not in calls
    assert "/addons/local_demo/stop" not in calls
    assert "/addons/local_demo/rebuild" not in calls
    assert "/store/addons/local_demo/update" not in calls
    assert sup.apps["local_demo"]["state"] == "started"
    assert c.state["current"]["demo"]["sha"] == next_sha
    assert c.state["current"]["demo"]["version_mode"] == "live-static"
    assert c.state["current"]["demo"]["container_sha"] == base
    folder = c.addons_dir / "devmgr_demo"
    assert (folder / "app/static/style.css").read_text() == "body{color:blue}"
    assert json.loads((folder / ".dev-manager.json").read_text())["sha"] == next_sha
    assert not (folder / "app/.devmgr-static-previous").exists()

    # Polling remains a no-op even when the installed Supervisor version
    # retains the original built commit suffix.
    sup.calls.clear()
    c.deploy_batch([target], next_sha)
    assert sup.calls == []

    # Selecting an older commit swaps assets back without a restart.
    c.rollback(target)
    assert c.state["current"]["demo"]["sha"] == base
    assert (folder / "app/static/style.css").read_text() == "body{color:red}"
    assert sup.apps["local_demo"]["state"] == "started"


@pytest.mark.parametrize("filename", ["run.sh", "app/static/assetlinks.json"])
def test_non_hot_change_requires_full_supervisor_build(deployment, filename):
    c, sup, target, upstream, _base = enable_live_assets(deployment)
    path = upstream / "app" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("new nonhot content")
    sha = new_commit(c, target, upstream, "Change non-hot source")
    sup.calls.clear()
    c.deploy_batch([target], sha)
    calls = [p for p, _ in sup.calls]
    assert "/addons/local_demo/rebuild" in calls
    assert "/addons/local_demo/stop" in calls


def test_hot_update_failure_restores_static_checkpoint(deployment, monkeypatch):
    c, sup, target, upstream, base = enable_live_assets(deployment)
    (upstream / "app/app/static/style.css").write_text("body{color:green}")
    sha = new_commit(c, target, upstream, "Update style")
    original_health = c.health

    def fail_health(*args):
        raise RuntimeError("injected health failure")

    monkeypatch.setattr(c, "health", fail_health)
    with pytest.raises(RuntimeError):
        c.deploy_batch([target], sha)
    folder = c.addons_dir / "devmgr_demo"
    assert c.state["transactions"]["demo"]["hot_static"] is True
    assert (folder / "app/.devmgr-static-previous").is_dir()
    monkeypatch.setattr(c, "health", original_health)
    # A fully published commit with a running container can be reconciled
    # without restoring old assets or rebuilding.
    c.recover(target)
    assert c.state["current"]["demo"]["sha"] == sha
    assert not c.state["transactions"]
    assert not (folder / "app/.devmgr-static-previous").exists()


def test_live_static_guard_rejects_untrusted_marker_or_rw_mount(deployment):
    c, sup, target, upstream, base = enable_live_assets(deployment)
    folder = c.addons_dir / "devmgr_demo"
    current = c.state["current"]["demo"]
    assert live_static.live_capable(folder, current)
    marker = folder / ".dev-manager.json"
    content = json.loads(marker.read_text())
    content["sha"] = "a" * 40
    marker.write_text(json.dumps(content))
    assert not live_static.live_capable(folder, current)
    marker.write_text(json.dumps(current))
    config = yaml.safe_load((folder / "config.yaml").read_text())
    config["map"][0]["read_only"] = False
    (folder / "config.yaml").write_text(yaml.safe_dump(config))
    assert not live_static.live_capable(folder, current)


def test_static_diff_rejects_backend_or_untrusted_app_link_changes(deployment):
    c, _sup, target, upstream, _base = enable_live_assets(deployment)
    (upstream / "app/app/static/style.css").write_text("body{color:purple}")
    next_sha = new_commit(c, target, upstream, "Change CSS")
    assert c.source.changed_files(target, _base, next_sha) == ["app/static/style.css"]
    assert live_static.allowed_paths(c.source.changed_files(target, _base, next_sha))
    assert not live_static.allowed_paths(["app/static/assetlinks.json"])
    assert not live_static.allowed_paths(["app/server.py"])
