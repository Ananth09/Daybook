#!/bin/bash
# Removes Daybook from THIS macOS account.
#   bash uninstall.sh                 keep your notes and timesheets
#   bash uninstall.sh --delete-notes  also delete this account's notes
# Your Excel files in the timesheet folder are never deleted.
set -u
LABEL="com.daybook.app"
SUPPORT="$HOME/Library/Application Support/Daybook"
launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || launchctl unload "$HOME/Library/LaunchAgents/$LABEL.plist" >/dev/null 2>&1
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist" "$HOME/.local/bin/daybook"
rm -rf "$HOME/Applications/Daybook.app" "$SUPPORT/app" "$SUPPORT/venv"
if [ "${1:-}" = "--delete-notes" ]; then
  rm -rf "$SUPPORT"
  echo "Daybook and its notes were removed from $(id -un). Your timesheet files are untouched."
else
  echo "Daybook was removed from $(id -un). Your notes are kept in: $SUPPORT"
  echo "Run again with --delete-notes to delete them too. Your timesheet files are untouched."
fi
