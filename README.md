# Queue Boss Run — Discord Queue Bot

A lightweight Discord bot to manage sign‑up queues for raids, bosses, or any first‑come events. Users join/leave via buttons; admins can list, kick, reset. Data persists inside a hidden channel, so you don’t need a database.

## Features

- Button signup: interactive panel with Sign up / Cancel
- Multiple queues: separated by a `key` (e.g., `boss-a`)
- Persistent storage: saves JSON in a hidden `#queue-storage` channel
- Admin tools: list, kick, reset, and quick slash‑command sync
- I18N: English and Vietnamese, configurable per server
- No external DB or files needed on the server

## Requirements

- Python 3.10+
- A Discord application/bot with intents enabled:
  - Server Members Intent
  - Message Content Intent
- Bot scopes and permissions when inviting:
  - Scopes: `bot`, `applications.commands`
  - Permissions: View Channels, Send Messages, Read Message History, Embed Links, Manage Channels (to create `#queue-storage`)

## Setup

1) Clone and install dependencies

- Windows (PowerShell)
  - `py -3 -m venv .venv`
  - `.venv\Scripts\Activate`
  - `pip install -r requirements.txt`

- macOS/Linux
  - `python3 -m venv .venv`
  - `source .venv/bin/activate`
  - `pip install -r requirements.txt`

2) Configure environment

- Copy `.env.example` to `.env` and set your token:
  - `.env`:
    - `BOT_TOKEN=your-discord-bot-token`

3) Run the bot

- `python bot.py`

If slash commands don’t appear immediately, wait up to a few minutes or use `/queue_sync` in the server.

## How It Works

- On first use, the bot creates a private `#queue-storage` channel (visible only to the bot) and stores all queue data as compact JSON in bot-authored messages. No external database is required.
- Queues are identified by a free‑form `key` (e.g., `boss-a`). Each key has its own stored list of user IDs.

## Commands

- `/queue_create key:<text> title:<text>`: Create a signup panel message with buttons.
- `/queue_list key:<text>`: Show the queue (oldest → newest) as an ephemeral message.
- `/queue_kick key:<text> user:<member>`: Remove a member from a queue.
- `/queue_reset key:<text>`: Clear a queue.
- `/queue_setup_storage` (admin): Ensure the hidden storage channel exists.
- `/queue_sync` (admin): Force‑sync slash commands for the current server.
- `/language [lang]` (admin): Set or show the bot language (`en` or `vi`).

Notes

- Users join/leave by pressing the buttons on the panel created by `/queue_create`.
- Many responses are ephemeral (only visible to the command invoker).
- Do not manually modify or delete content in `#queue-storage`. If removed, data may be lost; the bot will recreate structures but cannot recover deleted queues.

## Project Structure

- `bot.py`: Main bot and commands
- `lang/en.json`, `lang/vi.json`: Localized strings
- `requirements.txt`: Python dependencies
- `.env.example`: Sample environment variables

## Troubleshooting

- Missing BOT_TOKEN: ensure `.env` has `BOT_TOKEN` or provide a `token` file.
- Slash commands missing: wait for global sync or run `/queue_sync` in the server.
- Intents errors: enable Server Members and Message Content intents in the Discord Developer Portal for your bot.

## License

No license specified. If you plan to share/distribute, add a license file.

