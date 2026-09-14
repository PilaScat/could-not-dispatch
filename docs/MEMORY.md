# Could Not Dispatch — working notes

Decisions, traps and the release routine. What the plugin does for a user is in the
[README](../README.md).

## Decisions

- **One custom stream per channel.** Dispatcharr keeps the M3U profile a session holds under
  `stream_profile:<stream_id>`, per stream, not per channel. With one shared fallback stream a
  channel sent back from the card could take a provider connection without counting it: a
  provider limited to 3 connections served 4 streams (0.3.0). Dispatcharr counts every custom
  stream as a HDHomeRun tuner, so the advertised tuner count grows by one per channel.
- **The slate is a detached process** serving `http://127.0.0.1:<port>/slate.ts`, not a view
  inside Dispatcharr. Dispatcharr has no scheduler for plugins: the `restart` action is bound
  to `channel_start` and brings the process back, at most once a minute per uwsgi worker.
- **The fallback stays last and off excluded channels.** Apply and Cover new channels detach it
  from channels excluded since, and move it back to the end where a stream was added after it.
- **Sending a channel back** uses `POST /proxy/ts/change_stream/<uuid>` after 120 s on the card,
  then after 4, 8 and 15 minutes. A channel on the card is recognised by its URL.
- **State lives in `.runtime/state.json`**, never in the plugin settings: saving the settings
  replaces the whole object.

## Traps

- Dispatcharr passes `params` to `run()` and never puts them in `context`. An action started by
  an event gets `{"event": ..., "payload": ...}`; a button gets `{}`.
- Only the events in `apps/connect/models.py:SUPPORTED_EVENTS` reach a plugin: 19 in 0.31.0.
  `tests/test_plugin_contract.py` keeps that list.
- From Dispatcharr 0.31.0 a profile URL transform that does not match fails closed. The
  `custom` account's default profile must keep `^(.*)$` → `$1`, or the slate cannot start.
- Importing a zip with `overwrite=true` replaces the plugin folder: copy `no-more-streams.png`
  (or whatever `media_source` points to) and `.runtime/` out first and back after. The new code
  only runs after `POST /api/plugins/plugins/reload/`, and the reload stops every plugin's
  processes, so press Apply afterwards.
- On Windows the `posix_only` and `proc_fs_only` tests are skipped and `mypy .` reports
  `os.WNOHANG`; `mypy --platform linux .` is clean. The CI on Linux is the reference.

## Release

1. Version in `plugin.json`, `could_not_dispatch/constants.py` and `pyproject.toml`.
2. A `CHANGELOG.md` section, written before the tag.
3. `python scripts/build_zip.py`, then an annotated tag `vX.Y.Z` named `Could Not Dispatch X.Y.Z`.
4. A GitHub Release with the CHANGELOG text, the list of commits it contains, and the zip.
5. For the registry, a PR to `Dispatcharr/Plugins` from the fork: bump
   `plugins/could-not-dispatch/plugin.json` and copy the README next to it. The registry
   installs the zip at `source_url`, so a README change reaches users only with a new version.
