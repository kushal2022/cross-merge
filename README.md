# Cross-Merge Console

Local web UI for cross-merging one source branch into many target branches across
several repos (e.g. Aspen and Aspen+), with Bitbucket PRs opened on conflict.

```bash
python3 server.py            # opens http://127.0.0.1:8765
```

Python 3.8+, standard library only. Git must be able to push without prompting
(SSH key or credential helper), the same as the original shell script.

- **Repositories**: group (Aspen / Aspen+), Bitbucket repo slug, project key, local clone path.
- **Target branches**: one row per target, with reviewers and the repos it applies to.
- **Dry run** tests every merge locally and resets; **Live run** pushes clean merges and,
  on conflict, pushes `Sprint<N>-<src>-Crossmerge-to-<target>-<date>` and opens a PR from the source into it.
- Settings save to `crossmerge_config.json` (the API key is never saved). Logs go to `logs/`.
