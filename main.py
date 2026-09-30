import os
import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
import secrets
from datetime import datetime, timedelta, timezone
import aiosqlite
import aiohttp
import asyncio


# variables
CODE_TTL_MINUTES = 10
DB_PATH = "verify.db"
load_dotenv()
TOKEN = os.getenv('FLAREIFY_TOKEN')
intents = discord.Intents.default()
intents.message_content = True


async def find_account(session: aiohttp.ClientSession, username: str) -> dict | None:
    try:
        async with session.get(
            f"https://accounts.recflare.net/account/search?name={username}",
            params={"name": username}, # probably redundant idk
            timeout=aiohttp.ClientTimeout(total=10),
        ) as response:
            if response.status != 200:
                return None
            results = await response.json()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None
    for account in results:
        if account["username"].lower() == username.lower():
            return account

    return None

async def get_bio(session: aiohttp.ClientSession, account_id: int) -> str | None:
    try:
        async with session.get(
            f"https://accounts.recflare.net/account/{account_id}/bio",
            timeout=aiohttp.ClientTimeout(total=10)
        ) as response:
            if response.status != 200:
                return None
            data = await response.json()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None
    return data.get("bio", "")

async def get_level(session: aiohttp.ClientSession, account_id: int) -> int | None:
    try: 
        async with session.get(
            f"https://api.recflare.net/api/players/v2/progression/bulk",
            params={"id": account_id},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as response:
            if response.status != 200:
                return None
            data = await response.json()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None
    for player in data:
        if player["PlayerId"] == account_id:
            return player["Level"]

    return None

async def get_account(session: aiohttp.ClientSession, account_id: int) -> dict | None:
    try:
        async with session.get(
            f"https://accounts.recflare.net/account/{account_id}",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as response:
            if response.status != 200:
                return None
            return await response.json()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None
    
def account_age_days(created_at: str) -> int:
    created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - created).days

def generate_code() -> str:
    return "verify-" + secrets.token_hex(3)

async def save_pending(guild_id: int, discord_id: int, account_id: int, code: str):
    expires = (datetime.now(timezone.utc) + timedelta(minutes=CODE_TTL_MINUTES)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO pending_verifications "
            "(guild_id, discord_id, recflare_account_id, code, expires_at) VALUES (?,?,?,?,?)",
            (guild_id, discord_id, account_id, code, expires),
        )
        await db.commit()

async def get_pending(guild_id: int, discord_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM pending_verifications WHERE guild_id = ? AND discord_id = ?",
            (guild_id, discord_id),
        )
        return await cur.fetchone()

async def delete_pending(guild_id: int, discord_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM pending_verifications WHERE guild_id = ? AND discord_id = ?",
            (guild_id, discord_id),
        )
        await db.commit()

async def is_banned(guild_id: int, account_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT 1 FROM banned_accounts WHERE guild_id = ? AND recflare_account_id = ?",
            (guild_id, account_id),
        )
        return await cur.fetchone() is not None

async def save_guild_config(guild_id: int, role_id: int, min_age_days: int, min_level: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO guild_config (guild_id, role_id, min_age_days, min_level)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                role_id = excluded.role_id,
                min_age_days = excluded.min_age_days,
                min_level = excluded.min_level
            """,
            (guild_id, role_id, min_age_days, min_level),
        )
        await db.commit()

async def get_guild_config(guild_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM guild_config WHERE guild_id = ?", (guild_id,))
        return await cur.fetchone() 

async def is_linked_elsewhere(guild_id: int, account_id: int, discord_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT 1 FROM verified "
            "WHERE guild_id = ? AND recflare_account_id = ? AND discord_id != ?",
            (guild_id, account_id, discord_id),
        )
        return await cur.fetchone() is not None

async def mark_verified(guild_id: int, discord_id: int, account_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO verified "
            "(guild_id, discord_id, recflare_account_id, verified_at) VALUES (?,?,?,?)",
            (guild_id, discord_id, account_id, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()

async def save_panel_message(guild_id: int, channel_id: int, message_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE guild_config SET channel_id = ?, message_id = ? WHERE guild_id = ?",
            (channel_id, message_id, guild_id),
        )
        await db.commit()

async def get_panel_message(guild_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT channel_id, message_id FROM guild_config WHERE guild_id = ?",
            (guild_id,),
        )
        return await cur.fetchone()

def build_verification_embed(min_age_days: int, min_level: int) -> discord.Embed:
    embed = discord.Embed(
        title="RecFlare Verification",
        description="Link your RecFlare account to get access to this server.",
        color=discord.Color.orange(),
    )

    requirements = [f"Account age: at least {min_age_days} days"]
    if min_level > 1:
        requirements.append(f"Level: at least {min_level}")
    embed.add_field(name="Requirements", value="\n".join(requirements), inline=False)

    embed.add_field(
        name="Steps:",
        value=(
            "1. Press Verify and enter your RecFlare username.\n"
            "2. Put the code you are given in your in-game bio.\n"
            "3. Press Confirm."
        ),
        inline=False,
    )
    embed.set_footer(text="made by @cayrr.s")
    return embed


class VerifyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Verify", style=discord.ButtonStyle.primary, custom_id="verify:start")
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(VerifyModal())


# verify prompt

class VerifyModal(discord.ui.Modal, title="Flareify Verification"):
    username = discord.ui.TextInput(
        label="RecFlare Username",
        placeholder="Your @ name, not your display name.",
        max_length=50,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        async with aiohttp.ClientSession() as session:
            acc = await find_account(session, self.username.value.strip())

        if acc is None:
            return await interaction.followup.send(
                "No account found with that username.", ephemeral=True
            )

        if await is_banned(interaction.guild_id, acc["accountId"]):
            return await interaction.followup.send(
                "This account cannot be verified on this server.", ephemeral=True
            )

        code = generate_code()
        await save_pending(interaction.guild_id, interaction.user.id, acc["accountId"], code)

        await interaction.followup.send(
            f"Put this in your RecFlare bio:\n`{code}`\n\n"
            f"It expires after {CODE_TTL_MINUTES} minutes. After saving it in game, press Confirm.",
            view=ConfirmView(),
            ephemeral=True,
        )

# confirm
class ConfirmView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success, custom_id="verify:confirm")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.button):
        await interaction.response.defer(ephemeral=True)

        pending = await get_pending(interaction.guild_id, interaction.user.id)
        if pending is None or datetime.fromisoformat(pending["expires_at"]) < datetime.now(timezone.utc):
            return await interaction.followup.send("No active verification. Start again.", ephemeral=True)

        
        cfg = await get_guild_config(interaction.guild_id)
        if cfg is None:
            return await interaction.followup.send(
                "Verification is not set up on this server.", ephemeral=True)

        account_id = pending["recflare_account_id"]

        async with aiohttp.ClientSession() as session:
            acc, bio, level = await asyncio.gather(
                get_account(session, account_id),
                get_bio(session, account_id),
                get_level(session, account_id),
            )

        if acc is None or bio is None:
            return await interaction.followup.send(
                "Could not reach RecFlare. Try again in a moment.", ephemeral=True
            )
        if pending["code"].lower() not in bio.lower():
            return await interaction.followup.send(
                "Code not found in your bio yet. Save it in game, then press Confirm again.", ephemeral=True
            )
        
        if await is_banned(interaction.guild_id, account_id):
            return await interaction.followup.send(
                "This account cannot be verified on this server.", ephemeral=True)
        
        if await is_linked_elsewhere(interaction.guild_id, account_id, interaction.user.id):
            return await interaction.followup.send(
                "This RecFlare account is already linked to another Discord user.", ephemeral=True)
        age_days = account_age_days(acc["createdAt"])
        if age_days < cfg["min_age_days"]:
            return await interaction.followup.send(
                f"Your account is too new. It is {age_days} days old and "
                f"{cfg['min_age_days']} are required.", ephemeral=True
            )
        if cfg["min_level"] > 1:
            if level is None:
                return await interaction.followup.send(
                    "The level check is unavailable right now. Try again later.", ephemeral=True
                )
            if level < cfg["min_level"]:
                return await interaction.followup.send(
                    f"Your level is too low. You are level {level} and "
                    f"{cfg['min_level']} is required.", ephemeral=True
                )
        # finally give em the role
        role = interaction.guild.get_role(cfg["role_id"])
        if role is None:
            return await interaction.followup.send(
                "The verified role no longer exists. Ask an admin to run setup again.",
                ephemeral=True
            )
        try: 
            await interaction.user.add_roles(role, reason="Flareify Verification")
        except discord.Forbidden:
            return await interaction.followup.send(
                "I do not have permission to give that role. Ask an admin to move my role "
                "above the verified role.", ephemeral=True
            )
        await mark_verified(interaction.guild_id, interaction.user.id, account_id)
        await delete_pending(interaction.guild_id, interaction.user.id)
        await interaction.followup.send(
            "Verified. You can remove the code from your bio now.", ephemeral=True
        )

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS pending_verifications (
                guild_id INTEGER NOT NULL,
                discord_id INTEGER NOT NULL,
                recflare_account_id INTEGER NOT NULL,
                code TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, discord_id)
            );

            CREATE TABLE IF NOT EXISTS banned_accounts (
                guild_id INTEGER NOT NULL,
                recflare_account_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, recflare_account_id)
            );

            CREATE TABLE IF NOT EXISTS guild_config (
                guild_id INTEGER PRIMARY KEY,
                role_id INTEGER NOT NULL,
                min_age_days INTEGER NOT NULL,
                min_level INTEGER NOT NULL,
                channel_id INTEGER,
                message_id INTEGER
            );

            CREATE TABLE IF NOT EXISTS verified (
                guild_id INTEGER NOT NULL,
                discord_id INTEGER NOT NULL,
                recflare_account_id INTEGER NOT NULL,
                verified_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, discord_id)
            );
        """)
        await db.commit()

class Flareify(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix='!', intents=intents)
        

    async def setup_hook(self):
        await init_db()
        self.add_view(VerifyView())
        await self.tree.sync()

bot = Flareify()


@bot.tree.command(name="verify", description="Sends the Flareify verification message.")
@app_commands.describe(age="min account age", level="min level")
@app_commands.checks.has_permissions(administrator=True)
async def verify(
    interaction: discord.Interaction,
    level: int,
    age: int,
    role: discord.Role,
):
    existing = await get_panel_message(interaction.guild_id)

    if existing is not None:
        try:
            channel = bot.get_channel(existing["channel_id"])

            if channel is not None:
                message = await channel.fetch_message(existing["message_id"])

                return await interaction.response.send_message(
                    "there is already a verification message in this server.",
                    ephemeral=True,
                )

        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    await save_guild_config(
        interaction.guild_id,
        role.id,
        age,
        level,
    )

    msg = await interaction.channel.send(
        embed=build_verification_embed(age, level),
        view=VerifyView(),
    )

    await save_panel_message(
        interaction.guild_id,
        interaction.channel_id,
        msg.id,
    )

    await interaction.response.send_message(
        "verification message sent.",
        ephemeral=True,
    )
   



bot.run(TOKEN)    