import io
import json
import re
import tarfile
from unittest.mock import Mock

import pytest

from manager.source import GitSource, repository_url, source_path, targets_from_options
from manager.supervisor import Supervisor, SupervisorError, UncertainMutation
from manager.web import create_app
from test_deployments import deployment, row


@pytest.mark.parametrize("value", ["https://evil.example/a/b", "http://github.com/a/b", "https://user:secret@github.com/a/b", "https://github.com/a/b#main", "https://github.com/a/b?token=secret", "file:///tmp/repo", "a/..", "a/b/c"])
def test_repository_boundary(value):
    with pytest.raises(ValueError):
        repository_url(value)


@pytest.mark.parametrize("value", ["../outside", "/etc", "a/../../b", "a\\b", "main:app"])
def test_source_path_boundary(value):
    with pytest.raises(ValueError):
        source_path(value)


def test_invalid_and_duplicate_target_ids():
    with pytest.raises(ValueError):
        targets_from_options({"repositories": [dict(row(), id="../outside")]})
    with pytest.raises(ValueError, match="Duplicate"):
        targets_from_options({"repositories": [row(), row()]})


@pytest.mark.parametrize("kind,name", [("symlink", "link"), ("file", "../escape"), ("file", "nested/config.yaml")])
def test_archive_rejects_paths_links_and_nested_addon_configs(tmp_path, monkeypatch, kind, name):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as tar:
        member = tarfile.TarInfo(name)
        if kind == "symlink":
            member.type = tarfile.SYMTYPE
            member.linkname = "/etc/passwd"
            tar.addfile(member)
        else:
            payload = b"unsafe"
            member.size = len(payload)
            tar.addfile(member, io.BytesIO(payload))
    src = GitSource(tmp_path / "git")
    monkeypatch.setattr(src, "run", lambda *args, **kwargs: stream.getvalue() if kwargs.get("binary") else "")
    target = targets_from_options({"repositories": [row()]})[0]
    with pytest.raises(ValueError):
        src.stage(target, "a" * 40, tmp_path / "stage")
    assert not (tmp_path / "escape").exists()


def test_git_credentials_are_not_in_arguments_or_error_messages(tmp_path, monkeypatch):
    import manager.source as module
    observed = []
    def run(args, **kwargs):
        observed.append((args, kwargs))
        return Mock(returncode=1, stderr=b"private-secret", stdout=b"")
    monkeypatch.setattr(module.subprocess, "run", run)
    src = GitSource(tmp_path / "git", "private-secret")
    with pytest.raises(RuntimeError) as exc:
        src.run(row(), "fetch", "origin")
    assert "private-secret" not in str(exc.value)
    assert "private-secret" not in " ".join(observed[0][0])
    assert observed[0][1]["env"]["GIT_TERMINAL_PROMPT"] == "0"


def test_supervisor_transport_loss_is_uncertain_for_mutations(monkeypatch):
    import manager.supervisor as module
    monkeypatch.setattr(module, "urlopen", lambda *a, **kw: (_ for _ in ()).throw(TimeoutError("secret")))
    sup = Supervisor("test-token")
    with pytest.raises(UncertainMutation):
        sup.post("/addons/local_demo/rebuild")
    with pytest.raises(SupervisorError) as exc:
        sup.get("/addons")
    assert not isinstance(exc.value, UncertainMutation)
    assert "secret" not in str(exc.value)


def test_supervisor_recovery_refuses_active_child_job(monkeypatch):
    sup = Supervisor("test-token")
    monkeypatch.setattr(sup, "get", lambda path: {"jobs": [{"reference": None, "done": False, "child_jobs": [{"reference": "local_demo", "done": False}]}]})
    with pytest.raises(SupervisorError, match="still working"):
        sup.ensure_idle("local_demo")


def test_supervisor_recovery_refuses_global_repository_reload_race(monkeypatch):
    sup = Supervisor("test-token")
    monkeypatch.setattr(sup, "get", lambda path: {"jobs": [{"name": "store_manager_reload", "reference": None, "done": False}]})
    with pytest.raises(SupervisorError, match="still working"):
        sup.ensure_idle("local_demo")


@pytest.fixture
def web(deployment):
    c = deployment[0]
    client = create_app(c, testing=True).test_client()
    response = client.get("/", environ_base={"REMOTE_ADDR": "172.30.32.2"})
    token = re.search(r'name="csrf-token" content="([^"]+)"', response.text)[1]
    return client, c, token


def test_ingress_rejects_direct_requests_even_with_spoofed_forwarded_header(web):
    client, _, _ = web
    assert client.get("/", headers={"X-Forwarded-For": "172.30.32.2"}).status_code == 403


def test_csrf_and_json_are_required_before_mutations(web):
    client, c, token = web
    c.submit = Mock()
    env = {"REMOTE_ADDR": "172.30.32.2"}
    assert client.post("/api/actions", json={"action": "deploy_all"}, environ_base=env).status_code == 403
    assert client.post("/api/actions", data="action=deploy_all", headers={"X-CSRF-Token": token}, environ_base=env).status_code == 415
    assert not c.submit.called
    assert client.post("/api/actions", json={"action": "deploy_all"}, headers={"X-CSRF-Token": token}, environ_base=env).status_code == 202
    c.submit.assert_called_once_with("deploy_all", None, None)


def test_code_recovery_requires_data_acknowledgement(web):
    client, c, token = web
    c.submit = Mock()
    response = client.post("/api/actions", json={"action": "recover", "id": "demo"}, headers={"X-CSRF-Token": token}, environ_base={"REMOTE_ADDR": "172.30.32.2"})
    assert response.status_code == 400
    assert not c.submit.called


def test_secrets_never_returned_in_status_or_runtime(web):
    client, c, _ = web
    c.supervisor.apps["local_demo"] = dict(state="started", version="1.0", options={"spotify_secret": "spotify-secret"})
    for path in ("/api/status", "/api/runtime"):
        response = client.get(path, environ_base={"REMOTE_ADDR": "172.30.32.2"})
        assert response.status_code == 200
        assert "private-secret" not in response.text and "spotify-secret" not in response.text


def test_ingress_paths_work_without_absolute_static_urls(web):
    client, _, _ = web
    response = client.get("/", headers={"X-Ingress-Path": "/api/hassio_ingress/example"}, environ_base={"REMOTE_ADDR": "172.30.32.2"})
    assert 'src="/api/hassio_ingress/example/static/app.js?v=' in response.text
    assert 'href="/api/hassio_ingress/example/static/style.css?v=' in response.text


def test_actual_home_assistant_admin_check_is_enforced(deployment):
    client = create_app(deployment[0]).test_client()
    env = {"REMOTE_ADDR": "172.30.32.2"}
    assert client.get("/", environ_base=env).status_code == 403
    assert client.get("/", headers={"X-Remote-User-Name": "ordinary-user"}, environ_base=env).status_code == 403
    assert client.get("/", headers={"X-Remote-User-Name": "justin"}, environ_base=env).status_code == 200


def test_unknown_target_and_invalid_commit_do_not_schedule_actions(web):
    client, c, token = web
    response = client.post("/api/actions", json={"action": "deploy", "id": "demo", "sha": "main; rm -rf /"}, headers={"X-CSRF-Token": token}, environ_base={"REMOTE_ADDR": "172.30.32.2"})
    assert response.status_code == 400
    assert not c.busy


def test_discovery_endpoint_is_read_only_and_uses_configured_token(web, monkeypatch):
    client, c, _ = web
    observed = {}
    def fake(repository, branch, token):
        observed.update(repository=repository, branch=branch, token=token)
        return {"repository": repository, "branch": branch, "addons": [{"id": "demo", "name": "Demo"}]}
    monkeypatch.setattr("manager.web.discover_addons", fake)
    response = client.get(
        "/api/discover?repository=justcop%2Fhome-assistant-addons&branch=main",
        environ_base={"REMOTE_ADDR": "172.30.32.2"},
    )
    assert response.status_code == 200
    assert response.get_json()["addons"][0]["name"] == "Demo"
    assert observed == {
        "repository": "justcop/home-assistant-addons",
        "branch": "main",
        "token": c.source.token,
    }


def test_frontend_assets_are_cache_busted_and_not_cacheable(web):
    client, _, _ = web
    response = client.get("/", environ_base={"REMOTE_ADDR": "172.30.32.2"})
    assert "static/app.js?v=" in response.text
    assert "static/style.css?v=" in response.text
    assert "no-cache" in response.headers["Cache-Control"]


def test_native_add_discovered_persists_without_javascript(web, monkeypatch):
    client, c, token = web
    c.configure = Mock()
    monkeypatch.setattr(
        "manager.web.discover_addons",
        lambda repository, branch, github_token: {
            "repository": "justcop/home-assistant-addons",
            "branch": "main",
            "addons": [{
                "id": "money_locations",
                "name": "Money Locations",
                "path": "money_locations",
                "health_port": 0,
            }],
        },
    )
    response = client.post(
        "/add-discovered",
        data={
            "_csrf": token,
            "discovery_repository": "justcop/home-assistant-addons",
            "discovery_branch": "main",
            "discovered_path": "money_locations",
            "discovered_health_port": "8123",
        },
        environ_base={"REMOTE_ADDR": "172.30.32.2"},
    )
    assert response.status_code == 302
    assert "added=Money%20Locations" in response.headers["Location"]
    rows = c.configure.call_args.args[0]
    assert rows[-1] == {
        "id": "money_locations",
        "repository": "justcop/home-assistant-addons",
        "branch": "main",
        "path": "money_locations",
        "enabled": True,
        "update_on_start": True,
        "health_port": 8123,
        "health_path": "/",
    }


def test_native_add_discovered_rejects_stale_selection(web, monkeypatch):
    client, c, token = web
    c.configure = Mock()
    monkeypatch.setattr(
        "manager.web.discover_addons",
        lambda *args: {"repository": "justcop/home-assistant-addons", "branch": "main", "addons": []},
    )
    response = client.post(
        "/add-discovered",
        data={
            "_csrf": token,
            "discovery_repository": "justcop/home-assistant-addons",
            "discovery_branch": "main",
            "discovered_path": "missing",
            "discovered_health_port": "0",
        },
        environ_base={"REMOTE_ADDR": "172.30.32.2"},
    )
    assert response.status_code == 302
    assert "add_error=" in response.headers["Location"]
    assert not c.configure.called


def test_quick_add_is_a_separate_form_from_settings(web):
    client, _, _ = web
    response = client.get("/", environ_base={"REMOTE_ADDR": "172.30.32.2"})
    html = response.text
    assert 'id="quick-add-form"' in html
    assert 'action="/add-discovered"' in html
    assert 'id="settings-form"' in html
    quick_start = html.index('id="quick-add-form"')
    quick_end = html.index("</form>", quick_start)
    settings_start = html.index('id="settings-form"')
    assert quick_end < settings_start
    assert 'id="add-discovered"' in html[quick_start:quick_end]
    assert 'id="add-discovered"' not in html[settings_start:html.index("</form>", settings_start)]


def test_git_discovery_finds_addons_without_github_api(monkeypatch):
    from manager.discovery import discover_addons

    monkeypatch.setattr(
        "manager.discovery.subprocess.run",
        lambda *args, **kwargs: Mock(returncode=0, stdout=b"", stderr=b""),
    )

    calls = []
    def fake_git(git_dir, token, *args, binary=False):
        calls.append(args)
        if args[:3] == ("ls-tree", "-r", "--name-only"):
            return "money_locations/config.yaml\nmoney_locations/Dockerfile\nREADME.md"
        if args[0] == "show":
            return (
                b"name: Money Locations\n"
                b"slug: money_locations\n"
                b"description: Test add-on\n"
                b"version: 1.0.0\n"
                b"arch:\n  - amd64\n"
                b"ingress_port: 8099\n"
            )
        return b"" if binary else ""

    monkeypatch.setattr("manager.discovery._run_git", fake_git)
    result = discover_addons("justcop/home-assistant-addons", "main", "private-secret")

    assert result["repository"] == "justcop/home-assistant-addons"
    assert result["branch"] == "main"
    assert result["addons"] == [{
        "id": "money_locations",
        "name": "Money Locations",
        "slug": "money_locations",
        "description": "Test add-on",
        "version": "1.0.0",
        "arch": ["amd64"],
        "repository": "justcop/home-assistant-addons",
        "branch": "main",
        "path": "money_locations",
        "health_port": 0,
        "health_path": "/",
    }]
    assert any(call[0] == "fetch" for call in calls)


def test_health_accepts_ingress_only_http_403(deployment, monkeypatch):
    import manager.controller as module

    c = deployment[0]
    target = dict(row(), health_port=8099, health_path="/health")
    c.targets = [target]
    c.options["health_timeout"] = 1
    c.supervisor.info = Mock(return_value={"state": "started", "ip_address": "172.30.33.5"})

    class FakeOpener:
        def open(self, *args, **kwargs):
            from urllib.error import HTTPError
            raise HTTPError(args[0], 403, "Forbidden", {}, None)

    monkeypatch.setattr(module, "build_opener", lambda *args: FakeOpener())
    ticks = iter([0, 0, 6])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(module.time, "sleep", lambda *_: None)

    c.health(target, "local_demo")


def test_health_port_zero_uses_supervisor_state_only(deployment, monkeypatch):
    import manager.controller as module

    c = deployment[0]
    target = dict(row(), health_port=0, health_path="/")
    c.options["health_timeout"] = 10
    c.supervisor.info = Mock(return_value={"state": "started", "ip_address": "172.30.33.5"})

    class FakeOpener:
        def open(self, *args, **kwargs):
            pytest.fail("Supervisor-only health must not perform an HTTP request")

    monkeypatch.setattr(module, "build_opener", lambda *args: FakeOpener())
    ticks = iter([0, 0, 6])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(module.time, "sleep", lambda *_: None)

    c.health(target, "local_demo")
