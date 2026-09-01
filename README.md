# Fʟ | Cᴀʀᴅs 🃏

An advanced Kurigram Telegram football manager and card game. The bot uses **Fʟ | Cᴀʀᴅs 🃏** as its user-facing name and includes:

- MongoDB-backed users, collections, squads, cooldowns, and player records
- A balanced `/debut` 4-3-3 starter squad
- `/claim` every 12 hours with retain, release, and view-card actions
- Native Kurigram colored inline buttons with separate Club, Arena, Help, Support, and Developer destinations
- `/collection`, `/squad`, and `/profile`
- Fuzzy `/player Name` card search with aliases, typo tolerance, and next/back pagination
- Quantity-based `/shop` with rarity-first navigation, live MongoDB prices, and ×1/×2/×3 pack purchases
- Challenge winners receive an additional 30–120 coin reward, while the existing participation reward remains
- Owner-created group competitions for `/playcl`, `/playwc`, `/playacl`, and custom commands such as `/playcwc`
- UCL uses fixed club squads, World Cup uses fixed national squads, and ACL uses fixed Asian club squads; these opponents are never taken from another user's collection
- Owner/admin player management through `/admin`, `/addplayer`, `/addplayers`, and paginated `/players` in private chat or groups
- Owner-only `/botinfo` statistics and level 1/2 moderator permissions through `/addmod`
- Seeded UCL, WC, and ACL mode catalogues with five named teams and competition-only rosters
- Owner-created competitions and teams through `/addcompetition`, `/addteam`, `/editteam`, and `/deleteteam`
- Manager team controls through `/team`, `/formation`, `/teamname`, `/subs`, and `/instruction`
- Live player challenges with tactics, mentality, player instructions, substitutions, commentator updates, extra time, and penalties
- Owner/admin positional templates plus `/template` limited-edition templates for POTW, POTY, TOTY, and UCL TOTY
- Limited-edition player cards without a rarity through `/editionplayer`; original-image editions through `/tplayer Player Name | EDITION`
- Scoped command menus: arena/challenge commands appear for groups, owner controls appear in the owner's private chat
- Protected reset controls: `/resetall CONFIRM` for the full database, or `/resetuser USER_ID CONFIRM` for one manager's stats and collection
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

Add your Telegram numeric ID to `OWNER_IDS` to unlock the owner controls. Owners can grant:

- Level 1 moderators: `/players`, `/addplayer`, `/addplayers`, `/templates`, and `/templateguide`
- Level 2 moderators: all level 1 tools plus `/addtemplate` and competition/team management

Use `/addmod USER_ID 1` or `/addmod USER_ID 2`. `/botinfo` is owner-only.

The owner command menu is private-chat only. `/arena`, `/playcl`, `/playucl`, `/playwc`, `/playacl`, custom `/play...` modes, and `/challenge` are group-only. `/players` is available to level 1+ owners/moderators in private chat or groups and is not treated as a `/play...` mode.

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

## Card shop

Open `/shop` in a private chat, select a pack rarity, then choose ×1, ×2, or ×3.
The Common Pack starts at **1,500 coins**. Owners can view or change saved prices with:

```text
/shopprice
/shopprice COMMON | 1500
```

Price changes are stored in MongoDB and reflected in the shop immediately. Duplicate cards from
a purchase are converted into coin credit rather than being added twice.

## Widescreen card templates

The card renderer preserves uploaded template aspect ratios. For a red/black stadium design, export a **16:9 or 1280 × 640 (2:1)** PNG and keep these safe zones:

- OVR `(54, 42)`, position `(54, 122)`, nation/club `(54, 176)`
- Portrait `(370, 88)` to `(930, 472)`
- Name center `(650, 506)`, identity center `(650, 550)`
- Stats at `(54, 590)`, `(214, 590)`, `(374, 590)`, `(760, 590)`, `(920, 590)`, `(1080, 590)`

Reply to the uploaded image with `/addtemplate gk-wide | GK | RARE | Widescreen 2:1`. Use the same canvas for CB, MID, and ATT, moving visual emphasis to DEF, PAS/DRI, and SHO/PAC respectively. `/templateguide` contains the full coordinate guide.

For non-rarity editions, reply to the image with:

```text
/template potw-st | ST | POTW | Limited 2:1
/template ucl-toty-cam | CAM | UCL TOTY | Limited 2:1
```

Add a limited-edition player without a rarity using `/editionplayer` and its 17-field format
shown by the command. The card displays its edition label instead of a rarity.

Recommended position template IDs are `GK`, `CB`, `LB`, `RB`, `LWB`, `RWB`, `CDM`, `CM`, `CAM`, `LM`, `RM`, `LW`, `RW`, `CF`, `ST`, and `SS`. `DEF`, `MID`, `ATT`, and `ALL` can be used as fallbacks. Uploaded 16:9 and 2:1 artwork keeps its original aspect ratio while using the same safe-zone coordinates.

## PvP challenge

Reply to another player’s Telegram message with `/challenge`. The opponent receives an accept/decline button. Once accepted, both collected squads enter manager setup. Managers choose formations, tactics, mentalities, and player instructions; during the match they can change tactics and make substitutions. The commentator posts expandable updates every 5–6 minutes, with extra time and penalties when needed.

## Product direction

The code is split into `dhandlers`, `database`, `services`, and `plugins` so the next layers—card template uploads, interactive PvP decisions, formations, and richer match commentary—can be added without putting all bot logic in one file.