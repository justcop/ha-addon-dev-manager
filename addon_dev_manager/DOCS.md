# Add-on Development Manager

Open **Web UI** to configure repository entries, deploy code, select earlier commits and control the managed local add-ons.

With **Update on startup** enabled, restarting this manager checks and deploys new commits for entries with their own startup switch enabled. Unchanged commits are skipped. Code-only changes rebuild directly; changes to the Home Assistant definition require a repository refresh.

Each entry needs an ID, GitHub repository, branch and add-on directory. For Vinyl Guardian, use repository `justcop/home-assistant-addons`, branch `main`, directory `vinyl_guardian`. The same repository can supply several different entries. Leave HTTP health port at `0` to use a container-state check, or configure a dedicated health endpoint.

Set a read-only **GitHub token** here in Home Assistant's Configuration tab if a repository is private. It is never displayed in the manager UI. Timeout settings can also be adjusted here.

Existing repository-installed add-ons must be backed up and stopped before installing their local counterparts. Local add-ons have different identities and fresh private data directories. This manager does not automatically migrate private data or integrations. Keep the stopped original until migration is verified.

After an interrupted deployment, use **Recover**. For an existing deployment it restores the previous commit; for a first installation it retries the candidate. Recovery refuses to run while Supervisor is still working on that add-on. Code recovery does not reverse changes to application data.

The interface verifies your Home Assistant administrator account through Supervisor ingress. The add-on requests Supervisor admin access for this check and lifecycle management, and write access to the local add-on directory. There are no exposed host ports or Docker socket requirements.

Full setup, migration and recovery instructions: [README](https://github.com/justcop/ha-addon-dev-manager#readme).
