from __future__ import annotations

from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
import re
from typing import Any
import unicodedata
from uuid import uuid4

from motor.motor_asyncio import AsyncIOMotorClient

from services.economy import CLAIM_RARITY_WEIGHTS, SHOP_PACKS, weighted_player_choice

from .seed import COMPETITIONS, COMPETITION_TEAMS, MODE_ROSTERS


class MongoDatabase:
    def __init__(self, uri: str, database_name: str) -> None:
        self.client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=5000)
        self.db = self.client[database_name]
        self.users = self.db.users
        self.players = self.db.players
        self.matches = self.db.matches
        self.competitions = self.db.competitions
        self.group_games = self.db.group_games
        self.shop_purchases = self.db.shop_purchases
        self.shop_config = self.db.shop_config

    async def connect(self) -> None:
        await self.client.admin.command("ping")
        await self.users.create_index("user_id", unique=True)
        await self.players.create_index("player_id", unique=True)
        await self.matches.create_index("created_at")
        await self.db.challenges.create_index("challenge_id", unique=True)
        await self.db.admins.create_index("user_id", unique=True)
        await self.db.templates.create_index("template_id", unique=True)
        await self.competitions.create_index("competition_key", unique=True)
        await self.group_games.create_index("chat_id")
        await self.shop_purchases.create_index("user_id")
        await self.shop_config.create_index("pack_key", unique=True)

    async def close(self) -> None:
        self.client.close()

    async def reset_all(self) -> dict[str, int]:
        collections = {
            "users": self.users,
            "players": self.players,
            "matches": self.matches,
            "competitions": self.competitions,
            "group_games": self.group_games,
            "challenges": self.db.challenges,
            "templates": self.db.templates,
            "moderators": self.db.admins,
            "shop_purchases": self.shop_purchases,
            "shop_config": self.shop_config,
        }
        counts: dict[str, int] = {}
        for name, collection in collections.items():
            result = await collection.delete_many({})
            counts[name] = result.deleted_count
        return counts

    async def seed_mode_catalog(self) -> None:
        for competition_key, (competition_emoji, competition_name, short_name) in COMPETITIONS.items():
            await self.add_competition(
                {
                    "competition_key": competition_key,
                    "name": competition_name,
                    "emoji": competition_emoji,
                    "short_name": short_name,
                    "team_type": "national" if competition_key == "playwc" else "club",
                    "seeded": True,
                }
            )
            for team_key, (team_emoji, team_name, rating) in list(COMPETITION_TEAMS[competition_key].items())[:5]:
                roster = MODE_ROSTERS.get(competition_key, {}).get(team_key, [])
                if not roster:
                    roster = [
                        (f"{team_name} Goalkeeper", "GK"),
                        *[(f"{team_name} Defender {index}", "DEF") for index in range(1, 4)],
                        *[(f"{team_name} Midfielder {index}", "MID") for index in range(1, 4)],
                        *[(f"{team_name} Attacker {index}", "ATT") for index in range(1, 5)],
                    ]
                player_ids = []
                for index, (player_name, position) in enumerate(roster, 1):
                    player_id = f"mode-{competition_key}-{team_key}-{index}"
                    player_ids.append(player_id)
                    position_bonus = {"GK": 1, "DEF": 2, "MID": 4, "ATT": 5}.get(position, 0)
                    player_ovr = max(60, min(99, rating + position_bonus - ((index + 1) % 3)))
                    await self.players.update_one(
                        {"player_id": player_id},
                        {
                            "$set": {
                                "player_id": player_id,
                                "name": player_name,
                                "nation": team_emoji if competition_key == "playwc" else "🌐",
                                "club": team_name,
                                "position": position,
                                "secondary_positions": [],
                                "rarity": "COMPETITION",
                                "ovr": player_ovr,
                                "pace": max(40, min(99, rating + (8 if position == "ATT" else 2) - index % 4)),
                                "shooting": max(25, min(99, rating + (10 if position == "ATT" else -18) - index % 3)),
                                "passing": max(35, min(99, rating + (7 if position == "MID" else 0) - index % 4)),
                                "dribbling": max(30, min(99, rating + (8 if position in {"MID", "ATT"} else -4) - index % 3)),
                                "defending": max(25, min(99, rating + (7 if position in {"GK", "DEF"} else -15) - index % 4)),
                                "physical": max(40, min(99, rating + 3 - index % 3)),
                                "preferred_foot": "Right",
                                "weak_foot": 3,
                                "skill_moves": 3,
                                "height": "",
                                "traits": ["Competition roster"],
                                "competition_only": True,
                                "claimable": False,
                                "starter_eligible": False,
                                "source": "built-in-mode-roster",
                                "updated_at": datetime.now(UTC),
                            },
                            "$setOnInsert": {"created_at": datetime.now(UTC)},
                        },
                        upsert=True,
                    )
                team = {
                    "team_key": team_key,
                    "name": team_name,
                    "rating": rating,
                    "emoji": team_emoji,
                    "player_ids": player_ids,
                    "seeded": True,
                }
                competition = await self.get_competition(competition_key)
                existing = next(
                    (item for item in (competition or {}).get("teams", []) if item.get("team_key") == team_key),
                    None,
                )
                if existing:
                    await self.update_competition_team(competition_key, team_key, team)
                else:
                    await self.add_competition_team(competition_key, team)

    async def is_healthy(self) -> bool:
        try:
            await self.client.admin.command("ping")
            return True
        except Exception:
            return False

    async def get_or_create_user(self, telegram_user: Any) -> dict[str, Any]:
        now = datetime.now(UTC)
        user = {
            "user_id": telegram_user.id,
            "coins": 5000,
            "glory": 0,
            "xp": 0,
            "collection": [],
            "squad": [],
            "formation": "4-3-3",
            "team_name": "Legacy United",
            "tactics": "Balanced",
            "mentality": "Balanced",
            "substitutes": [],
            "cooldowns": {},
            "pending_claim": None,
            "created_at": now,
        }
        await self.users.update_one(
            {"user_id": telegram_user.id},
            {
                "$set": {
                    "username": telegram_user.username,
                    "first_name": telegram_user.first_name or "Player",
                    "updated_at": now,
                },
                "$setOnInsert": user,
            },
            upsert=True,
        )
        return await self.users.find_one({"user_id": telegram_user.id}) or user

    async def get_user(self, user_id: int) -> dict[str, Any] | None:
        return await self.users.find_one({"user_id": user_id})

    async def add_debut_squad(self, user_id: int) -> list[dict[str, Any]]:
        wanted = ["GK", "DEF", "DEF", "DEF", "DEF", "MID", "MID", "MID", "ATT", "ATT", "ATT"]
        position_options = {
            "GK": ["GK"],
            "DEF": ["DEF", "CB", "LB", "RB", "LWB", "RWB"],
            "MID": ["MID", "CDM", "CM", "CAM", "LM", "RM"],
            "ATT": ["ATT", "LW", "RW", "CF", "SS", "ST"],
        }
        existing = await self.users.find_one({"user_id": user_id}) or {}
        owned = set(existing.get("collection", []))
        selected: list[dict[str, Any]] = []
        for position in wanted:
            candidates = await self.players.find(
                {
                    "position": {"$in": position_options[position]},
                    "competition_only": {"$ne": True},
                    "claimable": {"$ne": False},
                    "player_id": {"$nin": list(owned | {p["player_id"] for p in selected})},
                }
            ).to_list(length=2500)
            player = weighted_player_choice(candidates, CLAIM_RARITY_WEIGHTS)
            if player:
                selected.append(player)

        ids = [player["player_id"] for player in selected]
        await self.users.update_one(
            {"user_id": user_id},
            {
                "$addToSet": {"collection": {"$each": ids}},
                "$set": {"squad": ids, "formation": "4-3-3", "updated_at": datetime.now(UTC)},
            },
        )
        return selected

    async def claim_candidate(self, user_id: int) -> tuple[dict[str, Any] | None, datetime | None]:
        user = await self.users.find_one({"user_id": user_id}) or {}
        last_claim = user.get("cooldowns", {}).get("claim")
        now = datetime.now(UTC)
        if last_claim and now - last_claim < timedelta(hours=12):
            return None, last_claim + timedelta(hours=12)

        owned = user.get("collection", [])
        candidates = await self.players.find(
            {
                "competition_only": {"$ne": True},
                "claimable": {"$ne": False},
                "player_id": {"$nin": owned},
            }
        ).to_list(length=5000)
        if not candidates:
            candidates = await self.players.find(
                {"competition_only": {"$ne": True}, "claimable": {"$ne": False}}
            ).to_list(length=5000)
        player = weighted_player_choice(candidates, CLAIM_RARITY_WEIGHTS)
        if not player:
            return None, None

        await self.users.update_one(
            {"user_id": user_id},
            {
                "$set": {
                    "pending_claim": player["player_id"],
                    "cooldowns.claim": now,
                    "updated_at": now,
                }
            },
        )
        return player, None

    async def buy_pack(self, user_id: int, pack_key: str, quantity: int) -> dict[str, Any]:
        pack_key = pack_key.upper()
        packs = await self.get_shop_packs()
        pack = packs.get(pack_key)
        quantity = int(quantity)
        if not pack or quantity < 1 or quantity > 3:
            return {"ok": False, "reason": "That pack quantity is not available."}

        candidates = await self.players.find(
            {"competition_only": {"$ne": True}, "claimable": {"$ne": False}}
        ).to_list(length=5000)
        if not candidates:
            return {"ok": False, "reason": "No collectible cards are available in the shop yet."}

        total_cost = int(pack["price"]) * quantity
        debit = await self.users.update_one(
            {"user_id": user_id, "coins": {"$gte": total_cost}},
            {"$inc": {"coins": -total_cost}, "$set": {"updated_at": datetime.now(UTC)}},
        )
        if debit.modified_count != 1:
            user = await self.get_user(user_id) or {}
            return {
                "ok": False,
                "reason": f"You need <b>{total_cost:,}</b> coins, but only have <b>{int(user.get('coins', 0)):,}</b>.",
            }

        drawn = [
            weighted_player_choice(candidates, pack["drops"])
            for _ in range(quantity)
        ]
        cards = [player for player in drawn if player]
        user = await self.get_user(user_id) or {}
        collection = list(user.get("collection", []))
        new_ids: list[str] = []
        duplicates: list[dict[str, Any]] = []
        for player in cards:
            player_id = player["player_id"]
            if player_id in collection:
                duplicates.append(player)
            else:
                collection.append(player_id)
                new_ids.append(player_id)
        duplicate_credit = sum(max(50, int(player.get("ovr", 50)) * 5) for player in duplicates)
        await self.users.update_one(
            {"user_id": user_id},
            {
                "$set": {"collection": collection, "updated_at": datetime.now(UTC)},
                "$inc": {"coins": duplicate_credit},
            },
        )
        receipt_id = uuid4().hex[:12]
        await self.shop_purchases.insert_one(
            {
                "receipt_id": receipt_id,
                "user_id": user_id,
                "pack": pack_key,
                "quantity": quantity,
                "cost": total_cost,
                "cards": [player["player_id"] for player in cards],
                "duplicates": len(duplicates),
                "duplicate_credit": duplicate_credit,
                "created_at": datetime.now(UTC),
            }
        )
        updated_user = await self.get_user(user_id) or {}
        return {
            "ok": True,
            "pack": pack,
            "pack_key": pack_key,
            "quantity": quantity,
            "cost": total_cost,
            "cards": cards,
            "new_cards": len(new_ids),
            "duplicates": duplicates,
            "duplicate_credit": duplicate_credit,
            "balance": int(updated_user.get("coins", 0)),
            "receipt_id": receipt_id,
        }

    async def get_shop_packs(self) -> dict[str, dict[str, Any]]:
        packs = {
            key: {**pack, "drops": dict(pack["drops"])}
            for key, pack in SHOP_PACKS.items()
        }
        overrides = await self.shop_config.find({}).to_list(length=100)
        for override in overrides:
            pack = packs.get(str(override.get("pack_key", "")).upper())
            if pack:
                pack["price"] = max(1, int(override.get("price", pack["price"])))
        return packs

    async def set_shop_price(self, pack_key: str, price: int, updated_by: int) -> bool:
        pack_key = pack_key.upper()
        if pack_key not in SHOP_PACKS or price < 1:
            return False
        await self.shop_config.update_one(
            {"pack_key": pack_key},
            {
                "$set": {
                    "pack_key": pack_key,
                    "price": int(price),
                    "updated_by": updated_by,
                    "updated_at": datetime.now(UTC),
                }
            },
            upsert=True,
        )
        return True

    async def retain_pending(self, user_id: int) -> dict[str, Any] | None:
        user = await self.users.find_one({"user_id": user_id}) or {}
        player_id = user.get("pending_claim")
        if not player_id:
            return None
        collection = user.get("collection", [])
        squad = user.get("squad", [])
        if player_id not in collection:
            collection.append(player_id)
        if len(squad) < 25 and player_id not in squad:
            squad.append(player_id)
        await self.users.update_one(
            {"user_id": user_id},
            {"$set": {"collection": collection, "squad": squad, "pending_claim": None, "updated_at": datetime.now(UTC)}},
        )
        return await self.players.find_one({"player_id": player_id})

    async def release_pending(self, user_id: int) -> dict[str, Any] | None:
        user = await self.users.find_one({"user_id": user_id}) or {}
        player_id = user.get("pending_claim")
        if not player_id:
            return None
        player = await self.players.find_one({"player_id": player_id})
        await self.users.update_one(
            {"user_id": user_id},
            {"$set": {"pending_claim": None, "updated_at": datetime.now(UTC)}, "$inc": {"coins": int((player or {}).get("ovr", 50) * 10)}},
        )
        return player

    async def get_players(self, ids: list[str]) -> list[dict[str, Any]]:
        if not ids:
            return []
        found = await self.players.find({"player_id": {"$in": ids}}).to_list(length=len(ids))
        order = {player_id: index for index, player_id in enumerate(ids)}
        return sorted(found, key=lambda player: order.get(player["player_id"], 999))

    async def search_user_players(self, user_id: int, query: str) -> list[dict[str, Any]]:
        _, players = await self.get_user_players(user_id)
        needle = query.casefold()
        return [
            player
            for player in players
            if needle in player.get("name", "").casefold()
            or needle in player.get("club", "").casefold()
            or needle in player.get("position", "").casefold()
        ]

    async def search_players(self, query: str) -> list[dict[str, Any]]:
        aliases = {
            "cr7": "ronaldo",
            "cristiano": "ronaldo",
            "crition": "cristiano",
            "cristiano-ronaldo": "ronaldo",
            "lm10": "messi",
            "leo": "messi",
            "kdb": "de bruyne",
        }
        def normalize(value: str) -> str:
            decomposed = unicodedata.normalize("NFKD", value.casefold())
            return "".join(char for char in decomposed if not unicodedata.combining(char))

        terms = [aliases.get(term, term) for term in re.findall(r"[a-z0-9À-ÿ]+", normalize(query))]
        if not terms:
            return []
        players = await self.players.find(
            {"competition_only": {"$ne": True}}
        ).sort("name", 1).to_list(length=5000)
        matches: list[tuple[float, dict[str, Any]]] = []
        for player in players:
            searchable = normalize(" ".join(
                [
                    str(player.get("name", "")),
                    str(player.get("club", "")),
                    str(player.get("nation", "")),
                    str(player.get("position", "")),
                    " ".join(player.get("secondary_positions", [])),
                ]
            ))
            searchable_tokens = re.findall(r"[a-z0-9]+", searchable)
            scores: list[float] = []
            for term in terms:
                if term in searchable:
                    scores.append(1.0)
                    continue
                scores.append(max((SequenceMatcher(None, term, token).ratio() for token in searchable_tokens), default=0.0))
            if all(score >= 0.58 for score in scores):
                name = normalize(str(player.get("name", "")))
                exact_name_bonus = 0.25 if normalize(" ".join(terms)) in name else 0
                matches.append((sum(scores) / len(scores) + exact_name_bonus, player))
        matches.sort(key=lambda item: (-item[0], normalize(str(item[1].get("name", "")))))
        return [player for _, player in matches]

    async def list_players(self, skip: int = 0, limit: int = 20) -> list[dict[str, Any]]:
        return await self.players.find(
            {"competition_only": {"$ne": True}}
        ).sort("name", 1).skip(skip).limit(limit).to_list(length=limit)

    async def count_players(self) -> int:
        return await self.players.count_documents({"competition_only": {"$ne": True}})

    async def get_bot_stats(self) -> dict[str, int]:
        users = await self.users.find({}, {"collection": 1, "coins": 1, "xp": 1}).to_list(length=100000)
        competitions = await self.competitions.find({}, {"teams": 1}).to_list(length=100)
        return {
            "users": len(users),
            "users_with_squads": await self.users.count_documents({"squad.0": {"$exists": True}}),
            "players": await self.players.count_documents({}),
            "competition_players": await self.players.count_documents({"competition_only": True}),
            "collectible_players": await self.players.count_documents({"competition_only": {"$ne": True}}),
            "collected_cards": sum(len(user.get("collection", [])) for user in users),
            "coins": sum(int(user.get("coins", 0)) for user in users),
            "xp": sum(int(user.get("xp", 0)) for user in users),
            "competitions": len(competitions),
            "teams": sum(len(item.get("teams", [])) for item in competitions),
            "templates": await self.db.templates.count_documents({}),
            "moderators": await self.db.admins.count_documents({}),
            "matches": await self.matches.count_documents({}),
            "group_games": await self.group_games.count_documents({}),
            "challenges": await self.db.challenges.count_documents({}),
            "active_group_games": await self.group_games.count_documents({"status": {"$in": ["lobby", "setup", "live"]}}),
            "active_challenges": await self.db.challenges.count_documents({"status": {"$in": ["pending", "setup", "live"]}}),
        }

    async def get_mod(self, user_id: int) -> dict[str, Any] | None:
        return await self.db.admins.find_one({"user_id": user_id})

    async def get_user_players(self, user_id: int, squad_only: bool = False) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        user = await self.users.find_one({"user_id": user_id}) or {}
        ids = user.get("squad" if squad_only else "collection", [])
        return user, await self.get_players(ids)

    async def update_user(self, user_id: int, updates: dict[str, Any]) -> None:
        updates["updated_at"] = datetime.now(UTC)
        await self.users.update_one({"user_id": user_id}, {"$set": updates})

    async def add_player(self, player: dict[str, Any]) -> None:
        await self.players.update_one(
            {"player_id": player["player_id"]},
            {"$set": {**player, "updated_at": datetime.now(UTC)}, "$setOnInsert": {"created_at": datetime.now(UTC)}},
            upsert=True,
        )

    async def player_exists(self, name: str, club: str) -> bool:
        name_pattern = f"^{re.escape(name.strip())}$"
        club_pattern = f"^{re.escape(club.strip())}$"
        return bool(
            await self.players.find_one(
                {"name": {"$regex": name_pattern, "$options": "i"}, "club": {"$regex": club_pattern, "$options": "i"}},
                {"_id": 1},
            )
        )

    async def add_player_if_new(self, player: dict[str, Any]) -> bool:
        if await self.player_exists(player["name"], player["club"]):
            return False
        await self.add_player(player)
        return True

    async def add_mod(self, user_id: int, added_by: int, level: int = 1) -> None:
        await self.db.admins.update_one(
            {"user_id": user_id},
            {
                "$set": {
                    "user_id": user_id,
                    "added_by": added_by,
                    "level": max(1, min(2, level)),
                    "updated_at": datetime.now(UTC),
                }
            },
            upsert=True,
        )

    async def remove_mod(self, user_id: int) -> None:
        await self.db.admins.delete_one({"user_id": user_id})

    async def is_mod(self, user_id: int) -> bool:
        return bool(await self.db.admins.find_one({"user_id": user_id}))

    async def mod_level(self, user_id: int) -> int:
        moderator = await self.get_mod(user_id)
        return max(0, min(2, int((moderator or {}).get("level", 1)))) if moderator else 0

    async def save_template(self, template: dict[str, Any]) -> None:
        await self.db.templates.update_one(
            {"template_id": template["template_id"]},
            {"$set": {**template, "updated_at": datetime.now(UTC)}, "$setOnInsert": {"created_at": datetime.now(UTC)}},
            upsert=True,
        )

    async def get_template(
        self,
        rarity: str | None = None,
        position: str | None = None,
    ) -> dict[str, Any] | None:
        position = position.upper() if position else None
        rarity = rarity.upper() if rarity else None
        groups = {
            "GK": "GK",
            "CB": "DEF", "LB": "DEF", "RB": "DEF", "LWB": "DEF", "RWB": "DEF", "DEF": "DEF",
            "CDM": "MID", "CM": "MID", "CAM": "MID", "LM": "MID", "RM": "MID", "MID": "MID",
            "LW": "ATT", "RW": "ATT", "CF": "ATT", "ST": "ATT", "SS": "ATT", "ATT": "ATT",
        }
        positions = [item for item in (position, groups.get(position or "")) if item]
        if not positions:
            positions = ["ALL"]
        for candidate_position in positions + ["ALL"]:
            query: dict[str, Any] = {"position": candidate_position}
            if rarity:
                query["rarity"] = rarity
            template = await self.db.templates.find_one(query, sort=[("created_at", -1)])
            if template:
                return template
        if rarity:
            return await self.db.templates.find_one({"rarity": rarity}, sort=[("created_at", -1)])
        return await self.db.templates.find_one({}, sort=[("created_at", -1)])

    async def list_competitions(self) -> list[dict[str, Any]]:
        return await self.competitions.find({}).sort("created_at", 1).to_list(length=100)

    async def get_competition(self, competition_key: str) -> dict[str, Any] | None:
        return await self.competitions.find_one({"competition_key": competition_key})

    async def add_competition(self, competition: dict[str, Any]) -> bool:
        result = await self.competitions.update_one(
            {"competition_key": competition["competition_key"]},
            {
                "$setOnInsert": {
                    **competition,
                    "teams": [],
                    "created_at": datetime.now(UTC),
                }
            },
            upsert=True,
        )
        return result.upserted_id is not None

    async def add_competition_team(self, competition_key: str, team: dict[str, Any]) -> bool:
        result = await self.competitions.update_one(
            {
                "competition_key": competition_key,
                "teams.team_key": {"$ne": team["team_key"]},
            },
            {"$push": {"teams": team}},
        )
        return result.modified_count > 0

    async def update_competition_team(self, competition_key: str, team_key: str, updates: dict[str, Any]) -> bool:
        result = await self.competitions.update_one(
            {"competition_key": competition_key, "teams.team_key": team_key},
            {"$set": {f"teams.$.{key}": value for key, value in updates.items()}},
        )
        return result.modified_count > 0

    async def remove_competition_team(self, competition_key: str, team_key: str) -> bool:
        result = await self.competitions.update_one(
            {"competition_key": competition_key},
            {"$pull": {"teams": {"team_key": team_key}}},
        )
        return result.modified_count > 0

    async def get_team_players(self, team: dict[str, Any], team_type: str) -> list[dict[str, Any]]:
        if team.get("player_ids"):
            players = await self.get_players(team["player_ids"])
            if players:
                return players[:25]
        if team_type == "national":
            query = {"nation": team.get("emoji", ""), "name": {"$exists": True}}
        else:
            query = {"club": team["name"]}
        return await self.players.find(query).sort("ovr", -1).to_list(length=25)

    async def create_group_game(self, chat_id: int, mode: str, creator: Any) -> dict[str, Any]:
        game_id = uuid4().hex[:12]
        game = {
            "game_id": game_id,
            "chat_id": chat_id,
            "mode": mode,
            "status": "lobby",
            "host_id": creator.id,
            "host_name": creator.first_name or "Manager A",
            "opponent_id": None,
            "opponent_name": None,
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }
        await self.group_games.insert_one(game)
        return game

    async def get_active_group_game(self, chat_id: int) -> dict[str, Any] | None:
        return await self.group_games.find_one(
            {"chat_id": chat_id, "status": {"$in": ["lobby", "setup", "live"]}},
            sort=[("created_at", -1)],
        )

    async def get_group_game(self, game_id: str) -> dict[str, Any] | None:
        return await self.group_games.find_one({"game_id": game_id})

    async def update_group_game(self, game_id: str, updates: dict[str, Any]) -> None:
        updates["updated_at"] = datetime.now(UTC)
        await self.group_games.update_one({"game_id": game_id}, {"$set": updates})

    async def finish_group_game(self, game_id: str, result: dict[str, Any]) -> None:
        await self.update_group_game(game_id, {"status": "finished", "result": result, "finished_at": datetime.now(UTC)})

    async def update_challenge(self, challenge_id: str, updates: dict[str, Any]) -> None:
        updates["updated_at"] = datetime.now(UTC)
        await self.db.challenges.update_one({"challenge_id": challenge_id}, {"$set": updates})

    async def create_challenge(self, challenger: Any, challenged: Any, chat_id: int) -> str:
        challenge_id = uuid4().hex[:12]
        await self.db.challenges.insert_one(
            {
                "challenge_id": challenge_id,
                "challenger_id": challenger.id,
                "challenger_name": challenger.first_name or "Player 1",
                "challenged_id": challenged.id,
                "challenged_name": challenged.first_name or "Player 2",
                "chat_id": chat_id,
                "status": "pending",
                "created_at": datetime.now(UTC),
            }
        )
        return challenge_id

    async def get_challenge(self, challenge_id: str) -> dict[str, Any] | None:
        return await self.db.challenges.find_one({"challenge_id": challenge_id})

    async def get_active_challenge(self, user_id: int) -> dict[str, Any] | None:
        return await self.db.challenges.find_one(
            {
                "$or": [{"challenger_id": user_id}, {"challenged_id": user_id}],
                "status": {"$in": ["pending", "setup", "live"]},
            },
            sort=[("created_at", -1)],
        )

    async def finish_challenge(self, challenge_id: str, result: dict[str, Any]) -> None:
        await self.db.challenges.update_one(
            {"challenge_id": challenge_id},
            {"$set": {"status": "finished", "result": result, "finished_at": datetime.now(UTC)}},
        )
