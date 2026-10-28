# Daybook

A private work log for your Mac that fills your timesheet for you.

You type what you're doing in plain words ("started 10:15 wfh", "done: fixed the EOI bug PROJ-212", "logged off 19:40"). Daybook sorts each note into Done, In progress, Planned and Notes. Every morning at 04:07 it writes the previous day's row into your Excel timesheet.

No account, no server, no subscription. It runs on your Mac and keeps your notes there.

## Who it's for

- **People at startups and small teams.** Your day rarely matches the sprint plan. A founder asks for a quick report, a customer needs a hotfix, someone pulls you into a call. Daybook lets you log that work in one line as it happens, so it still shows up in your timesheet at the end of the month.
- **Engineers at service companies and agencies** who fill a daily Excel timesheet for each client or project.
- **Freelancers and contractors** who have to show what they did each day before they invoice.
- **Anyone who opens the timesheet on the 30th** and tries to remember what they did on the 4th.

## Logging ad-hoc work

Type notes the way you'd message a teammate. One note can hold several things, and Daybook splits them up.

| You type | Daybook records |
|---|---|
| `started 9:40 wfo` | Start time 09:40, mode WFO |
| `urgent ask from sales: pulled signup numbers for the investor deck` | Done: pulled signup numbers for the investor deck |
| `hotfix for checkout crash, deployed PROJ-318` | Done, with ticket `PROJ-318` kept on the line |
| `working on the pricing page revamp` | In progress |
| `will review the onboarding PR tomorrow` | Planned, carried forward until you mark it done or drop it |
| `blocked on API keys from the payments vendor` | Remark: blocked on API keys |
| `yesterday also fixed the CSV import bug` | Added to yesterday, and yesterday's row is refilled |
| `logged off 19:45` | End time 19:45; total hours worked out for you |

Ticket IDs like `ABC-123` and any URLs you paste are kept with the task they belong to.

The table shows sorting with Claude Code. Without it, the keyword rules handle simpler notes like `done: ...`, `working on ...` and `logged off 19:45`, and you can fix anything in the Timesheet row panel.

## Features

- **Plain-language logging** from the app window or from Terminal (`daybook add "..."`).
- **Automatic daily fill** into your existing Excel timesheet. Columns are matched by their headings, not by position.
- **Safe with your edits.** It never overwrites a cell you typed yourself, and saves a backup before every change.
- **Catches up** after sleep, shutdown or notes added to past days.
- **Plans carry forward.** Unfinished plans show up on later days until you mark them done or drop them.
- **Export** any date range to Excel or CSV.
- **Several Mac accounts** on one machine, each with its own notes, timesheet folder and Claude login.
- **Works offline** with keyword rules; uses Claude Code for smarter sorting when it's available.

## Requirements

- macOS
- Python 3 (Apple's Command Line Tools are enough; the installer asks for them if missing)
- Optional: [Claude Code](https://code.claude.com/docs), signed in, for smarter note sorting

The installer creates a private Python environment and installs [openpyxl](https://openpyxl.readthedocs.io/) in it. Nothing else is installed system-wide.

## Install (once in each Mac account)

1. Put this `Daybook` folder somewhere every account can reach, for example `/Users/Shared/Daybook`:

   ```
   git clone https://github.com/Ananth09/Daybook.git /Users/Shared/Daybook
   ```
2. Log in to the Mac account, open **Terminal** and run:

   ```
   bash /Users/Shared/Daybook/install.sh
   ```

3. In the same account, open Terminal and run `claude` once. Sign in with the Claude account this Mac account should use. Skip this if Claude Code is already signed in there.
4. Repeat steps 2 and 3 in your other Mac account.

Each Mac account gets its own Daybook:
- its own notes, which the other account can't open
- its own timesheet folder
- its own Claude login

Open Daybook from Spotlight (search "Daybook") or from `~/Applications`.

## Your timesheet file

- Put each month's timesheet (.xlsx) in `~/Daybook/Timesheets`. You can change this folder in **Settings**.
- Daybook finds the row whose Date matches the day and fills the columns by their headings: Task Description, WFO/WFH/Other, Start Time, End Time, Total Working Hours, Remarks / Comments, Additional Comments.
- **It never fills Approval (Y/N).**
- **It never overwrites a cell you typed in Excel yourself.**
- If no file has that date, it creates `Daybook_Timesheet_YYYY-MM.xlsx` in your layout, with a green header and Sundays in orange.
- If the file is open in Excel at fill time, Daybook waits and fills it after you close it.
- Before each change it saves a backup copy in `.daybook-backups` inside the folder.

Keep the timesheet folder **outside** Documents, Desktop and Downloads. macOS blocks background apps from those folders unless you allow it.

## When the fill runs

- At 04:07 every day you can change the time in Settings. It fills the previous day.
- **If the Mac was asleep or off,** it catches up as soon as you're back in that account. It looks back up to 8 days.
- **If you add notes to a past day,** that day's row is refilled about 10 minutes later.
- **Edits in the Timesheet row panel are kept.** Times, task text and remarks you type there are never replaced by the automatic fill.

## Without Claude Code

Daybook still works without Claude Code. Notes are sorted with simple keyword rules until Claude Code is installed and signed in. The pill at the top shows which one is in use.

## Log from Terminal

```
daybook add "done: shipped the scoring API PROJ-240"
```

This needs `~/.local/bin` on your PATH.

## Remove

```
bash /Users/Shared/Daybook/uninstall.sh                 # keeps your notes
bash /Users/Shared/Daybook/uninstall.sh --delete-notes  # deletes them too
```

Your timesheet files are never deleted.

## What's in this repo

| File | What it does |
|---|---|
| `daybook.py` | The app: local web server, note sorting, timesheet fill, and the `daybook` command |
| `ui.html` | The app window |
| `install.sh` | Sets up Daybook for the current Mac account |
| `uninstall.sh` | Removes it from the current Mac account |
| `Daybook.icns` | App icon |
| `LICENSE` | MIT license |

## Update

```
cd /Users/Shared/Daybook && git pull
bash install.sh
```

Running the installer again keeps your notes and settings.

## Where things are

| What | Where |
|---|---|
| Notes and settings | `~/Library/Application Support/Daybook` |
| Activity log | `~/Library/Application Support/Daybook/daybook.log` |
| App icon | `~/Applications/Daybook.app` |
| Background service | `~/Library/LaunchAgents/com.daybook.app.plist` |

Daybook runs only on your Mac, at `127.0.0.1`. The only thing it sends anywhere is your note text, through the `claude` command, so Claude can sort it. Use it for yourself; Claude Code's sign-in is meant for personal use, not for running an app for other people.

## Contributing

Issues and pull requests are welcome. Daybook is one Python file (`daybook.py`) using the standard library plus openpyxl, and one HTML file (`ui.html`) with no build step. To try changes without touching your real notes, point it at a scratch folder:

```
DAYBOOK_HOME=/tmp/daybook-dev python3 daybook.py serve
```

If Daybook is already installed in that Mac account, change `port` in `/tmp/daybook-dev/config.json` first so the two don't clash. You'll also need `pip install openpyxl` for the Excel parts.

## License

[MIT](LICENSE)
