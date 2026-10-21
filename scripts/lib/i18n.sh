# SPDX-License-Identifier: MIT
# Shared shell adapter for the Atlas KubeKit message-key localization catalog.

atlas_i18n_library_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
atlas_i18n_python="$atlas_i18n_library_dir/i18n.py"
atlas_i18n_locale="${ATLAS_LANG:-en}"

atlas_i18n_init() {
  local requested="${1:-${ATLAS_LANG:-en}}"
  local assignments
  assignments="$(python3 "$atlas_i18n_python" --bash "$requested")" \
    || { printf '[ERROR] Unsupported language: %s\n' "$requested" >&2; return 2; }
  eval "$assignments"
  atlas_i18n_locale="$requested"
  ATLAS_LANG="$requested"
  export ATLAS_LANG
}

atlas_choose_language() {
  local current="${1:-${ATLAS_LANG:-en}}" selection code name index=0 default=1
  while IFS=$'\t' read -r code name; do
    index=$((index + 1))
    [ "$code" = "$current" ] && default="$index"
    printf ' %d) %s (%s)\n' "$index" "$name" "$code"
  done < <(atlas_i18n_languages)
  read -r -p "$(atlas_t common.language.prompt) [$default]: " selection || return 1
  selection="${selection:-$default}"
  index=0
  while IFS=$'\t' read -r code name; do
    index=$((index + 1))
    if [ "$selection" = "$index" ] || [ "${selection,,}" = "${code,,}" ]; then
      atlas_i18n_init "$code"
      printf '%s\n' "$(atlas_t common.language.selected | sed "s/%s/$code/")"
      return 0
    fi
  done < <(atlas_i18n_languages)
  printf '%s\n' "$(atlas_t common.error.invalid_language)" >&2
  return 2
}

atlas_language_label() {
  local key="common.language.$1"
  atlas_t "$key"
}

atlas_t() {
  local message_key="$1"
  if [ -n "${ATLAS_I18N_MESSAGES[$message_key]+present}" ]; then
    printf '%s' "${ATLAS_I18N_MESSAGES[$message_key]}"
  else
    printf -- "${ATLAS_I18N_MESSAGES[common.error.missing_message]:-Missing translation for message key: %s}" "$message_key"
  fi
}

atlas_printf() {
  local format
  format="$(atlas_t "$1")"
  shift
  printf -- "$format" "$@"
}

atlas_i18n_languages() {
  python3 "$atlas_i18n_python" --list
}
