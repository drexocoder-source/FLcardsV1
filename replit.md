# Football Legacy Manager

An advanced Telegram football card-collection and squad game powered by Kurigram and MongoDB.

## Telegram bot run & operate

- `python bot.py` — run the Telegram bot and health app
- `docker compose up --build` — run the Telegram bot and MongoDB together
- Health endpoint: `GET /health` on `PORT` (default `8080`)
- Required secrets: `BOT_TOKEN`, `API_ID`, `API_HASH`, `MONGO_URI`
- Optional environment: `MONGO_DB_NAME`, `OWNER_IDS`, `LOG_GROUP_ID`, `OPENROUTER_API_KEY`, `OPENROUTER_MODEL`, `PORT`
- The Replit workflow runs `PORT=8099 python bot.py`

## Stack

- Telegram bot: Python 3.12, Kurigram, Motor, MongoDB
- Health service: asyncio TCP server

## Where things live

- `bot.py` — starts Kurigram and the health app in one container process
- `app.py` — lightweight MongoDB-backed health server
- `dhandlers/` — Telegram command and callback handlers
- `database/` — MongoDB client, collections, and default player seed data
- `services/match.py` — match simulation and scorecards
- `plugins/` — extension point for future card and competition plugins
- `Dockerfile`, `docker-compose.yml` — container and local MongoDB orchestration
- `README.md` — bot setup, commands, and Docker usage

## Architecture decisions

- The player database is separate from collection ownership so one footballer can support multiple card editions later.
- A user has a collection plus an active squad capped at 25 players; `/debut` guarantees positional coverage.
- Claim cooldowns and pending retain/release actions live in MongoDB, so restarts do not reset game state.
- `bot.py` owns the process lifecycle and starts `app.py`'s health server alongside the Telegram client.

## Product

- Welcome hub with colored inline controls
- Balanced debut squad, timed random claims, player card display, retain/release actions
- Collection, active squad, and club profile views
- Group-only owner-created competitions with one active lobby per group
- Progressive condition, team, formation, lineup, and live manager flows
- Owner-managed player cards, positional templates, and original-image special-edition cards
- Player challenges with collected squads, tactics, player instructions, substitutions, extra time, and penalties

## Gotchas

- Telegram inline buttons do not support arbitrary background colors; the UI uses colored status icons and football-themed labels.
- `OWNER_IDS` must contain numeric Telegram user IDs for `/admin`, player imports, templates, photo cards, and competition management.
- Seeding is intentionally disabled: players, competitions, and teams must be added by owner commands.
- Create custom modes such as `playcwc` with `/addcompetition cwc | Club World Cup | 🌐 | CLUB`; users can then run `/playcwc` in a group.
- Add a team with `/addteam competition_key | team-key | Team name | Rating | Emoji | Player names`.
- Add positional card art with `/addtemplate ID | POSITION | RARITY | VERSION` by replying to an image.
- Add an original-image special card with `/tplayer Player Name` by replying to an image.
- The local Python environment may be externally managed; Docker is the supported way to install and run the Python dependencies.

## Pointers

- See `README.md` for bot commands, bulk player imports, and competition management.
