import os
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Missing DISCORD_BOT_TOKEN. Put it in .env at project root.")

import discord
from discord import app_commands, ui, Interaction
from discord.ext import commands
from datetime import datetime, timezone
from sqlalchemy import select, and_, func, desc
from .db import SessionLocal, init_models
from .models import User, Season, Session as GameSession, Entry, SessionStat, SeasonScore, Match
from .team_balance import split_4v4_min_diff
from typing import Optional

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)


ROOM_LABELS = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")


@bot.event
async def on_ready():
    await init_models()
    await bot.tree.sync()
    print(f"Logged in as {bot.user}")



async def ensure_user(db, member: discord.abc.User):
    uid = str(member.id)
    u = await db.scalar(select(User).where(User.discord_user_id == uid))
    if not u:
        u = User(discord_user_id=uid, display_name=member.display_name)
        db.add(u)
        await db.commit(); await db.refresh(u)
    return u


async def get_active_season(db):
    s = await db.scalar(select(Season).where(Season.is_active == True))
    return s


async def ensure_pending_session(db, season_id: int, week: int):
    s = await db.scalar(select(GameSession).where(
        and_(GameSession.season_id==season_id, GameSession.week_number==week, GameSession.room_label=="PENDING")
    ))
    if not s:
        s = GameSession(season_id=season_id, week_number=week, room_label="PENDING",
                        scheduled_at=datetime.now(timezone.utc), status="scheduled")
        db.add(s); await db.commit(); await db.refresh(s)
    return s


async def list_entries(db, session_id: int):
    q = select(Entry).where(and_(Entry.session_id==session_id, Entry.status=="confirmed")).order_by(Entry.id.asc())
    result = (await db.execute(q)).scalars().all()
    return result


async def init_session_stats(db, session_id: int, user_ids: list[int]):
    for uid in user_ids:
        exists = await db.scalar(select(SessionStat).where(and_(SessionStat.session_id==session_id, SessionStat.user_id==uid)))
        if not exists:
            db.add(SessionStat(session_id=session_id, user_id=uid, wins=0))
    await db.commit()


async def get_session_players_with_wins(db, session_id: int):
# entries→confirmedユーザーの wins を session_stats から取得
    ents = await list_entries(db, session_id)
    uids = [e.user_id for e in ents][:8] # 8人に制限
# 初期化
    await init_session_stats(db, session_id, uids)
    stats_map = { (s.user_id): s.wins for s in (await db.execute(
        select(SessionStat).where(SessionStat.session_id==session_id)
    )).scalars().all() }
    players = [ {"user_id":uid, "wins":stats_map.get(uid,0)} for uid in uids ]
    return players


# ========== コマンド ==========
@bot.tree.command(description="リーグに登録")
async def register(inter: Interaction):
    async with SessionLocal() as db:
        await ensure_user(db, inter.user)
    await inter.response.send_message("登録OK！", ephemeral=True)


@bot.tree.command(description="アクティブシーズンを作成（管理者）")
@commands.has_permissions(manage_guild=True)
async def create_season(inter: Interaction, name: str):
    async with SessionLocal() as db:
        now = datetime.now(timezone.utc)
        end = datetime.fromtimestamp(now.timestamp() + 60*60*24*90, tz=timezone.utc)
        existing_active = (await db.execute(select(Season).where(Season.is_active == True))).scalars().all()
        for season in existing_active:
            season.is_active = False
        s = Season(name=name, start_date=now, end_date=end, is_active=True)
        db.add(s)
        await db.commit()
    await inter.response.send_message(f"シーズン {name} を開始しました。", ephemeral=True)


@bot.tree.command(description="今週の参加告知を出す（管理者）")
@commands.has_permissions(manage_guild=True)
async def announce(inter: Interaction, week: int):
    async with SessionLocal() as db:
        season = await get_active_season(db)
        if not season:
            await inter.response.send_message("アクティブなシーズンがありません。/create_season で作成してください。", ephemeral=True)
            return
        await ensure_pending_session(db, season.id, week)
    await inter.channel.send(embed=discord.Embed(title=f"Week {week} 参加募集", description="下のボタンで参加/キャンセル。締切まで変更可。"), view=EntryView(week))
    await inter.response.send_message("告知を出しました。", ephemeral=True)

class EntryView(ui.View):
    def __init__(self, week:int):
        super().__init__(timeout=None)
        self.week = week


    @ui.button(label="参加", style=discord.ButtonStyle.success)
    async def join(self, inter: Interaction, button: ui.Button):
        async with SessionLocal() as db:
            user = await ensure_user(db, inter.user)
            season = await get_active_season(db)
            sess = await ensure_pending_session(db, season.id, self.week)
            # 既にエントリー済みか？
            existed = await db.scalar(select(Entry).where(and_(Entry.session_id==sess.id, Entry.user_id==user.id)))
            if not existed:
                db.add(Entry(session_id=sess.id, user_id=user.id, status="confirmed"))
            # 参加ポイントはMVPでは“押した時”に付与
                score = await db.scalar(select(SeasonScore).where(and_(SeasonScore.season_id==season.id, SeasonScore.user_id==user.id)))
                if not score:
                    score = SeasonScore(season_id=season.id, user_id=user.id, entry_points=0.0, win_points=0)
                    db.add(score)
                score.entry_points += 0.5
                await db.commit()
                await inter.response.send_message("参加を受け付けました（+0.5pt）", ephemeral=True)
            else:
                await inter.response.send_message("既に参加登録済みです。", ephemeral=True)


    @ui.button(label="キャンセル", style=discord.ButtonStyle.danger)
    async def cancel(self, inter: Interaction, button: ui.Button):
        async with SessionLocal() as db:
            user = await ensure_user(db, inter.user)
            season = await get_active_season(db)
            sess = await ensure_pending_session(db, season.id, self.week)
            ent = await db.scalar(select(Entry).where(and_(Entry.session_id==sess.id, Entry.user_id==user.id)))
            if ent:
                if ent.status == "confirmed":
                    ent.status = "canceled"
                    score = await db.scalar(select(SeasonScore).where(and_(SeasonScore.season_id==season.id, SeasonScore.user_id==user.id)))
                    if score:
                        score.entry_points -= 0.5
                    await db.commit()
                    await inter.response.send_message("キャンセルしました（-0.5pt）。", ephemeral=True)
                else:
                    await inter.response.send_message("既にキャンセル済みです。", ephemeral=True)
            else:
                await inter.response.send_message("参加登録が見つかりません。", ephemeral=True)

@bot.tree.command(description="締切：先着順に8人ずつ部屋確定（管理者）")
@commands.has_permissions(manage_guild=True)
async def close_entries(inter: Interaction, week: int):
    async with SessionLocal() as db:
        season = await get_active_season(db)
        pending = await ensure_pending_session(db, season.id, week)
        entries = await list_entries(db, pending.id)
        confirmed_ids = [e.user_id for e in entries if e.status == "confirmed"]
        if len(confirmed_ids) < 8:
            await inter.response.send_message("参加者が8人未満のため部屋確定できません。", ephemeral=True)
            return
        # 8人ずつ切って部屋を作る
        chunks = [confirmed_ids[i:i+8] for i in range(0, len(confirmed_ids), 8)]
        messages = []
        for idx, chunk in enumerate(chunks):
            if len(chunk) < 8:
                break
            room = ROOM_LABELS[idx]
            sess = GameSession(season_id=season.id, week_number=week, room_label=room,
                    scheduled_at=datetime.now(timezone.utc), status="scheduled")
            db.add(sess); await db.commit(); await db.refresh(sess)
            # この部屋の entries を作成
            for uid in chunk:
                db.add(Entry(session_id=sess.id, user_id=uid, status="confirmed"))
            await db.commit()
            # 当日勝数の初期化
            await init_session_stats(db, sess.id, chunk)
            # メンション文
            mentions = " ".join([f"<@{(await db.scalar(select(User).where(User.id==uid))).discord_user_id}>" for uid in chunk])
            messages.append(f"Week {week} 部屋 **{room}** を確定：{mentions}\nSession ID: `{sess.id}`")
        await inter.response.send_message("\n".join(messages), ephemeral=False)



@bot.tree.command(description="セッション開始（liveに変更）")
@commands.has_permissions(manage_guild=True)
async def start_session(inter: Interaction, session_id: int):
    async with SessionLocal() as db:
        sess = await db.get(GameSession, session_id)
        if not sess:
            await inter.response.send_message("セッションが見つかりません。", ephemeral=True)
            return
        sess.status = "live"
        await db.commit()
    await inter.response.send_message(f"Session {session_id} を開始しました。", ephemeral=False)

@bot.tree.command(description="次の試合のチームを発表（8人の勝数バランス）")
async def next_teams(inter: Interaction, session_id: int):
    async with SessionLocal() as db:
        sess = await db.get(GameSession, session_id)
        if not sess:
            await inter.response.send_message("セッションが見つかりません。", ephemeral=True)
            return
        players = await get_session_players_with_wins(db, session_id)
        if len(players) < 8:
            await inter.response.send_message("プレイヤーが8人揃っていません。", ephemeral=True)
            return
        teamA, teamB = split_4v4_min_diff(players)
        # match_index を決定
        last = await db.scalar(select(Match).where(Match.session_id==session_id).order_by(desc(Match.match_index)))
        next_idx = (last.match_index + 1) if last else 1
        m = Match(session_id=session_id, match_index=next_idx,
                team_a_ids=",".join(map(str, teamA)),
                team_b_ids=",".join(map(str, teamB)))
        db.add(m); await db.commit(); await db.refresh(m)
        # 表示
        async def mention(uid:int):
            u = await db.get(User, uid)
            return f"<@{u.discord_user_id}>"
        msg = (f"**Session {session_id} — Match #{next_idx}**\n"
                f"Team A: " + " ".join([await mention(u) for u in teamA]) + "\n"
                f"Team B: " + " ".join([await mention(u) for u in teamB]))
    await inter.response.send_message(msg, ephemeral=False)




@bot.tree.command(description="直近未確定の試合に勝敗を記録")
async def win(inter: Interaction, session_id: int, team: str, stage: str=""):
    team = team.upper()
    if team not in ("A","B"):
        await inter.response.send_message("team は A または B", ephemeral=True)
        return
    async with SessionLocal() as db:
    # winner未設定の最新マッチを取得
        m = await db.scalar(select(Match).where(and_(Match.session_id==session_id, Match.winner==None)).order_by(Match.match_index.asc()))
        if not m:
            await inter.response.send_message("勝敗未確定の試合が見つかりません。/next_teams で作成してください。", ephemeral=True)
            return
        m.winner = team
        m.stage = stage
    # 勝者側の wins を+1
        ids = list(map(int, (m.team_a_ids if team=="A" else m.team_b_ids).split(",")))
        for uid in ids:
            stat = await db.scalar(select(SessionStat).where(and_(SessionStat.session_id==session_id, SessionStat.user_id==uid)))
            if stat:
                stat.wins += 1
        await db.commit();
    # 10勝到達のチェック
        ten = await db.scalar(select(SessionStat).where(and_(SessionStat.session_id==session_id, SessionStat.wins>=10)))
        if ten:
            await inter.response.send_message(f"{inter.user.mention} 記録: Match #{m.match_index} → Team {team} 勝利。誰かが10勝に到達！ /finish を実行してください。", ephemeral=False)
        else:
            await inter.response.send_message(f"記録OK: Match #{m.match_index} → Team {team} 勝利", ephemeral=False)



@bot.tree.command(description="セッション終了（10勝条件達成）")
@commands.has_permissions(manage_guild=True)
async def finish(inter: Interaction, session_id: int):
    async with SessionLocal() as db:
        sess = await db.get(GameSession, session_id)
        if not sess:
            await inter.response.send_message("セッションが見つかりません。", ephemeral=True)
            return
        # 参加8人の wins をシーズンに加算
        stats = (await db.execute(select(SessionStat).where(SessionStat.session_id==session_id))).scalars().all()
        season = await get_active_season(db)
        for st in stats:
            sc = await db.scalar(select(SeasonScore).where(and_(SeasonScore.season_id==season.id, SeasonScore.user_id==st.user_id)))
            if not sc:
                sc = SeasonScore(season_id=season.id, user_id=st.user_id, entry_points=0.0, win_points=0)
                db.add(sc)
            sc.win_points += int(st.wins)
        sess.status = "finished"
        await db.commit()
    await inter.response.send_message(f"Session {session_id} を終了し、当日の勝数をシーズンに加算しました。", ephemeral=False)


@bot.tree.command(description="リーダーボードを表示")
async def leaderboard(inter: Interaction, season_name: Optional[str] = None):
    async with SessionLocal() as db:
        if season_name:
            season = await db.scalar(select(Season).where(Season.name==season_name))
        else:
            season = await get_active_season(db)
        if not season:
            await inter.response.send_message("シーズンが見つかりません。", ephemeral=True)
            return
        rows = (await db.execute(select(SeasonScore, User).join(User, User.id==SeasonScore.user_id)
                .where(SeasonScore.season_id==season.id)
                .order_by(desc(SeasonScore.entry_points + SeasonScore.win_points)))).all()
        if not rows:
            await inter.response.send_message("まだスコアがありません。", ephemeral=True)
            return
        lines = [f"**{season.name} Leaderboard**"]
        for i,(sc,u) in enumerate(rows, start=1):
            total = sc.entry_points + sc.win_points
            lines.append(f"{i}. {u.display_name} — {total:.1f}pt (参加{sc.entry_points:.1f} + 勝利{sc.win_points})")
        await inter.response.send_message("\n".join(lines), ephemeral=False)


if __name__ == "__main__":
    bot.run(TOKEN)