# shellcheck shell=bash
# XDG base directory for the install scripts. Sourced, not executed.

# The config home as the spec says, and as awaitray itself reads it: unset,
# empty or relative means ~/.config. A relative XDG_CONFIG_HOME must never
# turn into a folder under wherever the script was started (uninstall
# --purge deletes in there).
config_home() {
  case "${XDG_CONFIG_HOME:-}" in
    /*) printf '%s\n' "$XDG_CONFIG_HOME" ;;
    *) printf '%s\n' "$HOME/.config" ;;
  esac
}
