#!/usr/bin/env bash
# Installs SSHStation, a refined SSH server manager for the terminal. An Idea by Vaibhav Kite.
#
#   ./install.sh               install the `sshstation` command
#   ./install.sh <name>        install it under a name you choose, e.g. ./install.sh sst
#   ./install.sh --uninstall   remove SSHStation (your saved servers are kept)
set -euo pipefail

NAME="sshstation"
UNINSTALL=0
for arg in "$@"; do
  case "$arg" in
    --uninstall) UNINSTALL=1 ;;
    -h|--help) sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) NAME="$arg" ;;
  esac
done

APP_DIR="${SSHSTATION_HOME:-$HOME/.local/share/sshstation}"
BIN_DIR="${SSHSTATION_BIN_DIR:-$HOME/.local/bin}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

brass=$'\033[38;2;232;184;92m'; bold=$'\033[1m'; dim=$'\033[2m'; red=$'\033[38;2;255;90;95m'; reset=$'\033[0m'
say()  { printf '  %s\n' "$*"; }
fail() { printf '  %s%s%s\n' "$red" "$*" "$reset" >&2; exit 1; }

printf '\n  %s%s◆ SSHStation%s  %sAn Idea by Vaibhav Kite%s\n\n' "$brass" "$bold" "$reset" "$dim" "$reset"

if [[ $UNINSTALL -eq 1 ]]; then
  if [[ -f "$APP_DIR/commands" ]]; then
    while IFS= read -r cmd; do
      [[ -n "$cmd" ]] && rm -f "$BIN_DIR/$cmd" && say "Removed the $cmd command."
    done < "$APP_DIR/commands"
  fi
  rm -rf "$APP_DIR"
  say "SSHStation is uninstalled. Your servers are still in ~/.config/sshstation/servers.json."
  echo
  exit 0
fi

[[ "$NAME" =~ ^[A-Za-z0-9._-]+$ ]] || fail "Command names can only use letters, numbers, dots, dashes and underscores."
[[ -f "$SRC_DIR/sshstation.py" ]] || fail "sshstation.py wasn't found next to install.sh."

existing="$(command -v "$NAME" 2>/dev/null || true)"
if [[ -n "$existing" && "$existing" != "$BIN_DIR/$NAME" ]]; then
  say "${dim}Note: $existing already exists. Run ./install.sh <other-name> if you'd rather not shadow it.${reset}"
fi

PY=""
for candidate in python3.14 python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
    PY="$(command -v "$candidate")"
    break
  fi
done
[[ -n "$PY" ]] || fail "SSHStation needs Python 3.9 or newer. Install it with: brew install python"

say "Using $("$PY" --version 2>&1) from $PY"
mkdir -p "$APP_DIR" "$BIN_DIR"

if [[ ! -x "$APP_DIR/venv/bin/python" ]]; then
  say "Creating a private Python environment…"
  "$PY" -m venv "$APP_DIR/venv"
fi
say "Installing Textual…"
"$APP_DIR/venv/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade pip
"$APP_DIR/venv/bin/python" -m pip install --quiet --disable-pip-version-check "textual>=8.2,<9"

cp "$SRC_DIR/sshstation.py" "$APP_DIR/sshstation.py"

cat > "$BIN_DIR/$NAME" <<LAUNCHER
#!/usr/bin/env bash
export SSHSTATION_PROG_NAME="$NAME"
export SSHSTATION_LAUNCHER="$BIN_DIR/$NAME"
exec "$APP_DIR/venv/bin/python" "$APP_DIR/sshstation.py" "\$@"
LAUNCHER
chmod +x "$BIN_DIR/$NAME"
touch "$APP_DIR/commands"
grep -qx "$NAME" "$APP_DIR/commands" || echo "$NAME" >> "$APP_DIR/commands"

# Tidy up earlier versions (SSHMan, and before that hop). Their servers are copied over on first run.
for old in sshman:sshman.py hop:hop.py; do
  old_dir="$HOME/.local/share/${old%%:*}"
  old_script="$old_dir/${old##*:}"
  [[ -f "$old_dir/commands" ]] || continue
  while IFS= read -r cmd; do
    if [[ -n "$cmd" && -f "$BIN_DIR/$cmd" ]] && grep -q "$old_script" "$BIN_DIR/$cmd"; then
      rm -f "$BIN_DIR/$cmd"
      say "Removed the old $cmd command. Your servers come across the first time you run $NAME."
    fi
  done < "$old_dir/commands"
  rm -rf "$old_dir"
done

echo
say "${bold}Installed.${reset} Run ${brass}${bold}$NAME${reset} to open your servers, or ${brass}${bold}$NAME --demo${reset} to look around first."
say "Your servers are kept in ${bold}~/.config/sshstation/servers.json${reset}; ${brass}$NAME --edit${reset} opens it."

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    echo
    say "$BIN_DIR isn't on your PATH yet. Add it with:"
    say "  echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> ~/.zshrc && source ~/.zshrc"
    ;;
esac
echo
