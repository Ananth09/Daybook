#!/bin/bash
# Daybook installer. Run once in EACH macOS account that should have Daybook:
#   bash /path/to/daybook/install.sh
# Each account gets its own private log, its own timesheet folder, and uses
# whichever Claude account is signed in to Claude Code in that macOS account.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
SUPPORT="$HOME/Library/Application Support/Daybook"
APPDIR="$SUPPORT/app"
VENV="$SUPPORT/venv"
LABEL="com.daybook.app"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LAUNCHER="$HOME/Applications/Daybook.app"
USER_ID="$(id -u)"

say() { printf "\n\033[1m%s\033[0m\n" "$1"; }

say "Installing Daybook for Mac account: $(id -un)"

# 1. Python 3 that every Mac account can use (Apple's, from the Command Line Tools)
PY=""
if /usr/bin/xcode-select -p >/dev/null 2>&1 && /usr/bin/python3 -c 'import venv, ensurepip' >/dev/null 2>&1; then
  PY=/usr/bin/python3
else
  for c in /opt/homebrew/bin/python3 /usr/local/bin/python3 /Library/Frameworks/Python.framework/Versions/Current/bin/python3; do
    if [ -x "$c" ] && "$c" -c 'import venv, ensurepip' >/dev/null 2>&1; then PY="$c"; break; fi
  done
fi
if [ -z "$PY" ]; then
  echo "Daybook needs Python 3. A window will ask to install Apple's Command Line Tools."
  echo "Click Install, wait for it to finish, then run this installer again."
  /usr/bin/xcode-select --install >/dev/null 2>&1 || true
  exit 1
fi
echo "Using $("$PY" --version 2>&1) at $PY"

# 2. Private environment with the Excel library
mkdir -p "$SUPPORT" "$APPDIR"
chmod 700 "$SUPPORT"
if [ ! -x "$VENV/bin/python" ]; then
  "$PY" -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade pip >/dev/null 2>&1 || true
"$VENV/bin/python" -m pip install --quiet --disable-pip-version-check "openpyxl>=3.1,<4"
echo "Excel support installed."

# 3. App files
cp "$SRC/daybook.py" "$SRC/ui.html" "$APPDIR/"
mkdir -p "$HOME/Daybook/Timesheets"
"$VENV/bin/python" "$APPDIR/daybook.py" url >/dev/null   # creates this account's private settings

# 4. Keep Daybook running in the background for this account (starts at login)
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$VENV/bin/python</string>
    <string>$APPDIR/daybook.py</string>
    <string>serve</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$HOME/.local/bin:$HOME/.claude/local:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>StandardOutPath</key><string>$SUPPORT/service.log</string>
  <key>StandardErrorPath</key><string>$SUPPORT/service.log</string>
</dict>
</plist>
PLIST
launchctl bootout "gui/$USER_ID/$LABEL" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$USER_ID" "$PLIST" 2>/dev/null || launchctl load -w "$PLIST"
launchctl kickstart -k "gui/$USER_ID/$LABEL" >/dev/null 2>&1 || true
echo "Background service started (starts again automatically when you log in)."

# 5. A Daybook app icon in ~/Applications (shows up in Spotlight and Launchpad)
rm -rf "$LAUNCHER"
mkdir -p "$LAUNCHER/Contents/MacOS" "$LAUNCHER/Contents/Resources"
cat > "$LAUNCHER/Contents/MacOS/Daybook" <<'SH'
#!/bin/bash
exec "$HOME/Library/Application Support/Daybook/venv/bin/python" "$HOME/Library/Application Support/Daybook/app/daybook.py" open
SH
chmod +x "$LAUNCHER/Contents/MacOS/Daybook"
cat > "$LAUNCHER/Contents/Info.plist" <<'PL'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Daybook</string>
  <key>CFBundleDisplayName</key><string>Daybook</string>
  <key>CFBundleIdentifier</key><string>local.daybook.launcher</string>
  <key>CFBundleExecutable</key><string>Daybook</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleIconFile</key><string>Daybook</string>
  <key>LSUIElement</key><true/>
</dict>
</plist>
PL
if [ -f "$SRC/Daybook.icns" ]; then cp "$SRC/Daybook.icns" "$LAUNCHER/Contents/Resources/Daybook.icns"; fi
touch "$LAUNCHER"

# 6. Terminal shortcut:  daybook add "fixed the login bug"
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/daybook" <<'SH'
#!/bin/bash
exec "$HOME/Library/Application Support/Daybook/venv/bin/python" "$HOME/Library/Application Support/Daybook/app/daybook.py" "$@"
SH
chmod +x "$HOME/.local/bin/daybook"

# 7. Claude Code for this account
CLAUDE_BIN=""
for c in "$HOME/.local/bin/claude" "$HOME/.claude/local/claude" /opt/homebrew/bin/claude /usr/local/bin/claude; do
  if [ -x "$c" ]; then CLAUDE_BIN="$c"; break; fi
done
[ -z "$CLAUDE_BIN" ] && CLAUDE_BIN="$(/bin/zsh -lc 'command -v claude' 2>/dev/null || true)"

say "Daybook is installed."
echo "  Open it:          Daybook in Spotlight, or ~/Applications/Daybook"
echo "  Timesheet folder: ~/Daybook/Timesheets  (put each month's .xlsx here; change it in Settings)"
echo "  From Terminal:    daybook add \"what you did\"   (if ~/.local/bin is on your PATH)"
if [ -n "$CLAUDE_BIN" ]; then
  echo "  Claude Code:      found at $CLAUDE_BIN"
  echo "                    If you haven't yet, run 'claude' once in this Mac account and sign in"
  echo "                    with the Claude account you want Daybook to use here."
else
  echo "  Claude Code:      not installed for this Mac account. Daybook works with simple rules until it is."
  echo "                    Install it (see https://code.claude.com/docs), run 'claude' once and sign in,"
  echo "                    then click Settings > Test Claude in Daybook."
fi
sleep 1
"$VENV/bin/python" "$APPDIR/daybook.py" open >/dev/null 2>&1 || true
