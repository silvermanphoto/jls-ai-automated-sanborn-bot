# Publishing the public software safely

Joel explicitly chose public distribution of this engine for historians and academics on 2026-09-25. Public sharing does not authorize publication of private project data.

Before each push:

1. Inspect the exact staged file list and file sizes. Keep QGIS projects, credential files, environment files, logs, scans, generated imagery and the working batch/download trees local. Ignore rules do not remove already-tracked files.
2. Run Gitleaks with full redaction against the staged diff. When auditing history, scan all reachable refs as well. Use the default rules, an explicit empty ignore file, and `--ignore-gitleaks-allow` so local exclusions cannot hide findings. Keep reports outside Git.
3. Inspect databases and compressed project contents separately: a text-diff scan cannot prove binary data safe. Check connection fields and private notes, not just filenames.
4. Review every alert. The engine's `approval_token` is a SHA-256 evidence checksum, not an authentication credential; verify that context rather than ignoring all token alerts.
5. Stop for any actual credential or sensitive-data finding. Do not print its value, publish it, or silently rewrite history. Preserve local originals and request a specific remediation decision when needed.

Gitleaks 8.30.1 staged command (CONFIG extends the default rules; EMPTY_IGNORE is an empty file):

```sh
gitleaks git --pre-commit --staged --redact=100 --no-banner --no-color \
  --ignore-gitleaks-allow --config CONFIG --gitleaks-ignore-path EMPTY_IGNORE \
  --report-format json --report-path LOCAL_REPORT --timeout 120 .
```

For history, replace `--pre-commit --staged` with `--log-opts=--all`. A clean scan is useful evidence, not a guarantee that every kind of sensitive information has been detected.

QGIS imports retain a local dated backup under `_local/qgis-project-backups/`. They never commit or push that backup. Existing historical backups were removed from the current published file list without deleting local copies or rewriting history.

Software releases exclude the complete `batch/` and `1911 SANBORN DOWNLOADS/` trees, including queue databases, review packets and provenance records. Existing local files are preserved when removed from public tracking. Publishing a dataset is a separate decision from sharing the software.
