# Atlas KubeKit internationalization

## Current text inventory

| Source | Text to externalize | Keep in source / runtime |
| --- | --- | --- |
| `03-install-addons.sh` | Message-key catalog now supplies the language menu, add-on menu and descriptions, download-source help/labels, prompts, warnings, completion messages, and Longhorn replica guidance; remaining bootstrap/remote dependency diagnostics still need migration | Plugin IDs/names used as Kubernetes/Helm values, shell commands, chart output, resource names, and third-party tool output |
| `scripts/addons/sources.py` | Download/compatibility status labels and diagnostics returned to the operator | Registry/URL values, compatibility metadata, HTTP errors from upstream, and data passed to Helm |
| `04-verify-cluster.sh` | Prompts, role menus, status labels, summaries, check results, and CLI errors now use message keys and shared locale selection | Command invocations, resource/API identifiers, raw kubectl/systemd output, and discovered cluster data |
| `01-prepare-node.sh`, `02-deploy-cluster.sh` | Phase 1 is migrated. Phase 2 now chooses a language at startup (or accepts `--lang`), remembers it for automatic resume, and externalizes its CLI help/status, core deployment prompts, storage plan, and main progress messages. The Python `--check`/`--repair` reports and some detailed Phase 2 validation/repair diagnostics remain to be migrated. | Pairing-code alphabet/protocol, option tokens, hostnames, IPs, file paths, systemd names, and command output |
| `scripts/stages/*.sh`, `scripts/stages/*.py` | Operator-facing output from deployment, check, repair, and storage flows; existing Chinese in these files is mostly source comments | Comments, shell protocol strings, resource/config identifiers, remote command payloads, and upstream tool output |
| `scripts/update-version.py`, sync/build helpers | Only messages intended for the repository maintainer during normal tool use | Generated payload markers, file names, command flags, and machine-readable output |

This inventory separates human-facing text from values that are part of Kubernetes, Linux, shell, or network contracts. The shared catalogs and Bash/Python lookup APIs are in place, but the migration is not complete: remaining 03 bootstrap/remote messages, Phase 2 detailed diagnostics and check/repair helpers, and English-only stage messages still need their call sites converted to message keys. Until a call site is migrated, it continues to display its current text; catalog fallback only applies after that call site uses the lookup API. Do not describe the scripts as fully localized until the call-site inventory is empty.

User-facing messages use stable message keys and locale catalogs. Locale files are UTF-8 JSON under `locales/`:

- `en.json` is the required fallback catalog and contains the complete message-key set.
- `zh_cn.json` is Simplified Chinese and falls back to English for untranslated messages.
- `zh_tw.json` is Traditional Chinese and falls back to Simplified Chinese, then English.

Each catalog has `locale`, `displayName`, `fallback`, and `messages`. New languages are added by creating another `<locale>.json` file, setting its display name and fallback, then adding translations under the same message keys. Script logic and menus should discover available locales rather than enumerate language codes in each script.

Use namespaced keys such as `common.prompt.confirm`, `phase03.addon.longhorn.description`, and `phase04.check.nodes.title`:

```bash
prompt="$(atlas_t common.language.prompt)"
```

```python
from scripts.lib.i18n import translate

title = translate("zh_CN", "phase04.menu.title")
```

Keep printf placeholders (`%s`, `%d`) identical across translations. Do not translate command names, Kubernetes API identifiers, resource names, configuration values, paths, or output emitted by third-party tools.

`scripts/lib/i18n.py` implements fallback lookup, interpolation, locale discovery, and catalog validation. Bash entrypoints use `scripts/lib/i18n.sh`; Python helpers import the shared Python module. Phase 1, 3, and 4 entrypoints depend on the shared lookup runtime and locale catalogs; deployment bundles must include `locales/` and `scripts/lib/`. Phase 3 source-code synchronization embeds only the source-policy helper, not locale catalogs.

Run `python3 scripts/lib/i18n.py --validate` while authoring catalogs. Call sites should pass message keys to `atlas_t` (shell) or `translate` (Python), never embed a translated sentence as the lookup key.
