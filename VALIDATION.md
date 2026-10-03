# Validation record

Validation date: 3 October 2026.

## Completed

- 54 automated tests passed using Python 3.12, real Git repositories and an independently implemented Supervisor contract model.
- Python modules compiled successfully; JavaScript syntax validation passed.
- Browser workflow exercised repository settings, initial installation, commit selection, a failed update and recovery through the real application routes with the Supervisor model.
- Desktop and mobile layouts inspected. No JavaScript errors or horizontal mobile overflow were found.
- The current `justcop/home-assistant-addons` main branch was fetched through the production Git client. `vinyl_guardian` at commit `49d35a0b0aea` (source version `5.9.0`) staged successfully.

## Behaviour covered

1. Initial local installation builds selected source instead of pulling the upstream image.
2. Code-only updates, including upstream version bumps, skip repository refresh and rebuild directly.
3. Changes outside the selected add-on directory skip deployment.
4. Schema, translation and definition changes refresh metadata and preserve user settings.
5. Multi-add-on batches refresh metadata once.
6. All source preflight checks complete before targets stop.
7. Stop confirmation and watchdog handling protect source replacement.
8. Interrupted builds, publication and startup checks leave recoverable journals.
9. Recovery restores pinned commits offline and refuses to race Supervisor jobs.
10. Rollback selects the previous successful deployment and records its exact source commit.
11. Unmanaged directories, duplicate slugs, symlinks, unsafe archive paths and invalid repository URLs are rejected.
12. Ingress peer checks, administrator verification, request tokens and credential redaction restrict web access.

## Not yet verified

- A controller Docker image build in this execution environment, which has no Docker executable. The repository includes a CI build step.
- Installation or deployment against a real Home Assistant Supervisor.
- Migration of an existing add-on's private data, hardware access or integrations. Migration is deliberately manual and documented.

The first release is experimental. These checks demonstrate the implementation against the documented Supervisor contract, not a completed installation test on a Home Assistant host.
