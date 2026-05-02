import discord
from discord.ext import commands, tasks
from discord import app_commands
import motor.motor_asyncio
import os, asyncio
from datetime import datetime, timedelta
import bson
from flask import Flask
from threading import Thread

# ══════════════════════════════════════════
#              CẤU HÌNH / CONFIG
# ══════════════════════════════════════════
TOKEN    = os.environ.get("DISCORD_TOKEN_UN")
MONGO_URL = os.environ.get("MONGO_URL")

VOTE_DURATION_HOURS  = 12
PASS_THRESHOLD       = 0.75
MAX_PERMANENT_MEMBERS = 5

# ══════════════════════════════════════════
#           KẾT NỐI DATABASE / DATABASE
# ══════════════════════════════════════════
mongo_client = motor.motor_asyncio.AsyncIOMotorClient(
    MONGO_URL,
    serverSelectionTimeoutMS=5000,
    tlsInsecure=True
)
db            = mongo_client["WorldRP_2000"]
resolutions_col = db["UN_Resolutions"]
sanctions_col   = db["UN_Sanctions"]
permanent_col   = db["UN_PermanentMembers"]
awards_col      = db["UN_Awards"]
stats_col       = db["CountryStats"]

# ══════════════════════════════════════════
#           HELPER: TẠO EMBED ĐẸP
# ══════════════════════════════════════════
def make_embed(title_vi, title_en, color, desc_vi="", desc_en="", footer=""):
    desc = ""
    if desc_vi: desc += f"🇻🇳 {desc_vi}\n"
    if desc_en: desc += f"🇬🇧 {desc_en}"
    embed = discord.Embed(
        title=f"{title_vi}  ·  {title_en}",
        description=desc.strip() or None,
        color=color
    )
    if footer:
        embed.set_footer(text=footer)
    embed.timestamp = datetime.utcnow()
    return embed

def bilingual(vi, en):
    return f"🇻🇳 {vi}\n🇬🇧 {en}"

TYPE_LABELS = {
    "general":       "📋 Tổng quát  ·  General",
    "sanction":      "🚫 Cấm vận  ·  Sanction",
    "lift_sanction": "✅ Gỡ cấm vận  ·  Lift Sanction",
    "peace":         "☮️ Kêu gọi Hòa Bình  ·  Peace Call",
}

STATUS_LABELS = {
    "voting": "🗳️ Đang bỏ phiếu  ·  Voting",
    "passed": "✅ Đã thông qua  ·  Passed",
    "failed": "❌ Thất bại  ·  Failed",
    "vetoed": "🚫 Bị phủ quyết  ·  Vetoed",
}

# ══════════════════════════════════════════
#               KHỞI TẠO BOT
# ══════════════════════════════════════════
class UNBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        self.check_resolutions.start()
        self.apply_sanctions.start()
        print("✅ UN Bot sẵn sàng / Ready!")

    @tasks.loop(minutes=5)
    async def check_resolutions(self):
        now = datetime.utcnow()
        async for res in resolutions_col.find({"status": "voting", "deadline": {"$lt": now}}):
            await self.finalize_resolution(res)

    @tasks.loop(hours=1)
    async def apply_sanctions(self):
        async for sanction in sanctions_col.find({"active": True}):
            target_id = sanction["target_id"]
            penalty   = sanction.get("penalty_percent", 0.05)
            target    = await stats_col.find_one({"_id": target_id})
            if target:
                deduct = target.get("budget_mil", 0) * penalty
                await stats_col.update_one({"_id": target_id}, {"$inc": {"budget_mil": -deduct}})

    async def finalize_resolution(self, res):
        votes_yes = res.get("votes_yes", [])
        votes_no  = res.get("votes_no",  [])
        vetoed_by = res.get("vetoed_by", [])
        total     = len(votes_yes) + len(votes_no)
        res_id    = str(res["_id"])[-6:].upper()

        if vetoed_by:
            result = "vetoed"
            vetoers = ", ".join([f"<@{v}>" for v in vetoed_by])
            result_vi = f"Bị phủ quyết bởi: {vetoers}"
            result_en = f"Vetoed by: {vetoers}"
            color = discord.Color.dark_red()
        elif total == 0:
            result = "failed"
            result_vi = "Không có ai bỏ phiếu."
            result_en = "No votes were cast."
            color = discord.Color.red()
        else:
            ratio = len(votes_yes) / total
            if ratio >= PASS_THRESHOLD:
                result = "passed"
                result_vi = f"Thông qua với {len(votes_yes)}/{total} phiếu ({ratio*100:.1f}%)"
                result_en = f"Passed with {len(votes_yes)}/{total} votes ({ratio*100:.1f}%)"
                color = discord.Color.green()
                await self.execute_resolution(res)
            else:
                result = "failed"
                result_vi = f"Thất bại — {len(votes_yes)}/{total} phiếu ({ratio*100:.1f}%) — Cần 75%"
                result_en = f"Failed — {len(votes_yes)}/{total} votes ({ratio*100:.1f}%) — Need 75%"
                color = discord.Color.red()

        await resolutions_col.update_one(
            {"_id": res["_id"]},
            {"$set": {"status": result, "finalized_at": datetime.utcnow()}}
        )

        channel_id = res.get("channel_id")
        if channel_id:
            channel = self.get_channel(int(channel_id))
            if channel:
                embed = make_embed(
                    f"📋 KẾT QUẢ NGHỊ QUYẾT #{res_id}",
                    f"Resolution #{res_id} Result",
                    color
                )
                embed.add_field(name="📌 Tiêu đề  ·  Title", value=res["title"], inline=False)
                embed.add_field(
                    name="📊 Kết quả  ·  Result",
                    value=bilingual(result_vi, result_en),
                    inline=False
                )
                await channel.send(embed=embed)

    async def execute_resolution(self, res):
        res_type  = res.get("type")
        target_id = res.get("target_id")

        if res_type == "sanction" and target_id:
            existing = await sanctions_col.find_one({"target_id": target_id, "active": True})
            if not existing:
                await sanctions_col.insert_one({
                    "target_id":       target_id,
                    "active":          True,
                    "penalty_percent": 0.05,
                    "resolution_id":   str(res["_id"]),
                    "created_at":      datetime.utcnow()
                })
        elif res_type == "lift_sanction" and target_id:
            await sanctions_col.update_many(
                {"target_id": target_id, "active": True},
                {"$set": {"active": False}}
            )

bot = UNBot()

# ══════════════════════════════════════════
#                SYNC (Admin)
# ══════════════════════════════════════════
@bot.command()
@commands.has_permissions(administrator=True)
async def sync(ctx):
    fmt = await bot.tree.sync()
    await ctx.send(f"✅ Đã sync {len(fmt)} lệnh Slash!  ·  Synced {len(fmt)} slash commands!")

# ══════════════════════════════════════════
# 1. ĐỀ XUẤT NGHỊ QUYẾT / PROPOSE RESOLUTION
# ══════════════════════════════════════════
@bot.tree.command(name="de_xuat", description="🌐 Đề xuất nghị quyết lên LHQ / Propose a UN resolution")
@app_commands.describe(
    tieu_de="Tiêu đề / Title",
    noi_dung="Nội dung / Content",
    loai="Loại nghị quyết / Type",
    nuoc_lien_quan="Nước liên quan (nếu có) / Target country (if any)"
)
@app_commands.choices(loai=[
    app_commands.Choice(name="📋 Tổng quát / General",          value="general"),
    app_commands.Choice(name="🚫 Cấm vận / Sanction",           value="sanction"),
    app_commands.Choice(name="✅ Gỡ cấm vận / Lift Sanction",   value="lift_sanction"),
    app_commands.Choice(name="☮️ Kêu gọi Hòa Bình / Peace",    value="peace"),
])
async def de_xuat(
    interaction: discord.Interaction,
    tieu_de: str,
    noi_dung: str,
    loai: str,
    nuoc_lien_quan: discord.Member = None
):
    try:
        await interaction.response.defer()
    except discord.errors.NotFound:
        return

    proposer_id = str(interaction.user.id)
    proposer    = await stats_col.find_one({"_id": proposer_id})
    if not proposer:
        embed = make_embed("❌ Chưa đăng ký", "Not Registered", discord.Color.red(),
            "Bạn chưa đăng ký quốc gia! Dùng /dangky.",
            "You haven't registered a country! Use /dangky.")
        return await interaction.followup.send(embed=embed)

    target_id = str(nuoc_lien_quan.id) if nuoc_lien_quan else None
    if loai in ["sanction", "lift_sanction"] and not target_id:
        embed = make_embed("❌ Thiếu thông tin", "Missing Info", discord.Color.red(),
            "Loại nghị quyết này cần chỉ định nước liên quan!",
            "This resolution type requires a target country!")
        return await interaction.followup.send(embed=embed)

    deadline = datetime.utcnow() + timedelta(hours=VOTE_DURATION_HOURS)
    doc = {
        "title": tieu_de, "content": noi_dung, "type": loai,
        "proposer_id": proposer_id, "target_id": target_id,
        "votes_yes": [], "votes_no": [], "vetoed_by": [],
        "status": "voting", "deadline": deadline,
        "channel_id": str(interaction.channel_id),
        "created_at": datetime.utcnow()
    }
    result = await resolutions_col.insert_one(doc)
    res_id = str(result.inserted_id)

    embed = make_embed(
        f"🌐 NGHỊ QUYẾT MỚI #{res_id[-6:].upper()}",
        f"New Resolution #{res_id[-6:].upper()}",
        discord.Color.blue(),
        footer="Dùng /bophieu để tham gia bỏ phiếu  ·  Use /bophieu to vote"
    )
    embed.add_field(name="📌 Tiêu đề  ·  Title",       value=tieu_de,                              inline=False)
    embed.add_field(name="📝 Nội dung  ·  Content",     value=noi_dung[:1000],                      inline=False)
    embed.add_field(name="🗂️ Loại  ·  Type",            value=TYPE_LABELS.get(loai, loai),          inline=True)
    embed.add_field(name="🏳️ Đề xuất bởi  ·  Proposed by", value=interaction.user.mention,         inline=True)
    if nuoc_lien_quan:
        embed.add_field(name="🎯 Nước liên quan  ·  Target", value=nuoc_lien_quan.mention,          inline=True)
    embed.add_field(name="⏳ Deadline",                  value=f"<t:{int(deadline.timestamp())}:F> (<t:{int(deadline.timestamp())}:R>)", inline=False)
    embed.add_field(name="🆔 ID",                        value=f"`{res_id}`",                        inline=True)
    embed.add_field(name="🗳️ Bỏ phiếu  ·  Vote",        value=bilingual("Dùng `/bophieu`", "Use `/bophieu`"), inline=True)

    await interaction.followup.send(embed=embed)

# ══════════════════════════════════════════
# 2. BỎ PHIẾU / VOTE
# ══════════════════════════════════════════
@bot.tree.command(name="bophieu", description="🗳️ Bỏ phiếu cho nghị quyết / Vote on a resolution")
@app_commands.describe(
    resolution_id="ID nghị quyết / Resolution ID",
    phieu="Lựa chọn / Your choice"
)
@app_commands.choices(phieu=[
    app_commands.Choice(name="✅ Đồng ý / Yes",      value="yes"),
    app_commands.Choice(name="❌ Phản đối / No",      value="no"),
    app_commands.Choice(name="⬜棄권 / Abstain",     value="abstain"),
])
async def bophieu(interaction: discord.Interaction, resolution_id: str, phieu: str):
    try:
        await interaction.response.defer(ephemeral=True)
    except discord.errors.NotFound:
        return

    user_id = str(interaction.user.id)
    if not await stats_col.find_one({"_id": user_id}):
        embed = make_embed("❌ Chưa đăng ký", "Not Registered", discord.Color.red(),
            "Bạn chưa đăng ký quốc gia!", "You haven't registered a country!")
        return await interaction.followup.send(embed=embed)

    try:
        res = await resolutions_col.find_one({"_id": bson.ObjectId(resolution_id)})
    except:
        return await interaction.followup.send(embed=make_embed("❌ ID không hợp lệ", "Invalid ID", discord.Color.red()))

    if not res:
        return await interaction.followup.send(embed=make_embed("❌ Không tìm thấy", "Not Found", discord.Color.red(),
            "Không tìm thấy nghị quyết!", "Resolution not found!"))
    if res["status"] != "voting":
        return await interaction.followup.send(embed=make_embed("⚠️ Đã kết thúc", "Already Ended", discord.Color.orange(),
            "Nghị quyết này đã kết thúc!", "This resolution has ended!"))
    if res["deadline"] < datetime.utcnow():
        return await interaction.followup.send(embed=make_embed("⏰ Hết thời gian", "Time's Up", discord.Color.orange(),
            "Đã hết thời gian bỏ phiếu!", "Voting period has ended!"))

    await resolutions_col.update_one({"_id": res["_id"]}, {"$pull": {"votes_yes": user_id, "votes_no": user_id}})

    if phieu == "yes":
        await resolutions_col.update_one({"_id": res["_id"]}, {"$addToSet": {"votes_yes": user_id}})
        embed = make_embed("✅ Đã bỏ phiếu ĐỒNG Ý", "Voted YES", discord.Color.green(),
            f"Bạn đã bỏ phiếu **ĐỒNG Ý** cho nghị quyết `{resolution_id[-6:].upper()}`",
            f"You voted **YES** on resolution `{resolution_id[-6:].upper()}`")
    elif phieu == "no":
        await resolutions_col.update_one({"_id": res["_id"]}, {"$addToSet": {"votes_no": user_id}})
        embed = make_embed("❌ Đã bỏ phiếu PHẢN ĐỐI", "Voted NO", discord.Color.red(),
            f"Bạn đã bỏ phiếu **PHẢN ĐỐI** nghị quyết `{resolution_id[-6:].upper()}`",
            f"You voted **NO** on resolution `{resolution_id[-6:].upper()}`")
    else:
        embed = make_embed("⬜ Đã棄権", "Abstained", discord.Color.greyple(),
            f"Bạn đã **棄権** (không bỏ phiếu) nghị quyết `{resolution_id[-6:].upper()}`",
            f"You **abstained** on resolution `{resolution_id[-6:].upper()}`")

    await interaction.followup.send(embed=embed)

# ══════════════════════════════════════════
# 3. PHỦ QUYẾT / VETO
# ══════════════════════════════════════════
@bot.tree.command(name="vetoquyen", description="🚫 [Ủy viên thường trực] Phủ quyết nghị quyết / Veto a resolution")
@app_commands.describe(resolution_id="ID nghị quyết cần phủ quyết / Resolution ID to veto")
async def vetoquyen(interaction: discord.Interaction, resolution_id: str):
    try:
        await interaction.response.defer()
    except discord.errors.NotFound:
        return

    user_id = str(interaction.user.id)
    if not await permanent_col.find_one({"_id": user_id}):
        embed = make_embed("❌ Không có quyền", "No Permission", discord.Color.red(),
            "Bạn không phải Ủy viên thường trực Hội đồng Bảo an!",
            "You are not a Permanent Security Council Member!")
        return await interaction.followup.send(embed=embed)

    try:
        res = await resolutions_col.find_one({"_id": bson.ObjectId(resolution_id)})
    except:
        return await interaction.followup.send(embed=make_embed("❌ ID không hợp lệ", "Invalid ID", discord.Color.red()))

    if not res:
        return await interaction.followup.send(embed=make_embed("❌ Không tìm thấy", "Not Found", discord.Color.red()))
    if res["status"] != "voting":
        return await interaction.followup.send(embed=make_embed("⚠️ Đã kết thúc", "Already Ended", discord.Color.orange(),
            "Nghị quyết này đã kết thúc!", "This resolution has ended!"))

    await resolutions_col.update_one(
        {"_id": res["_id"]},
        {"$addToSet": {"vetoed_by": user_id}, "$set": {"status": "vetoed", "finalized_at": datetime.utcnow()}}
    )

    res_id = str(res["_id"])[-6:].upper()
    embed = make_embed(
        f"🚫 PHỦ QUYẾT NGHỊ QUYẾT #{res_id}",
        f"Resolution #{res_id} Vetoed",
        discord.Color.dark_red(),
        f"{interaction.user.mention} đã sử dụng quyền phủ quyết! Nghị quyết bị hủy bỏ.",
        f"{interaction.user.mention} used their veto power! Resolution is now void.",
        footer="Hội đồng Bảo an  ·  Security Council"
    )
    embed.add_field(name="📌 Tiêu đề  ·  Title", value=res["title"], inline=False)
    await interaction.followup.send(embed=embed)

# ══════════════════════════════════════════
# 4. XEM NGHỊ QUYẾT / VIEW RESOLUTION
# ══════════════════════════════════════════
@bot.tree.command(name="xem_nghiquyet", description="🔍 Xem chi tiết nghị quyết / View resolution details")
@app_commands.describe(resolution_id="ID nghị quyết / Resolution ID")
async def xem_nghiquyet(interaction: discord.Interaction, resolution_id: str):
    try:
        await interaction.response.defer()
    except discord.errors.NotFound:
        return

    try:
        res = await resolutions_col.find_one({"_id": bson.ObjectId(resolution_id)})
    except:
        return await interaction.followup.send(embed=make_embed("❌ ID không hợp lệ", "Invalid ID", discord.Color.red()))

    if not res:
        return await interaction.followup.send(embed=make_embed("❌ Không tìm thấy", "Not Found", discord.Color.red()))

    yes   = len(res.get("votes_yes", []))
    no    = len(res.get("votes_no",  []))
    total = yes + no
    ratio = (yes / total * 100) if total > 0 else 0
    res_id = str(res["_id"])[-6:].upper()

    status = res["status"]
    color_map = {"voting": discord.Color.blue(), "passed": discord.Color.green(),
                 "failed": discord.Color.red(), "vetoed": discord.Color.dark_red()}

    # Progress bar
    bar_len   = 20
    filled    = int(bar_len * (yes / total)) if total > 0 else 0
    bar       = "█" * filled + "░" * (bar_len - filled)
    threshold = int(bar_len * PASS_THRESHOLD)
    bar_list  = list(bar)
    if threshold < bar_len:
        bar_list[threshold] = "│"
    bar = "".join(bar_list)

    embed = make_embed(
        f"🌐 NGHỊ QUYẾT #{res_id}",
        f"Resolution #{res_id}",
        color_map.get(status, discord.Color.blue()),
        footer="LHQ WorldRP  ·  UN WorldRP"
    )
    embed.add_field(name="📌 Tiêu đề  ·  Title",       value=res["title"],                              inline=False)
    embed.add_field(name="📝 Nội dung  ·  Content",     value=res["content"][:500] + ("..." if len(res["content"]) > 500 else ""), inline=False)
    embed.add_field(name="🗂️ Loại  ·  Type",            value=TYPE_LABELS.get(res["type"], res["type"]), inline=True)
    embed.add_field(name="📊 Trạng thái  ·  Status",    value=STATUS_LABELS.get(status, status),         inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)
    embed.add_field(
        name="🗳️ Kết quả bỏ phiếu  ·  Vote Results",
        value=f"```\n✅ Đồng ý / Yes : {yes:>3}\n❌ Phản đối / No: {no:>3}\n📊 Tỷ lệ / Ratio: {ratio:.1f}%  (Cần/Need 75%)\n\n[{bar}]\n```",
        inline=False
    )
    if status == "voting":
        embed.add_field(name="⏳ Còn lại  ·  Remaining", value=f"<t:{int(res['deadline'].timestamp())}:R>", inline=True)
    if res.get("vetoed_by"):
        vetoers = ", ".join([f"<@{v}>" for v in res["vetoed_by"]])
        embed.add_field(name="🚫 Phủ quyết bởi  ·  Vetoed by", value=vetoers, inline=False)

    await interaction.followup.send(embed=embed)

# ══════════════════════════════════════════
# 5. DANH SÁCH ĐANG BỎ PHIẾU / ACTIVE LIST
# ══════════════════════════════════════════
@bot.tree.command(name="danhsach_nghiquyet", description="📋 Xem danh sách nghị quyết đang bỏ phiếu / Active resolutions")
async def danhsach_nghiquyet(interaction: discord.Interaction):
    try:
        await interaction.response.defer()
    except discord.errors.NotFound:
        return

    resolutions = await resolutions_col.find({"status": "voting"}).to_list(length=20)
    if not resolutions:
        embed = make_embed("🌐 Không có nghị quyết nào", "No Active Resolutions", discord.Color.greyple(),
            "Hiện tại không có nghị quyết nào đang bỏ phiếu.",
            "There are currently no active resolutions.")
        return await interaction.followup.send(embed=embed)

    embed = make_embed(
        "🌐 DANH SÁCH NGHỊ QUYẾT ĐANG BỎ PHIẾU",
        "Active Resolutions",
        discord.Color.blue(),
        footer=f"Tổng cộng / Total: {len(resolutions)} nghị quyết  ·  Dùng /xem_nghiquyet <ID> để xem chi tiết"
    )
    for res in resolutions:
        yes = len(res.get("votes_yes", []))
        no  = len(res.get("votes_no",  []))
        res_id = str(res["_id"])[-6:].upper()
        embed.add_field(
            name=f"#{res_id}  —  {res['title'][:50]}",
            value=(
                f"🗂️ {TYPE_LABELS.get(res['type'], res['type'])}\n"
                f"✅ {yes}  ❌ {no}  ·  ⏳ <t:{int(res['deadline'].timestamp())}:R>\n"
                f"🆔 `{str(res['_id'])}`"
            ),
            inline=False
        )
    await interaction.followup.send(embed=embed)

# ══════════════════════════════════════════
# 6. LỊCH SỬ NGHỊ QUYẾT / HISTORY
# ══════════════════════════════════════════
@bot.tree.command(name="lichsu_nghiquyet", description="📜 Xem lịch sử nghị quyết đã kết thúc / Resolution history")
async def lichsu_nghiquyet(interaction: discord.Interaction):
    try:
        await interaction.response.defer()
    except discord.errors.NotFound:
        return

    resolutions = await resolutions_col.find(
        {"status": {"$in": ["passed", "failed", "vetoed"]}}
    ).sort("finalized_at", -1).to_list(length=15)

    if not resolutions:
        embed = make_embed("📜 Chưa có lịch sử", "No History Yet", discord.Color.greyple(),
            "Chưa có nghị quyết nào kết thúc.", "No resolved resolutions yet.")
        return await interaction.followup.send(embed=embed)

    icons = {"passed": "✅", "failed": "❌", "vetoed": "🚫"}
    embed = make_embed(
        "📜 LỊCH SỬ NGHỊ QUYẾT",
        "Resolution History",
        discord.Color.greyple(),
        footer="15 nghị quyết gần nhất  ·  Last 15 resolutions"
    )
    for res in resolutions:
        icon   = icons.get(res["status"], "❓")
        res_id = str(res["_id"])[-6:].upper()
        fin_ts = int(res.get("finalized_at", datetime.utcnow()).timestamp())
        embed.add_field(
            name=f"{icon} #{res_id}  —  {res['title'][:45]}",
            value=f"{STATUS_LABELS.get(res['status'])}  ·  <t:{fin_ts}:D>",
            inline=False
        )
    await interaction.followup.send(embed=embed)

# ══════════════════════════════════════════
# 7. CẤM VẬN HIỆN HÀNH / ACTIVE SANCTIONS
# ══════════════════════════════════════════
@bot.tree.command(name="camvan_hienhanh", description="🚫 Xem danh sách cấm vận đang hiệu lực / Active sanctions")
async def camvan_hienhanh(interaction: discord.Interaction):
    try:
        await interaction.response.defer()
    except discord.errors.NotFound:
        return

    sanctions = await sanctions_col.find({"active": True}).to_list(length=20)
    if not sanctions:
        embed = make_embed("🌐 Không có cấm vận", "No Active Sanctions", discord.Color.green(),
            "Hiện không có quốc gia nào bị cấm vận.",
            "No countries are currently under sanctions.")
        return await interaction.followup.send(embed=embed)

    embed = make_embed(
        "🚫 CẤM VẬN ĐANG HIỆU LỰC",
        "Active Sanctions",
        discord.Color.orange(),
        footer=f"Tổng / Total: {len(sanctions)} cấm vận  ·  Trừ ngân sách mỗi giờ / Budget deducted hourly"
    )
    for s in sanctions:
        target = await stats_col.find_one({"_id": s["target_id"]})
        name   = target.get("name", f"<@{s['target_id']}>") if target else f"<@{s['target_id']}>"
        pct    = s.get("penalty_percent", 0.05) * 100
        embed.add_field(
            name=f"🚫 {name}",
            value=(
                f"💸 Phạt / Penalty: `-{pct:.0f}%` ngân sách/giờ  ·  budget/hour\n"
                f"📋 Nghị quyết / Resolution: `{s.get('resolution_id', 'N/A')[-6:].upper() if s.get('resolution_id') else 'N/A'}`\n"
                f"📅 Từ / Since: <t:{int(s['created_at'].timestamp())}:D>"
            ),
            inline=False
        )
    await interaction.followup.send(embed=embed)

# ══════════════════════════════════════════
# 8. QUẢN LÝ ỦY VIÊN THƯỜNG TRỰC / PERM MEMBERS
# ══════════════════════════════════════════
@bot.tree.command(name="them_uvthuongtrc", description="🪑 [ADMIN] Thêm ủy viên thường trực / Add permanent member")
@app_commands.describe(nuoc="Nước được thêm / Country to add")
async def them_uvthuongtrc(interaction: discord.Interaction, nuoc: discord.Member):
    try:
        await interaction.response.defer()
    except discord.errors.NotFound:
        return

    if not interaction.user.guild_permissions.administrator:
        return await interaction.followup.send(embed=make_embed("❌ Không có quyền", "No Permission", discord.Color.red(),
            "Chỉ Admin mới dùng được!", "Admin only!"))

    count = await permanent_col.count_documents({})
    if count >= MAX_PERMANENT_MEMBERS:
        return await interaction.followup.send(embed=make_embed("❌ Đã đủ số lượng", "Maximum Reached", discord.Color.red(),
            f"Đã đủ {MAX_PERMANENT_MEMBERS} ủy viên thường trực!",
            f"Maximum {MAX_PERMANENT_MEMBERS} permanent members reached!"))

    if await permanent_col.find_one({"_id": str(nuoc.id)}):
        return await interaction.followup.send(embed=make_embed("⚠️ Đã là ủy viên", "Already Member", discord.Color.orange(),
            "Nước này đã là ủy viên thường trực rồi!", "Already a permanent member!"))

    await permanent_col.insert_one({"_id": str(nuoc.id), "added_at": datetime.utcnow()})
    embed = make_embed(
        "🪑 THÊM ỦY VIÊN THƯỜNG TRỰC",
        "Permanent Member Added",
        discord.Color.gold(),
        f"{nuoc.mention} đã được thêm vào Hội đồng Bảo an với quyền phủ quyết!",
        f"{nuoc.mention} has been added to the Security Council with veto power!",
        footer=f"Thêm bởi / Added by: {interaction.user.name}"
    )
    embed.add_field(name="👤 Ủy viên  ·  Member", value=nuoc.mention, inline=True)
    embed.add_field(name="🪑 Vị trí  ·  Seat", value=f"{count + 1}/{MAX_PERMANENT_MEMBERS}", inline=True)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="xoa_uvthuongtrc", description="🗑️ [ADMIN] Xóa ủy viên thường trực / Remove permanent member")
@app_commands.describe(nuoc="Nước bị xóa / Country to remove")
async def xoa_uvthuongtrc(interaction: discord.Interaction, nuoc: discord.Member):
    try:
        await interaction.response.defer()
    except discord.errors.NotFound:
        return

    if not interaction.user.guild_permissions.administrator:
        return await interaction.followup.send(embed=make_embed("❌ Không có quyền", "No Permission", discord.Color.red()))

    result = await permanent_col.delete_one({"_id": str(nuoc.id)})
    if result.deleted_count == 0:
        return await interaction.followup.send(embed=make_embed("❌ Không tìm thấy", "Not Found", discord.Color.red(),
            "Nước này không phải ủy viên thường trực!", "Not a permanent member!"))

    embed = make_embed(
        "🗑️ XÓA ỦY VIÊN THƯỜNG TRỰC",
        "Permanent Member Removed",
        discord.Color.dark_grey(),
        f"{nuoc.mention} đã bị xóa khỏi Hội đồng Bảo an.",
        f"{nuoc.mention} has been removed from the Security Council.",
        footer=f"Xóa bởi / Removed by: {interaction.user.name}"
    )
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="danhsach_hdba", description="🪑 Xem Hội đồng Bảo an / View Security Council")
async def danhsach_hdba(interaction: discord.Interaction):
    try:
        await interaction.response.defer()
    except discord.errors.NotFound:
        return

    members = await permanent_col.find({}).to_list(length=10)
    if not members:
        embed = make_embed("🪑 Chưa có ủy viên", "No Members Yet", discord.Color.greyple(),
            "Chưa có ủy viên thường trực nào.", "No permanent members yet.")
        return await interaction.followup.send(embed=embed)

    embed = make_embed(
        "🪑 HỘI ĐỒNG BẢO AN",
        "Security Council",
        discord.Color.gold(),
        "Ủy viên thường trực có quyền phủ quyết (Veto) bất kỳ nghị quyết nào.",
        "Permanent members hold veto power over any resolution.",
        footer="LHQ WorldRP  ·  UN WorldRP"
    )
    embed.add_field(
        name=f"👥 Ủy viên thường trực  ·  Permanent Members ({len(members)}/{MAX_PERMANENT_MEMBERS})",
        value="\n".join([f"🪑 **#{i+1}** — <@{m['_id']}>" for i, m in enumerate(members)]),
        inline=False
    )
    await interaction.followup.send(embed=embed)

# ══════════════════════════════════════════
# 9. TRAO GIẢI THƯỞNG / AWARD
# ══════════════════════════════════════════
AWARD_CONFIGS = {
    "nobel_peace": {
        "name_vi": "🕊️ Nobel Hòa Bình",
        "name_en": "Nobel Peace Prize",
        "buff":    {"on_dinh": 20},
        "desc_vi": "+20 Ổn định",
        "desc_en": "+20 Stability",
        "color":   discord.Color.from_rgb(212, 175, 55)
    },
    "economic": {
        "name_vi": "📈 Giải Kinh tế Xuất sắc",
        "name_en": "Outstanding Economic Award",
        "buff":    {},   # dynamic
        "desc_vi": "+10% Ngân sách",
        "desc_en": "+10% Budget",
        "color":   discord.Color.green()
    },
    "nation_of_year": {
        "name_vi": "🌟 Quốc gia Tiêu biểu Năm",
        "name_en": "Nation of the Year",
        "buff":    {"on_dinh": 10, "budget_mil": 50},
        "desc_vi": "+10 Ổn định, +50M Ngân sách",
        "desc_en": "+10 Stability, +50M Budget",
        "color":   discord.Color.gold()
    }
}

@bot.tree.command(name="trao_giai", description="🏅 [ADMIN] Trao giải thưởng quốc tế / Award international prize")
@app_commands.describe(nuoc="Nước nhận giải / Recipient", loai_giai="Loại giải / Award type")
@app_commands.choices(loai_giai=[
    app_commands.Choice(name="🕊️ Nobel Hòa Bình / Nobel Peace Prize",          value="nobel_peace"),
    app_commands.Choice(name="📈 Giải Kinh tế Xuất sắc / Economic Award",       value="economic"),
    app_commands.Choice(name="🌟 Quốc gia Tiêu biểu Năm / Nation of the Year",  value="nation_of_year"),
])
async def trao_giai(interaction: discord.Interaction, nuoc: discord.Member, loai_giai: str):
    try:
        await interaction.response.defer()
    except discord.errors.NotFound:
        return

    if not interaction.user.guild_permissions.administrator:
        return await interaction.followup.send(embed=make_embed("❌ Không có quyền", "No Permission", discord.Color.red()))

    recipient_id   = str(nuoc.id)
    recipient_data = await stats_col.find_one({"_id": recipient_id})
    if not recipient_data:
        return await interaction.followup.send(embed=make_embed("❌ Chưa đăng ký", "Not Registered", discord.Color.red(),
            "Nước này chưa đăng ký!", "Country not registered!"))

    cfg  = AWARD_CONFIGS[loai_giai]
    buff = cfg["buff"].copy()
    if loai_giai == "economic":
        buff["budget_mil"] = recipient_data.get("budget_mil", 0) * 0.1

    await stats_col.update_one({"_id": recipient_id}, {"$inc": buff})
    await awards_col.insert_one({
        "recipient_id": recipient_id,
        "award":        loai_giai,
        "awarded_by":   str(interaction.user.id),
        "awarded_at":   datetime.utcnow()
    })

    embed = make_embed(
        f"🏅 TRAO GIẢI: {cfg['name_vi']}",
        f"Award: {cfg['name_en']}",
        cfg["color"],
        f"{nuoc.mention} đã được trao giải **{cfg['name_vi']}**!\n🎁 Phần thưởng: {cfg['desc_vi']}",
        f"{nuoc.mention} has been awarded **{cfg['name_en']}**!\n🎁 Reward: {cfg['desc_en']}",
        footer=f"Trao bởi / Awarded by: {interaction.user.name}"
    )
    embed.add_field(name="🏳️ Quốc gia  ·  Nation",    value=nuoc.mention,    inline=True)
    embed.add_field(name="🎖️ Giải  ·  Award",          value=cfg["name_vi"],  inline=True)
    await interaction.followup.send(embed=embed)

# ══════════════════════════════════════════
# 10. XEM GIẢI THƯỞNG / VIEW AWARDS
# ══════════════════════════════════════════
@bot.tree.command(name="giai_thuong", description="🏅 Xem giải thưởng của một nước / View a country's awards")
@app_commands.describe(nuoc="Nước cần xem / Country to check (leave empty for yourself)")
async def giai_thuong(interaction: discord.Interaction, nuoc: discord.Member = None):
    try:
        await interaction.response.defer()
    except discord.errors.NotFound:
        return

    target    = nuoc or interaction.user
    target_id = str(target.id)
    awards    = await awards_col.find({"recipient_id": target_id}).to_list(length=50)

    if not awards:
        embed = make_embed("🏅 Chưa có giải thưởng", "No Awards Yet", discord.Color.greyple(),
            f"{target.mention} chưa nhận được giải thưởng nào.",
            f"{target.mention} has not received any awards yet.")
        return await interaction.followup.send(embed=embed)

    embed = make_embed(
        f"🏅 GIẢI THƯỞNG: {target.display_name}",
        f"Awards: {target.display_name}",
        discord.Color.gold(),
        footer=f"Tổng / Total: {len(awards)} giải thưởng  ·  awards"
    )
    for a in awards:
        cfg = AWARD_CONFIGS.get(a["award"])
        if cfg:
            embed.add_field(
                name=f"{cfg['name_vi']}  ·  {cfg['name_en']}",
                value=f"📅 <t:{int(a['awarded_at'].timestamp())}:D>",
                inline=True
            )
    await interaction.followup.send(embed=embed)

# ══════════════════════════════════════════
#          KEEP-ALIVE SERVER (Render/HF)
# ══════════════════════════════════════════
flask_app = Flask("")

@flask_app.route("/")
def home():
    return "🌐 UN Bot đang chạy! / Running!", 200

@flask_app.route("/health")
def health():
    return {"status": "ok", "bot": "UN WorldRP"}, 200

def run_flask():
    flask_app.run(host="0.0.0.0", port=8080)

# ══════════════════════════════════════════
#                  CHẠY BOT
# ══════════════════════════════════════════
if __name__ == "__main__":
    Thread(target=run_flask, daemon=True).start()
    print("🌐 Flask keep-alive server started on port 8080!")
    bot.run(TOKEN)
