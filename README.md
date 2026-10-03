# Add-on Development Manager

A Home Assistant add-on for deploying GitHub source to local add-ons. Configure a list of repositories, then restart the manager or use its web interface to update, select a commit, start, stop, restart or roll back an add-on.

## What makes updates faster

**Code-only changes skip the repository refresh.** The manager downloads the selected commit, confirms the target is stopped, replaces its local source and asks Supervisor to rebuild its container.

If the Home Assistant definition changes, the manager reloads store metadata once for the batch, then updates the containers. This includes configuration schema/defaults, permissions, ports, build configuration, AppArmor and translations. Supervisor currently exposes only a whole-store reload API, so this path can still wait on other repositories. Docker build and dependency installation time also remains.

The interface distinguishes the **source version and commit** from the **container definition version**. Code-only deployments keep the existing definition version to avoid a store reload. A changed definition gets a version such as `1.2.3-dev.abcdef123456`. Git history records the exact source actually deployed in either case. For repositories containing several add-ons, changes outside the selected subdirectory are skipped as well.

Automatic updates are enabled by default. The controller checks enabled entries every 60 seconds and deploys changed source, even when the source version number has not changed. Configure **Automatic updates** and **Check interval** in Home Assistant or **Repositories & settings**. Turning automatic updates off also disables startup deployments; manual updates still work. Settings saved in the web UI take effect immediately; changes in Home Assistant Configuration require restarting the manager.

The interval bounds the wait until the next check only while the controller is idle and GitHub is reachable. Downloads, builds, global store refreshes for configuration changes and startup checks take additional time. Busy operations delay polling. Interrupted deployments pause it until recovery. There is no guaranteed maximum completion time. Each entry’s startup toggle controls startup deployment only; regular polling includes all enabled entries.

## Install

1. Open **Settings → Apps → App store → ⋮ → Repositories**. Older Home Assistant versions call these Add-ons.
2. Add `https://github.com/justcop/ha-addon-dev-manager`.
3. Install **Add-on Development Manager**, then start it and select **Open Web UI**.
4. Open **Repositories & settings** and add each target. Settings are saved through Supervisor into the manager's own Home Assistant configuration.
5. Select **Install latest** for the first local deployment.

This first release is experimental. Automated tests cover the deployment contract and security boundaries, but it has not yet been installed on a real Home Assistant host.

### Example configuration

The same repository may supply several add-ons. Set `path` to the individual add-on directory, or `.` if the add-on is at the root.

```yaml
update_on_start: true
github_token: ""
stop_timeout: 60
health_timeout: 120
repositories:
  - id: vinyl
    repository: https://github.com/justcop/home-assistant-addons
    branch: main
    path: vinyl_guardian
    enabled: true
    update_on_start: true
    health_port: 0
    health_path: /
```

`id` must be unique, start with a lowercase letter, and use lowercase letters, digits and underscores. A deployed ID remains bound to its repository and directory. Change branches freely; use a new ID for a different repository or add-on path.

### Startup behaviour

With global `update_on_start: true`, starting or restarting this controller checks all enabled entries whose own `update_on_start` is true. Unchanged commits are skipped without stopping the target. **Update all enabled add-ons** ignores the per-entry startup switch and updates all enabled entries.

Updates run in the background. You can open the interface while they run and see their progress. If a previous deployment was interrupted, startup updates pause until you recover it. The controller never tries to update itself.

### Private repositories

Set `github_token` in the controller's Home Assistant **Configuration** tab. Use a fine-grained GitHub token with read-only Contents access to the repositories you need. Public repositories require no token.

The web UI never receives the token. Git authentication is supplied in the child process environment, not in a remote URL, command-line argument or persistent Git configuration. The token is still present in this controller's Home Assistant options and its backups. Only trusted repository source should be built, since Dockerfiles and application code execute locally.

## Moving existing add-ons to local development

Repository-installed and local add-ons have **different Home Assistant identities and separate private `/data` volumes**. This manager does not automatically take over repository installations or copy their private data.

1. Take a Home Assistant backup of the original add-on and record its configuration, exposed ports and integrations.
2. Stop the original add-on and disable its automatic start and watchdog before running its local counterpart.
3. Install the local version through this manager. Configure the new local add-on in Home Assistant.
4. If the original uses persistent private data, arrange an app-specific transfer before using the local version with live data. Do not assume the original `/data` appeared in the new container. Shared or explicitly mapped external data may already be available.
5. Keep the stopped original until the local deployment and its integrations are confirmed working.

The controller refuses a first local deployment if the repository-installed counterpart is running. It also refuses to overwrite an existing unmanaged local add-on or an unrelated directory. Local containers remain normal Supervisor-managed add-ons, including their own configuration menu and ingress.

## Deployment and recovery

1. Fetch Git branches/tags and pin the selected full commit SHA.
2. Stage and validate every selected add-on before stopping any target. The selected folder must contain one `config.yaml`, `config.yml` or `config.json` and a `Dockerfile`.
3. Write a durable deployment journal, pause the target's watchdog, stop the target and wait for confirmed stop.
4. Replace only source owned by this manager, publishing the definition last. Persistent add-on data is not touched.
5. If needed, refresh Supervisor definitions once per batch. Ask Supervisor to install, update or rebuild as appropriate. Source deployments remove `image` from their local config so Supervisor builds the selected source instead of pulling an unrelated published image.
6. Validate persisted settings, start the target and check it remains started. Optionally check an HTTP endpoint on the add-on's internal IP.
7. Restore its watchdog setting and record a numbered, timestamped successful deployment.

`health_port: 0` checks container state only. A positive port requires a successful HTTP 2xx response at `health_path`, sustained with the started state for five seconds. Select an unauthenticated health endpoint if available. HTTP redirects are not followed, and requests stay on the add-on's Supervisor-provided internal IP. No health endpoint is inferred from an application's Spotify or other authenticated pages.

**Rollback** deploys the most recent different successful commit. **Versions** allows any cached commit to be selected. Use **Check GitHub** before opening Versions to fetch new history. The picker displays up to 80 commits; deployment history retains the latest 200 events. Deployed commits are pinned in local Git storage, so recovery can work without GitHub and there are no permanent full-directory source backups.

**Recovery is deliberately explicit.** After an error, the journal remains and the interface shows **Recover**. For an interrupted update it restores the previous successful source; for a failed first install it retries that candidate. If Supervisor is still running a build or update after connection loss, recovery refuses to race it. Previously stopped apps remain stopped after recovering an existing deployment.

Code rollback does not undo database migrations, configuration edits or recordings. If new code changed its data format, use a compatible Home Assistant data backup before selecting older code. The manager does not blindly roll back on failed startup checks.

## Access and permissions

The UI accepts TCP connections only from Supervisor ingress (`172.30.32.2`). It verifies the authenticated Home Assistant username is an active administrator/owner using Supervisor's `/auth/list`, rather than treating `panel_admin` as access enforcement. It fails closed if that check is unavailable. Mutations also require a per-process request token, and credentials are excluded from status/lifecycle responses.

The controller uses `hassio_role: admin` to perform that administrator check and manage deployments, plus write access to `/addons`. It does not expose host ports, request Docker socket access, enable host networking, disable protection mode or mount other add-ons' private data. Its authority is substantial; keep it restricted to your own trusted development repositories.

GitHub HTTPS repositories are supported. Symlinks, submodules and Git LFS-based source are not supported by this version. Parent paths, credential-bearing URLs, nested add-on definitions, directory symlinks and duplicate add-on slugs are rejected. Do not manually edit the controller's managed directories.

## Development and validation

```sh
python -m venv .venv
.venv/bin/pip install -r addon_dev_manager/requirements.txt pytest
.venv/bin/python -m pytest -q
node --check addon_dev_manager/manager/static/app.js
docker build -t addon-dev-manager:test addon_dev_manager
```

The tests use real Git commits and a separate Supervisor contract model, covering first installs, source updates, schema changes, skipped refreshes, stop confirmation, interrupted deployments, offline recovery, watchdog restoration, version rollback, preflight failures, source ownership and ingress security. CI also builds the controller container on Linux.

Supervisor API behaviour was checked against Home Assistant's developer documentation and upstream source on 3 October 2026. Supported configurations should be checked on your host before migrating live add-on data.

## References

- [Local add-on development](https://developers.home-assistant.io/docs/apps/testing/)
- [Supervisor API](https://developers.home-assistant.io/docs/api/supervisor/endpoints/)
- [Add-on configuration](https://developers.home-assistant.io/docs/apps/configuration/)
- [Ingress requirements](https://developers.home-assistant.io/docs/apps/presentation/#ingress)
