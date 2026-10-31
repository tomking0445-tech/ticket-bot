import asyncio
import ctypes
import json
import os
import sys
import discord
from discord import app_commands

if sys.platform == "win32":
    k = ctypes.windll.kernel32
    h = k.GetStdHandle(-10)
    m = ctypes.c_uint()
    k.GetConsoleMode(h, ctypes.byref(m))
    k.SetConsoleMode(h, (m.value & ~0x0040) | 0x0080)

CONFIG = {"ticket_category": "진행중", "staff_role": "대표", "client_role": "의뢰인", "done_role": "완료고객", "log_channel": "로그"}
if os.path.exists("config.json"):
    with open("config.json", encoding="utf-8") as f:
        CONFIG.update(json.load(f))
CONFIG.update({k: os.environ[k.upper()] for k in ("token", "guild_id", *CONFIG) if k.upper() in os.environ})

GUILD = discord.Object(id=int(CONFIG["guild_id"]))


def role(guild, key):
    return discord.utils.get(guild.roles, name=CONFIG[key])


def is_staff(member):
    return member.guild_permissions.administrator or role(member.guild, "staff_role") in member.roles


async def log(guild, text):
    ch = discord.utils.get(guild.text_channels, name=CONFIG["log_channel"])
    if ch:
        await ch.send(text)


async def swap(member, add=None, remove=None):
    try:
        if remove and remove in member.roles:
            await member.remove_roles(remove)
        if add:
            await member.add_roles(add)
    except discord.HTTPException:
        await log(member.guild, f"⚠️ {member.mention} 역할 변경 실패: 봇 역할을 의뢰인·완료고객보다 위로 올려주세요")


async def owner_of(channel):
    try:
        return await channel.guild.fetch_member(int(channel.topic))
    except (discord.NotFound, TypeError, ValueError):
        return None


class TicketModal(discord.ui.Modal, title="의뢰 문의"):
    kind = discord.ui.TextInput(label="종류", placeholder="게임 / 웹 / 앱 / 마크 플러그인 / 마크 모드 / 서버 구축 / 디코 봇", max_length=60)
    detail = discord.ui.TextInput(label="내용", style=discord.TextStyle.paragraph, placeholder="원하는 기능을 최대한 자세히 적어주세요", max_length=1500)
    env = discord.ui.TextInput(label="환경", placeholder="마크 서버 종류·버전, 앱 플랫폼 등", required=False, max_length=100)
    deadline = discord.ui.TextInput(label="희망 기한", required=False, max_length=50)
    budget = discord.ui.TextInput(label="예산", required=False, max_length=50)

    async def on_submit(self, interaction):
        guild = interaction.guild
        user = interaction.user
        existing = discord.utils.find(lambda c: c.topic == str(user.id), guild.text_channels)
        if existing:
            return await interaction.response.send_message(f"이미 진행 중인 문의가 있어요: {existing.mention}", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        staff = role(guild, "staff_role")
        allow = discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, read_message_history=True)
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: allow,
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
        }
        if staff:
            overwrites[staff] = allow
        channel = await guild.create_text_channel(
            f"의뢰-{user.display_name}",
            category=discord.utils.get(guild.categories, name=CONFIG["ticket_category"]),
            overwrites=overwrites,
            topic=str(user.id),
        )
        embed = discord.Embed(title="의뢰 문의", color=0x2ECC71)
        for name, field in (("종류", self.kind), ("내용", self.detail), ("환경", self.env), ("희망 기한", self.deadline), ("예산", self.budget)):
            embed.add_field(name=name, value=field.value or "-", inline=False)
        embed.set_footer(text="확인 후 견적 안내드릴게요.")
        await channel.send(content=f"{user.mention} {staff.mention if staff else ''}", embed=embed, view=TicketView())
        await swap(user, add=role(guild, "client_role"))
        await interaction.followup.send(f"문의 채널이 열렸어요: {channel.mention}", ephemeral=True)
        await log(guild, f"📥 문의 열림: {channel.mention} ({user.mention}, {self.kind.value})")


class PanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="문의하기", style=discord.ButtonStyle.success, custom_id="ticket:open")
    async def open(self, interaction, button):
        await interaction.response.send_modal(TicketModal())


class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="작업 완료", style=discord.ButtonStyle.primary, custom_id="ticket:done")
    async def done(self, interaction, button):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("대표만 누를 수 있어요.", ephemeral=True)
        member = await owner_of(interaction.channel)
        if member:
            await swap(member, add=role(interaction.guild, "done_role"), remove=role(interaction.guild, "client_role"))
        await interaction.response.send_message("작업이 완료됐어요. 이용해주셔서 감사합니다. #후기에 후기 남겨주시면 큰 힘이 됩니다.")
        await log(interaction.guild, f"✅ 작업 완료: {interaction.channel.mention}")

    @discord.ui.button(label="문의 닫기", style=discord.ButtonStyle.danger, custom_id="ticket:close")
    async def close(self, interaction, button):
        if not (is_staff(interaction.user) or interaction.channel.topic == str(interaction.user.id)):
            return await interaction.response.send_message("닫을 권한이 없어요.", ephemeral=True)
        await interaction.response.send_message("5초 후 채널이 닫혀요.")
        member = await owner_of(interaction.channel)
        if member:
            await swap(member, remove=role(interaction.guild, "client_role"))
        await log(interaction.guild, f"🔒 문의 닫힘: #{interaction.channel.name} (닫은 사람 {interaction.user.mention})")
        await asyncio.sleep(5)
        await interaction.channel.delete()


class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        self.add_view(PanelView())
        self.add_view(TicketView())
        await self.tree.sync(guild=GUILD)


client = Bot()


@client.tree.command(name="문의패널", description="현재 채널에 문의 버튼을 올려요", guild=GUILD)
@app_commands.default_permissions(administrator=True)
async def panel(interaction):
    embed = discord.Embed(
        title="의뢰 문의",
        description="아래 버튼을 눌러 의뢰서를 작성해주세요.\n작성하면 대표와 나만 보이는 전용 채널이 열려요.",
        color=0x2ECC71,
    )
    await interaction.channel.send(embed=embed, view=PanelView())
    await interaction.response.send_message("패널을 올렸어요.", ephemeral=True)


client.run(CONFIG["token"])
