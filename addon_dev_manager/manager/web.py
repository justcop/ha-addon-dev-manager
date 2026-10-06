"""Ingress-only administrative interface and background job endpoints."""

import fcntl
import json
import logging
import os
from pathlib import Path
import secrets
import time

from flask import Flask, abort, jsonify, render_template, request

from .controller import Controller
from .discovery import discover_addons
from .source import SHA
from .supervisor import Supervisor


def create_app(controller, testing=False):
    app = Flask(__name__)
    app.config.update(MAX_CONTENT_LENGTH=128 * 1024, TESTING=testing)
    csrf = secrets.token_urlsafe(32)
    admin_cache = {"until": 0, "names": set()}

    @app.before_request
    def authorise():
        # Do not trust X-Forwarded-For: check the actual TCP peer.
        if request.remote_addr != "172.30.32.2":
            abort(403, "Use Open Web UI in Home Assistant.")
        if not testing:
            username = request.headers.get("X-Remote-User-Name", "")
            if not username:
                abort(403, "An identified Home Assistant administrator is required. Reopen the ingress session.")
            if time.monotonic() >= admin_cache["until"]:
                try:
                    users = controller.supervisor.get("/auth/list").get("users", [])
                except Exception:
                    abort(503, "Unable to verify administrator access through Supervisor.")
                admin_cache["names"] = {u["username"] for u in users if u.get("is_active")
                                         and (u.get("is_owner") or "system-admin" in u.get("group_ids", []))}
                admin_cache["until"] = time.monotonic() + 30
            if username not in admin_cache["names"]:
                abort(403, "Only Home Assistant administrators can manage development add-ons.")
        if request.method != "GET":
            supplied = request.headers.get("X-CSRF-Token", "")
            if not secrets.compare_digest(csrf, supplied):
                abort(403, "Missing or expired request token. Reload the page.")
            if not request.is_json:
                abort(415, "Send application/json")

    @app.after_request
    def headers(response):
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                 "Referrer-Policy": "same-origin",
                                 "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; "
                                 "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'"})
        return response

    @app.errorhandler(ValueError)
    def invalid(exc):
        return jsonify(error=str(exc)), 400

    @app.errorhandler(Exception)
    def failure(exc):
        from werkzeug.exceptions import HTTPException
        if isinstance(exc, HTTPException):
            return jsonify(error=exc.description), exc.code
        logging.exception("Web request failed")
        return jsonify(error="Operation failed. See the add-on log for details."), 500

    @app.get("/")
    def index():
        return render_template("index.html", csrf=csrf,
                               base=request.headers.get("X-Ingress-Path", "").rstrip("/"))

    @app.get("/api/status")
    def status():
        return jsonify(controller.snapshot())

    @app.get("/api/runtime")
    def runtime():
        # Options may contain Spotify or other credentials. Return only lifecycle fields.
        return jsonify({slug: {key: info.get(key) for key in ("state", "version", "name")}
                        for slug, info in controller.supervisor.installed().items()})

    @app.get("/api/targets/<ident>/versions")
    def versions(ident):
        target = controller.target(ident)
        if not (controller.source.root / (ident + ".git")).exists():
            return jsonify([])
        return jsonify(controller.source.versions(target))

    @app.get("/api/discover")
    def discover():
        # Discovery is read-only, so use GET. This also avoids ingress/proxy
        # environments that are unnecessarily fussy about POST headers.
        repository = request.args.get("repository", "")
        branch = request.args.get("branch", "")
        if not isinstance(repository, str) or not isinstance(branch, str):
            raise ValueError("Repository and branch must be text")
        return jsonify(discover_addons(repository, branch, controller.source.token))

    @app.post("/api/settings")
    def settings():
        body = request.get_json()
        if not isinstance(body, dict) or type(body.get("update_on_start")) is not bool:
            raise ValueError("Settings need repositories and an update_on_start boolean")
        controller.configure(body.get("repositories"), body["update_on_start"],
                             body.get("automatic_updates"), body.get("check_interval"))
        return jsonify(ok=True)

    @app.post("/api/actions")
    def actions():
        body = request.get_json()
        if not isinstance(body, dict):
            raise ValueError("Expected an action object")
        action, ident, sha = body.get("action"), body.get("id"), body.get("sha")
        if action in ("startup", "automatic"):
            raise ValueError("Startup is an internal action")
        if sha is not None and (not isinstance(sha, str) or not SHA.fullmatch(sha)):
            raise ValueError("Select a commit from version history")
        if action in ("rollback", "recover") and body.get("acknowledge_data") is not True:
            raise ValueError("Confirm that code recovery does not reverse application data changes")
        if action == "deploy" and sha and body.get("acknowledge_data") is not True:
            raise ValueError("Confirm that choosing another version does not reverse application data changes")
        controller.submit(action, ident, sha)
        return jsonify(accepted=True), 202

    return app


def main():
    from waitress import serve
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    data_dir = Path(os.environ.get("DEV_MANAGER_DATA", "/data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    # One service process across restarts, not just a Python thread lock.
    lock = (data_dir / "controller.lock").open("w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    options = json.loads((data_dir / "options.json").read_text())
    controller = Controller(data_dir, os.environ.get("DEV_MANAGER_ADDONS", "/addons"), options, Supervisor())
    app = create_app(controller)
    if controller.state["transactions"]:
        controller.event("An interrupted deployment needs recovery. Open the controller before updating.")
    elif options.get("automatic_updates", True) and options.get("update_on_start", True):
        controller.submit("startup")
    controller.start_automatic_updates()
    serve(app, host="0.0.0.0", port=8099, threads=6)


if __name__ == "__main__":
    main()
