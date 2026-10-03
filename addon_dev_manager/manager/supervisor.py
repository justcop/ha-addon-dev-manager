"""Authenticated Supervisor client. Mutating timeouts are never assumed failures."""

import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class SupervisorError(RuntimeError):
    pass


class UncertainMutation(SupervisorError):
    """Supervisor may still be running a mutation after transport loss."""


class Supervisor:
    def __init__(self, token=None, base="http://supervisor"):
        self.token = token if token is not None else os.environ.get("SUPERVISOR_TOKEN", "")
        self.base = base.rstrip("/")

    def request(self, method, path, data=None, timeout=1800):
        if not self.token:
            raise SupervisorError("SUPERVISOR_TOKEN is missing. Run this inside the add-on.")
        req = Request(self.base + path, method=method,
                      data=json.dumps(data or {}).encode() if method == "POST" else None,
                      headers={"Authorization": "Bearer " + self.token,
                               "Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=timeout) as response:
                result = json.load(response)
        except HTTPError as exc:
            raise SupervisorError(f"Supervisor {path} returned HTTP {exc.code}") from None
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            error = UncertainMutation if method == "POST" else SupervisorError
            raise error(f"Supervisor connection lost during {path}. Check operation status before retrying.") from exc
        if result.get("result") != "ok":
            raise SupervisorError(f"Supervisor rejected {path}. Check its logs for details.")
        return result.get("data") or {}

    def get(self, path):
        return self.request("GET", path, timeout=30)

    def post(self, path, data=None):
        return self.request("POST", path, data)

    def installed(self):
        return {a["slug"]: a for a in self.get("/addons").get("addons", [])}

    def info(self, slug):
        return self.get(f"/addons/{slug}/info")

    def wait_state(self, slug, desired, timeout):
        deadline = time.monotonic() + timeout
        while True:
            info = self.info(slug)
            if info.get("state") == desired:
                return info
            if time.monotonic() >= deadline:
                raise SupervisorError(f"Timed out waiting for {slug} to become {desired}")
            time.sleep(1)

    def ensure_idle(self, slug):
        """Do not race an interrupted Supervisor build/update job."""
        def running(job):
            touches_target = (job.get("reference") == slug
                              or str(job.get("name", "")).startswith("store_manager_"))
            return ((touches_target and not job.get("done", False))
                    or any(running(child) for child in job.get("child_jobs", [])))
        if any(running(job) for job in self.get("/jobs/info").get("jobs", [])):
            raise SupervisorError("Supervisor is still working on this add-on. Wait before recovering.")
