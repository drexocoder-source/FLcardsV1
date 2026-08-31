# Fʟ | Cᴀʀᴅs 🃏

An advanced Kurigram Telegram football manager and card game. The bot uses **Fʟ | Cᴀʀᴅs 🃏** as its user-facing name and includes:

- MongoDB-backed users, collections, squads, cooldowns, and player records
- A balanced `/debut` 4-3-3 starter squad
- `/claim` every 12 hours with retain, release, and view-card actions
- Native Kurigram colored inline buttons with separate Club, Arena, Help, Support, and Developer destinations
- `/collection`, `/squad`, and `/profile`
- Owner-created group competitions for `/playcl`, `/playwc`, `/playacl`, and custom commands such as `/playcwc`
- UCL uses fixed club squads, World Cup uses fixed national squads, and ACL uses fixed Asian club squads; these opponents are never taken from another user's collection
- Owner/admin player management through `/admin`, `/addplayer`, `/addplayers`, and `/players` (seeding is disabled)
- Owner-created competitions and teams through `/addcompetition`, `/addteam`, `/editteam`, and `/deleteteam`
- Manager team controls through `/team`, `/formation`, `/teamname`, `/subs`, and `/instruction`
- Live player challenges with tactics, mentality, player instructions, substitutions, commentator updates, extra time, and penalties
- Owner-only positional templates and `/tplayer` original-image special edition cards
- Scoped command menus: arena/challenge commands appear for groups, owner controls appear in the owner's private chat
- A protected `/resetall CONFIRM` command for a deliberate full database reset
- A small health app in `app.py` running beside the Telegram client
- Dockerfile and Docker Compose with MongoDB persistence

## Required secrets

Set these in the environment:

- `BOT_TOKEN`
- `API_ID`
- `API_HASH`
- `MONGO_URI`

Optional:

- `MONGO_DB_NAME` (defaults to `fl_cards`)
- `OWNER_IDS` (comma-separated Telegram user IDs; the configured owner is included by default)
- `LOG_GROUP_ID` (defaults to the configured log group)
- `OPENROUTER_API_KEY` (the bot also falls back to the `OPENAI_API_KEY` secret)
- `OPENROUTER_MODEL` (defaults to `openai/gpt-4o-mini`)
- `PORT` (defaults to `8080`)

Add your Telegram numeric ID to `OWNER_IDS` to unlock the admin controls.

The owner command menu is private-chat only. `/arena`, `/playcl`, `/playwc`, `/playacl`, custom `/play...` modes, and `/challenge` are group-only. `/players` is a private owner/admin command and is not treated as a `/play...` mode.

## Run with Docker

```bash
cp .env.example .env
# Fill the four required values and OWNER_IDS in .env
docker compose up --build
```

The container runs `python bot.py`. That process starts both the Kurigram bot and the health app. Check `http://localhost:8080/health`.

## Add one or many players

```text
/addplayer Name | Nation | Club | Position | Secondary positions | Rarity | OVR | PAC | SHO | PAS | DRI | DEF | PHY | Foot | Weak foot | Skill moves | Height | Traits
```

Use one player per line to import many players in one message. The bot validates every row, shows live progress, adds valid new cards, skips an existing name + club pair, and reports invalid rows. `/addplayers` is also available when replying to a separate message containing the rows. Use `/templateguide` for the complete explanation.

## Competitions

Use `/arena` in a group to choose an owner-created competition. Each group has one lobby at a time. A manager selects pitch, weather, team, formation, and lineup before kick-off:

```text
/addcompetition key | Competition name | Emoji | CLUB or NATIONAL
/addteam competition_key | team-key | Team name | Rating | Emoji | Player names
```

## Widescreen card templates

The card renderer preserves uploaded template aspect ratios. For a red/black stadium design, export a **1280 × 640 (2:1)** PNG and keep these safe zones:

- OVR `(54, 42)`, position `(54, 122)`, nation/club `(54, 176)`
- Portrait `(370, 88)` to `(930, 472)`
- Name center `(650, 506)`, identity center `(650, 550)`
- Stats at `(54, 590)`, `(214, 590)`, `(374, 590)`, `(760, 590)`, `(920, 590)`, `(1080, 590)`

Reply to the uploaded image with `/addtemplate gk-wide | GK | RARE | Widescreen 2:1`. Use the same canvas for CB, MID, and ATT, moving visual emphasis to DEF, PAS/DRI, and SHO/PAC respectively. `/templateguide` contains the full coordinate guide.

## PvP challenge

Reply to another player’s Telegram message with `/challenge`. The opponent receives an accept/decline button. Once accepted, both collected squads enter manager setup. Managers choose formations, tactics, mentalities, and player instructions; during the match they can change tactics and make substitutions. The commentator posts expandable updates every 5–6 minutes, with extra time and penalties when needed.

## Product direction

The code is split into `dhandlers`, `database`, `services`, and `plugins` so the next layers—card template uploads, interactive PvP decisions, formations, and richer match commentary—can be added without putting all bot logic in one file.