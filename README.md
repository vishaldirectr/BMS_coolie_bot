# BMS_AGENT — Hyderabad Movie Ticket Monitor

BMS_AGENT is a local Telegram bot for browsing BookMyShow movies and showtimes,
marking movies as priorities, and receiving alerts when monitored availability
changes. It reuses the repository's BookMyShow listing/showtime integration and
stores its local preferences, snapshots, and event history in JSON files. It
does not require MongoDB.

The local process must stay running for Telegram interactions and background
monitoring. Windows sleep, shutdown, or closing the process pauses checks.

## Requirements

- Windows 10/11 and PowerShell
- Python 3.14+ managed with [uv](https://docs.astral.sh/uv/)
- A Telegram bot created through [@BotFather](https://t.me/BotFather)

The bot token is a password. If you have shared a token in a message, screenshot,
or repository, revoke it with BotFather and use a replacement token. Do not put
the token in source code or commit it.

## Start the bot locally

Open PowerShell in the project directory and run:

```powershell
winget install --id=astral-sh.uv -e
uv python install 3.14
uv sync --frozen
```

Set the replacement token without echoing it to the console, then start the
interactive bot:

```powershell
$secureToken = Read-Host "Paste the replacement BotFather token" -AsSecureString
$env:TELEGRAM_BOT_TOKEN = [Net.NetworkCredential]::new("", $secureToken).Password
$env:BMS_CITY = "hyderabad"
$env:BMS_POLL_INTERVAL_SECONDS = "300"
uv run main.py
```

Leave that PowerShell window open. In Telegram, open your bot and send `/start`.
The first catalogue refresh can take a few minutes while the bot checks the
listed movies. The bot saves your Telegram chat when you message it; no chat ID
is needed for normal interactive use. Press **Ctrl+C** in PowerShell to stop.
After stopping, clear the token from that shell with:

```powershell
Remove-Item Env:TELEGRAM_BOT_TOKEN
$secureToken = $null
```

The default monitor interval is five minutes. Set
`BMS_POLL_INTERVAL_SECONDS` to change it (minimum 60 seconds); BookMyShow may
throttle requests or change its undocumented listing/showtime responses, so a
shorter interval does not guarantee fresher data.

## Telegram controls

- `/start`: open the main menu (first asks for favourite theatres)
- `SHOW MOVIES`, `MOVIE LIST`, or `/movies`: refresh the catalogue
- `/refresh`: rescan the city listing
- `/favourites`: search and edit your favourite theatres
- `/priorities`: view, pause/resume, mute/unmute, refresh, or remove priority
  movies
- `/history`: view recorded events for your priority movies
- Browse a movie to view its saved showtimes, filter and sort results, refresh
  availability, and open the official BookMyShow page

On first use, the bot asks you to select one or more favourite theatres from the
venues found in BookMyShow showtime data. Search by sending `search <name or
location>`, toggle venues to add or remove them, then save. Your selection is
stored per Telegram user in `bms_bot_data.json` and persists across restarts.
Use `/favourites` any time to edit it.
If the bot reports that saving failed or the saved count differs from the
selection, keep the bot running and check the local interaction log described
below; reload the theatre list and try selecting the venues again.

The priority dashboard lists only priority movies with upcoming bookable shows
at your selected theatres. Selecting a movie drills down through its available
dates, selected theatres, showtimes, and ticket-category details. Priority
history, background change detection, and notifications are also restricted to
the current favourite selection; changing the selection immediately changes
the monitoring and delivery scope. There is no fallback to non-favourite
theatres. The current upstream integration fetches movie-level showtime data;
the application enforces the theatre filter in its backend snapshot, history,
and notification processing because the integration does not expose a
documented venue-specific API query.

Only movies with at least one bookable category appear in the catalogue. Movie
details and counts are based on the latest successful local scan. A show URL
opens BookMyShow; the bot does not book tickets.

## Local state and configuration

- `bms_bot_data.json` contains Telegram users, priorities, the latest snapshots,
  event history, delivery state, and catalogue. It is ignored by Git and should
  stay private. Back it up if you want to preserve settings and history.
- On first start, existing `bms_state.json` is used as an initial snapshot when
  available; the checker does not overwrite that file during bot monitoring.
- `BMS_DATA_FILE` optionally changes the bot JSON file path.
- `logs\bms_bot.log` records Telegram commands, callback/button actions,
  outgoing response text, and delivery failures. It is stored in the project
  `logs` folder, is ignored by Git, and rotates at 5 MB while keeping three
  backups. The log path is printed when the bot starts. Set `BMS_LOG_FILE` to
  choose another log path. Search the log for
  `favourite_save_failed`, `favourite_save_mismatch`, or `update_failed` when
  troubleshooting an action.
- `BMS_CITY` defaults to `hyderabad`; supported slugs include `chennai`,
  `mumbai`, `delhi-ncr`, `bengaluru`, `hyderabad`, `kolkata`, `pune`, and
  `kochi`.

The GitHub Actions workflow has no automatic schedule. Its manual
**workflow_dispatch** run remains available for a one-time check; configure
`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` as repository secrets for that mode.
It is not a replacement for the local interactive bot and will not keep
monitoring between runs.

## Data and monitoring limitations

The BookMyShow listing/showtime integration is not a documented public API and
may stop working if the site changes. The bot reports fetch errors and preserves
the last successful snapshot instead of treating failed data as a change.
Monitoring events include new shows/theatres/dates/categories, ticket status
and price changes, and listings that disappear. A disappeared listing is not
reported as a confirmed cancellation.

The current data source exposes category-level availability and prices, not
reliable individual seat maps. BMS_AGENT therefore does not report released,
held, sold, or blocked seats. It also displays location, screen, language, or
format information only when those fields are actually returned by the source.
Theatre identity uses the exact upstream venue code and name; similarly named
venues at different locations remain separate. If the source provides no
location, the bot displays “Theatre information unavailable.” Seat rows and
individual-seat browsing are not available without reliable seat-map data.
Please respect BookMyShow's terms, access controls, and rate limits.

Telegram reports callback-button clicks to the bot and those actions are
logged. Telegram does not notify the bot when a user taps a URL button such as
an official BookMyShow booking link, so those external link taps cannot be
captured by this application.
