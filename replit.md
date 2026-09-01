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
- `README.md` — bot setup, commands, permissions, and Docker usage

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
- Rarity-first card shop with owner-editable prices and quantity purchases
- Limited/special edition templates and player cards that display an edition rather than a rarity
- Player challenges with collected squads, tactics, player instructions, substitutions, extra time, and penalties

## Gotchas

- Telegram inline buttons do not support arbitrary background colors; the UI uses colored status icons and football-themed labels.
- `OWNER_IDS` must contain numeric Telegram user IDs for `/admin`, player imports, templates, photo cards, and competition management.
- Built-in UCL, WC, and ACL mode catalogues seed five named teams each at startup. Those competition-only cards are excluded from `/claim`, `/debut`, `/player`, `/players`, and the shop.
- `/players` is a public browser in private chats and groups; `/player` searches all non-competition editions and rarities.
- `ICONIC` is the retired-player rarity and can use its own `/addtemplate` design.
- Owner grants moderator levels with `/addmod USER_ID 1` or `/addmod USER_ID 2`: level 1 handles player database work; level 2 also manages templates and competition teams.
- `/players` is a paginated level 1+ admin list available in private chat or groups, `/player NAME` is a fuzzy card search for all users, and `/botinfo` is an owner-only stats report.
- Create custom modes such as `playcwc` with `/addcompetition cwc | Club World Cup | 🌐 | CLUB`; users can then run `/playcwc` in a group.
- Add a team with `/addteam competition_key | team-key | Team name | Rating | Emoji | Player names`.
- Add positional card art with `/addtemplate ID | POSITION | RARITY | VERSION` by replying to an image.
- Add limited-edition card art with `/template ID | POSITION | EDITION | VERSION`; use `/editionplayer` for text cards with no rarity and `/tplayer Player Name | EDITION` for original-image cards.
- Open `/shop` to select a rarity, then a pack price and quantity. Owners can edit prices with `/shopprice RARITY | PRICE`.
- `/resetall` presents safe reset buttons; `/resetuser USER_ID CONFIRM` resets one user's game stats and collection without deleting their account identity.
- The local Python environment may be externally managed; Docker is the supported way to install and run the Python dependencies.

## Pointers

- See `README.md` for bot commands, bulk player imports, and competition management.
