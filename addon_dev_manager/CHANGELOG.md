## 0.3.4

- Reconcile interrupted starts with matching installed and managed source versions before attempting rollback.
- Preserve newer installations when a stale recovery record refers to older code.
- Refuse recovery downgrades when an unknown installed version cannot be verified against managed source.

## 0.3.3
- Every deployed Git commit now gets its own Supervisor version ending in `-dev.<sha>`.
- Removes the code-only version reuse that could advance Git state while leaving Home Assistant on an older container version.
- Update to latest repairs legacy mismatches where the recorded SHA is current but the installed/container version belongs to an older commit.

## 0.3.2
- Supervisor-only health is now the default; repository discovery no longer mistakes ingress ports for health-check ports.
- Recovery always uses Supervisor state and cannot be trapped by an optional HTTP health probe.
- Update-all isolates preflight and deployment failures per add-on, so one broken app no longer blocks valid updates.
- Known failures leave recovery state only on the affected add-on; unrelated deployments continue.

## 0.3.1
- Recovery is isolated per add-on: one failed deployment no longer blocks unrelated updates or settings changes.
- Failed add-ons can be removed from monitoring while their recovery journal is retained separately.
- Automatic/startup batch updates skip only add-ons that need recovery.

## 0.2.0

- Add repository scanning and a drop-down picker for add-ons.
- Auto-fill manager ID, repository, branch, source path and standard defaults from each add-on config.
- Keep the health check port prominent while moving uncommon settings under Advanced.
- Add dedicated add-on artwork and refreshed in-app branding.

## 0.1.1

- Enable automatic update checks by default every 60 seconds.
- Add automatic update toggle and configurable interval to Home Assistant configuration and the web UI.
- Serialise checks with manual operations and pause during recovery.

# Changelog

## 0.1.0, 3 October 2026

- Initial experimental release with repository/branch/subdirectory configuration.
- Startup deployment, manual updates and start/stop/restart controls.
- Code-only rebuilds without a repository refresh.
- Batched definition refreshes for configuration and container metadata changes.
- Git version selection, numbered deployment history and explicit recovery.
- Ingress-only administrator access and guarded source ownership.
