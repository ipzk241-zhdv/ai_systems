# Automatic Oracle deployment

Repository: `ipzk241-zhdv/ai_systems`, production branch: **master**.
Service: `news-bot`, application: `/home/ubuntu/ai_systems/mkr`.

## What happens on a push

1. Changes under `mkr/` or the bot workflow trigger GitHub Actions.
2. A GitHub-hosted Ubuntu runner installs test dependencies and runs both unit-test suites. No Telegram/Groq keys or live AI requests are needed for tests.
3. Only a successful push to `master` (or manual run on `master`) can deploy. Pull requests only run tests.
4. SSH requests `deploy <tested commit SHA>` on the VM with a dedicated key.
5. The VM fetches master, rejects dirty tracked files, skips superseded commits, and tests the candidate code in a temporary directory before stopping the bot.
6. It stops the service, snapshots each SQLite database, updates the checkout to the tested commit, starts the service and checks its process stays active for 24 seconds.
7. On failure it restores the previous code and restarts the service; GitHub marks the deployment as failed.

`.env` and `.runtime` are untracked and preserved. Deployment never runs `git clean`.
Database snapshots live at `~/.local/state/news-bot-deploy/backups/` and the last successful
commit is recorded in `~/.local/state/news-bot-deploy/last-success.json`.
These are on-VM backups; copy important history off the VM as well. Backups currently accumulate,
so remove older ones when no longer needed. Code rollback does not automatically revert database
migrations: an incompatible migration may require restoring its pre-deploy database snapshot.

## One-time secret

At https://github.com/ipzk241-zhdv/ai_systems/settings/secrets/actions create:

| Secret | Value |
|---|---|
| `ORACLE_SSH_KEY` | Entire dedicated **private** deployment key, including BEGIN/END lines |

Do not upload your Telegram token, Groq key or `.env` to Actions. They remain on the VM.
The key generated for the initial setup is in ignored `mkr/deploy/.local/oracle-actions` on the PC.
Only its `.pub` half is installed on the VM. Never commit or paste the private half into an issue/chat.

The workflow pins the VM's already verified ED25519 host key and refuses a different host key.
It pins third-party Actions to full commit SHAs. If the VM/IP/host key changes, update the workflow
after verifying the replacement VM's host fingerprint.

## One-time VM setup

Install the dedicated public key and the root-owned deployment handler using:

```sh
bash mkr/deploy/bootstrap_vm.sh /path/to/oracle-actions.pub
```

The key has `restrict` and a forced deployment command. It cannot open an interactive shell,
forward ports, or use SCP. The original SSH key remains available for normal administration.
The handler is installed outside the Git checkout, so deploying code cannot silently replace
the SSH entry point. Re-run bootstrap explicitly if intentionally changing the handler.

The running service and original SSH key are not changed by bootstrap. Deployment reuses
the installed systemd service; it does not replace its configuration on each push.

## Using it

Commit and push bot changes to **master**. Open:
https://github.com/ipzk241-zhdv/ai_systems/actions

You can also choose **News bot CI and Oracle deployment → Run workflow** on master.
The first run needs the Actions secret and the VM setup above.
If only unrelated lab files change, this workflow does not run.

If deployment fails, the previous code is normally restored. Read the deployment step output,
then use your normal SSH key to inspect `sudo journalctl -u news-bot -n 80 --no-pager`.
An active process check confirms startup stability, not answer quality or every external website.
No test messages are sent to Telegram during CI/deployment.

The VM uses Ubuntu's existing Python packages, without `.venv`. If you add a new system/package
dependency, install it on the VM first; CI installing a package does not install it on the VM.
VM-side tests catch missing Python dependencies before the running bot is stopped.

Do not edit tracked application code directly on the VM. Local tracked changes intentionally
block automatic deployment instead of being overwritten. Keep VM configuration in `.env`.

The handler refreshes `origin/master` even after an intentional history rewrite, then
validates the exact requested commit before changing application files. Avoid force-pushing
for ordinary updates: after an automated commit, pull the latest master before editing.
If you change `deploy_vm.py`, also refresh its installed root-owned copy on the VM;
ordinary deployments intentionally cannot update their own SSH entry point.

GitHub environment `oracle` shows deployment history. Deployment concurrency is serialized;
pull-request workflows never receive the deployment key. Revoke the key by removing its line
from `~/.ssh/authorized_keys` and deleting/replacing the Actions secret.
