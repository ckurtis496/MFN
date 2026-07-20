# MFN.se → Discord

Polls https://mfn.se/all every 5 minutes and posts new press releases to a
Discord channel via webhook. Runs entirely on GitHub Actions' free tier — no
paid third-party service involved.

## Setup (5 minutes)

1. Create a new GitHub repo (Settings can be Public — the code has no
   secrets in it, and public repos get unlimited free Actions minutes).
2. Add these two files to the repo, preserving the path:
   - `mfn_notifier.py`
   - `.github/workflows/mfn-discord.yml`
3. Repo → Settings → Secrets and variables → Actions → New repository secret:
   - Name: `DISCORD_WEBHOOK_URL`
   - Value: your Discord webhook URL
4. Repo → Settings → Actions → General → Workflow permissions → set to
   "Read and write permissions" (needed so the workflow can commit
   `state.json` back after each run).
5. Push. The workflow starts running automatically every 5 minutes. You can
   also trigger it manually from the Actions tab ("Run workflow") to test it
   immediately instead of waiting for the next tick.

The first run only records what's currently on the page — it won't dump the
whole existing feed into Discord. Every run after that posts only genuinely
new items.

## Narrowing the scope

By default this watches the entire Nordic MFN feed, which is high volume
(dozens of releases/hour across all listed companies). To watch a single
company instead, uncomment `MFN_URL` in `.github/workflows/mfn-discord.yml`
and point it at that company's MFN page, e.g.:

```yaml
MFN_URL: https://mfn.se/all/a/carasent
```

## Notes / limitations

- GitHub's schedule trigger is best-effort — under platform load it can fire
  a few minutes late. Not an issue for this use case.
- GitHub disables scheduled workflows automatically after 60 days of repo
  inactivity. A push or manual run re-enables it.
- If MFN.se changes its page markup, the regex in `mfn_notifier.py` will
  stop matching and the script will log a warning instead of posting
  garbage. Ping me if that happens and I'll update the parser.
