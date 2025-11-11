import os
from dotenv import load_dotenv

# トークンの読み込み
load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Missing DISCORD_BOT_TOKEN. Put it in .env at project root.")

import discord
from discord import app_commands, ui, Interaction
from discord.ext import commands
from datetime import datetime, timezone
from sqlalchemy import select, and_, func, desc, delete
from .db import SessionLocal, init_models
from .models import User, Season, Session as GameSession, Entry, SessionStat, SeasonScore, Match, SeasonParticipant
from .team_balance import split_4v4_min_diff
from typing import Optional

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)


ROOM_LABELS = list("123456789")


@bot.event
async def on_ready():
    await init_models()
    await bot.tree.sync()
    bot.add_view(RegisterView())
    print(f"Logged in as {bot.user}")


# Discord上のユーザーがDBにいない場合、自動的に登録
async def ensure_user(db, member: discord.abc.User):
    uid = str(member.id)
    u = await db.scalar(select(User).where(User.discord_user_id == uid))
    if not u:
        u = User(discord_user_id=uid, display_name=member.display_name)
        db.add(u)
        await db.commit(); await db.refresh(u)
    return u

# 現在アクティブなシーズンを取得
async def get_active_season(db):
    s = await db.scalar(select(Season).where(Season.is_active == True))
    return s

# 現在待ち状態(PENDING)のセッションを取得、なければ作成
async def ensure_pending_session(db, season_id: int, week: int):
    s = await db.scalar(select(GameSession).where(
        and_(GameSession.season_id==season_id, GameSession.week_number==week, GameSession.room_label=="PENDING")
    ))
    if not s:
        s = GameSession(season_id=season_id, week_number=week, room_label="PENDING",
                        scheduled_at=datetime.now(timezone.utc), status="scheduled")
        db.add(s); await db.commit(); await db.refresh(s)
    return s

# 指定された試合（session_id）に「参加が確定している（confirmed）」ユーザーのエントリーを取得
async def list_entries(db, session_id: int):
    q = select(Entry).where(and_(Entry.session_id==session_id, Entry.status=="confirmed")).order_by(Entry.id.asc())
    result = (await db.execute(q)).scalars().all()
    return result

# 指定された試合の参加者たちの勝利数カウント用の行を作る
async def init_session_stats(db, session_id: int, user_ids: list[int]):
    for uid in user_ids:
        exists = await db.scalar(select(SessionStat).where(and_(SessionStat.session_id==session_id, SessionStat.user_id==uid)))
        if not exists:
            db.add(SessionStat(session_id=session_id, user_id=uid, wins=0))
    await db.commit()


async def _start_session(db, session_id: int) -> str:
    sess = await db.get(GameSession, session_id)
    if not sess:
        return "セッションが見つかりません。"
    if sess.status == "finished":
        return f"Session {session_id} は終了済みのため開始できません。"
    if sess.status == "live":
        return f"Session {session_id} は既に live です。"
    sess.status = "live"
    await db.commit()
    return f"Session {session_id} を開始しました。"

# 部屋名に対応するテキスト&ボイスチャンネルを「るーとさんプラベ」カテゴリ内で確保し、テキストへ投稿
# 部屋名に対応するテキストチャンネル + チームA/Bのボイスチャンネルを作成して投稿
async def _post_to_room_channel(inter: Interaction, room_label: str, msg: str):
    guild = inter.guild
    base_name = f"room{room_label}"  # 例: room1

    # 1) カテゴリ取得 or 作成
    category = discord.utils.get(guild.categories, name="るーとさんプラベ")
    if not category:
        category = await guild.create_category("るーとさんプラベ")

    # 共有の権限（必要に応じて調整）
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        guild.me: discord.PermissionOverwrite(read_messages=True, connect=True, speak=True),
    }

    # 2) テキストチャンネル取得 or 作成
    text_ch = discord.utils.get(category.text_channels, name=base_name)
    if not text_ch:
        text_ch = await guild.create_text_channel(
            base_name,
            overwrites=overwrites,
            category=category
        )

    # 3) チームA・チームBのボイスチャンネルを取得 or 作成
    voice_names = [f"{base_name}-A", f"{base_name}-B"]

    for vname in voice_names:
        voice_ch = discord.utils.get(category.voice_channels, name=vname)
        if not voice_ch:
            await guild.create_voice_channel(
                vname,
                overwrites=overwrites,
                category=category,
                # オプション設定
                # user_limit=8,
                # bitrate=64000,
            )

    # 4) テキストチャンネルに投稿
    await text_ch.send(msg)

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

async def _create_next_match_and_message(db, session_id: int) -> str:
    sess = await db.get(GameSession, session_id)
    if not sess:
        return "セッションが見つかりません。"
    if sess.status == "finished":
        return f"Session {session_id} は既に終了済みです。"

    players = await get_session_players_with_wins(db, session_id)
    if len(players) < 8:
        return "プレイヤーが8人揃っていません。"

    # バランス編成（playersは {user_id, wins} の配列を想定）
    teamA, teamB = split_4v4_min_diff(players)

    # 次の match_index を決定
    last = await db.scalar(
        select(Match)
        .where(Match.session_id == session_id)
        .order_by(desc(Match.match_index))
    )
    next_idx = (last.match_index + 1) if last else 1

    # Match を作成
    m = Match(
        session_id=session_id,
        match_index=next_idx,
        team_a_ids=",".join(map(str, teamA)),
        team_b_ids=",".join(map(str, teamB)),
    )
    db.add(m)
    await db.commit()
    await db.refresh(m)

    # 表示用メンションを作成
    async def mention(uid: int) -> str:
        u = await db.get(User, uid)
        return f"<@{u.discord_user_id}>" if u else f"(uid:{uid})"

    msg = (
        f"**Session {session_id} — Match #{next_idx}**\n"
        f"Team A: " + " ".join([await mention(u) for u in teamA]) + "\n"
        f"Team B: " + " ".join([await mention(u) for u in teamB])
    )
    return msg

async def _apply_match_edit(db, match: Match, new_winner: str, new_stage: str) -> str:
    """match の勝者・ステージを new_* に更新し、SessionStat の wins を差分反映する。"""
    new_winner = new_winner.upper()
    if new_winner not in ("A", "B"):
        return "勝利チームは A または B を指定してください。"

    # 変更前の情報
    old_winner: Optional[str] = match.winner
    old_stage: str = match.stage or ""

    # チームメンバーをIDリスト化
    team_a_ids = list(map(int, match.team_a_ids.split(","))) if match.team_a_ids else []
    team_b_ids = list(map(int, match.team_b_ids.split(","))) if match.team_b_ids else []

    # ① 旧勝者側の wins をデクリメント
    if old_winner in ("A", "B"):
        old_ids = team_a_ids if old_winner == "A" else team_b_ids
        for uid in old_ids:
            stat = await db.scalar(select(SessionStat).where(
                and_(SessionStat.session_id == match.session_id,
                     SessionStat.user_id    == uid)
            ))
            if stat and stat.wins > 0:
                stat.wins -= 1

    # ② 新勝者側の wins をインクリメント
    new_ids = team_a_ids if new_winner == "A" else team_b_ids
    for uid in new_ids:
        stat = await db.scalar(select(SessionStat).where(
            and_(SessionStat.session_id == match.session_id,
                 SessionStat.user_id    == uid)
        ))
        if not stat:
            # 念のため存在しない場合は作成（通常は init_session_stats で作られている想定）
            stat = SessionStat(session_id=match.session_id, user_id=uid, wins=0)
            db.add(stat)
        stat.wins += 1

    # ③ 試合オブジェクトを更新
    match.winner = new_winner
    match.stage  = new_stage

    await db.commit()
    await db.refresh(match)

    return (f"Match #{match.match_index} を修正しました：\n"
            f"- 勝者: {old_winner or '未設定'} → **{new_winner}**\n"
            f"- ステージ: \"{old_stage}\" → \"{new_stage}\"")

async def _finish_session(db, session_id: int) -> str:
    sess = await db.get(GameSession, session_id)
    if not sess:
        return "セッションが見つかりません。"
    if sess.status == "finished":
        return f"Session {session_id} は既に終了済みです。"

    # 該当セッションの全ユーザーの wins を取得
    stats = (await db.execute(
        select(SessionStat).where(SessionStat.session_id == session_id)
    )).scalars().all()

    season = await get_active_season(db)
    if not season:
        return "アクティブなシーズンが見つかりません。"

    # シーズン累計へ加算
    for st in stats:
        sc = await db.scalar(select(SeasonScore).where(
            and_(SeasonScore.season_id == season.id, SeasonScore.user_id == st.user_id)
        ))
        if not sc:
            sc = SeasonScore(season_id=season.id, user_id=st.user_id,
                             entry_points=0.0, win_points=0)
            db.add(sc)
        sc.win_points += int(st.wins)

    # セッションを終了
    sess.status = "finished"
    await db.commit()
    return f"Session {session_id} を終了し、当日の勝数をシーズンに加算しました。"

# ---- 永続ビュー ----
class RegisterView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # 永続化

    @ui.button(label="登録", style=discord.ButtonStyle.primary, custom_id="register:primary")
    async def do_register(self, inter: Interaction, button: ui.Button):
        async with SessionLocal() as db:
            # ユーザー確保
            user = await ensure_user(db, inter.user)
            # アクティブシーズン取得
            season = await get_active_season(db)

            if season:
                # すでにシーズン参加者か？
                existed_participant = await db.scalar(
                    select(SeasonParticipant).where(
                        and_(SeasonParticipant.season_id == season.id,
                             SeasonParticipant.user_id   == user.id)
                    )
                )
                if existed_participant:
                    # 既に登録済み → モーダルは出さずに終了
                    await inter.response.send_message("すでに登録済みです。", ephemeral=True)
                    return

        # ここまで来たら未参加 or アクティブシーズンなし → XP入力モーダルを表示
        await inter.response.send_modal(XpModal())

class XpModal(ui.Modal, title="XPを入力"):
    def __init__(self):
        super().__init__(timeout=180)
        self.rate_input = ui.TextInput(
            label="XP",
            placeholder="例）2000",
            required=True,
            max_length=12
        )
        self.add_item(self.rate_input)

    async def on_submit(self, inter: Interaction):
        # 入力検証（floatに変換）
        try:
            init_rate = float(str(self.rate_input.value).strip())
        except ValueError:
            await inter.response.send_message("数値を入力してください。", ephemeral=True)
            return

        async with SessionLocal() as db:
            # ユーザー確保
            user = await ensure_user(db, inter.user)

            # 1) User.xp を更新
            user.xp = init_rate
            await db.commit()

            # 2) アクティブシーズンがあれば SeasonParticipant と SeasonScore を用意
            season = await get_active_season(db)
            if season:
                # 参加者登録（冪等）
                existed_participant = await db.scalar(
                    select(SeasonParticipant).where(
                        and_(SeasonParticipant.season_id == season.id,
                             SeasonParticipant.user_id   == user.id)
                    )
                )
                if not existed_participant:
                    db.add(SeasonParticipant(season_id=season.id, user_id=user.id))
                    await db.commit()

                # SeasonScore（そのシーズンのスコアレコード）を用意
                score = await db.scalar(
                    select(SeasonScore).where(
                        and_(SeasonScore.season_id == season.id,
                             SeasonScore.user_id   == user.id)
                    )
                )
                created_score = False
                if not score:
                    # まだなければ“初期値”として rate を設定
                    score = SeasonScore(
                        season_id=season.id, user_id=user.id,
                        entry_points=0.0, win_points=0, rate=init_rate
                    )
                    db.add(score)
                    created_score = True
                    await db.commit()

                # 3) ロール付与（「シーズンS1参加者」など）
                role_name = f"シーズン{season.name}参加者"
                guild = inter.guild
                role = discord.utils.get(guild.roles, name=role_name)
                member = inter.user if isinstance(inter.user, discord.Member) else guild.get_member(inter.user.id)

                # ロールが存在しない場合の案内
                if role is None:
                    await inter.response.send_message(
                        f"登録完了！XPを {init_rate} に設定しました。\n"
                        f"シーズン{season.name}の参加者として記録しました。\n"
                        f"ただしロール「{role_name}」が見つかりません。管理者に作成を依頼してください。",
                        ephemeral=True
                    )
                    return

                # Bot階層チェック
                bot_member = guild.me
                can_assign = role.position < bot_member.top_role.position

                if not can_assign:
                    await inter.response.send_message(
                        f"登録完了！XPを {init_rate} に設定しました。\n"
                        f"シーズン{season.name}の参加者として記録しました。\n"
                        f"権限の都合でロールを付与できませんでした。"
                        f"ご自身でロール「{role_name}」を付与してください。",
                        ephemeral=True
                    )
                    return

                # 付与実行
                try:
                    await member.add_roles(role, reason="League registration with initial rate")
                    if created_score:
                        msg_tail = f"SeasonScore.rate を {init_rate} で初期化し、ロール「{role_name}」を付与しました。"
                    else:
                        # 既にSeasonScoreがある場合は“初期値”のため上書きしない
                        msg_tail = f"既にシーズン{season.name}のスコアがあるため rate は変更していません。ロール「{role_name}」を付与しました。"
                    await inter.response.send_message(
                        f"登録完了！XPを {init_rate} に設定しました。\n{msg_tail}",
                        ephemeral=True
                    )
                except discord.Forbidden:
                    await inter.response.send_message(
                        f"登録完了！XPを {init_rate} に設定しました。\n"
                        f"ロール付与に失敗しました。権限がありません。ロール「{role_name}」を自身で付与してください。",
                        ephemeral=True
                    )
                except discord.HTTPException:
                    await inter.response.send_message(
                        f"登録完了！XPを {init_rate} に設定しました。\n"
                        f"ロール付与に失敗しました。後ほど再試行するか管理者にご連絡ください。",
                        ephemeral=True
                    )
            else:
                # アクティブシーズンがない場合は xp のみ更新
                await inter.response.send_message(
                    f"登録完了！XPを {init_rate} に設定しました。\n現在アクティブなシーズンはありません。",
                    ephemeral=True
                )

# ========== コマンド ==========
@bot.tree.command(description="リーグに登録（管理者）")
@commands.has_permissions(manage_guild=True)
async def register(inter: Interaction):
    # メッセージに「登録」ボタンを表示
    await inter.channel.send(
        embed=discord.Embed(title="リーグ登録", description="下のボタンから登録してください。"),
        view=RegisterView()
    )
    await inter.response.send_message(
        f"登録ボタンを表示しました。",
        ephemeral=True
    )

@bot.tree.command(description="アクティブシーズンを作成（管理者）")
@commands.has_permissions(manage_guild=True)
async def create_season(inter: Interaction, name: str):
    async with SessionLocal() as db:
        now = datetime.now(timezone.utc)
        end = datetime.fromtimestamp(now.timestamp() + 60 * 60 * 24 * 90, tz=timezone.utc)

        # 既存アクティブシーズンを無効化
        existing_active = (await db.execute(
            select(Season).where(Season.is_active == True)
        )).scalars().all()
        for season in existing_active:
            season.is_active = False

        # 新しいシーズンを作成
        s = Season(name=name, start_date=now, end_date=end, is_active=True)
        db.add(s)
        await db.commit()

    # ---- Discordロール作成 ----
    guild = inter.guild
    role_name = f"シーズン{name}参加者"

    # 既に同名のロールが存在するかチェック
    existing_role = discord.utils.get(guild.roles, name=role_name)
    if not existing_role:
        await guild.create_role(name=role_name)
        role_msg = f"ロール「{role_name}」を作成しました。"
    else:
        role_msg = f"ロール「{role_name}」は既に存在します。"

    await inter.response.send_message(
        f"シーズン {name} を開始しました。\n{role_msg}",
        ephemeral=True
    )


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
    def __init__(self, week: int):
        super().__init__(timeout=None)
        self.week = week

    @ui.button(label="参加", style=discord.ButtonStyle.success)
    async def join(self, inter: Interaction, button: ui.Button):
        async with SessionLocal() as db:
            user = await ensure_user(db, inter.user)
            season = await get_active_season(db)

            if not season:
                await inter.response.send_message(
                    "現在アクティブなシーズンがありません。管理者に確認してください。",
                    ephemeral=True,
                )
                return

            # シーズン参加者チェック
            is_participant = await db.scalar(
                select(SeasonParticipant).where(
                    and_(
                        SeasonParticipant.season_id == season.id,
                        SeasonParticipant.user_id == user.id,
                    )
                )
            )
            if not is_participant:
                await inter.response.send_message(
                    f"{inter.user.mention} さんはまだシーズン{season.name}の参加者ではありません。\n"
                    "ピン留めされたメッセージにある登録ボタンを押してください。",
                    ephemeral=True,
                )
                return

            # 参加処理
            sess = await ensure_pending_session(db, season.id, self.week)
            ent = await db.scalar(
                select(Entry).where(
                    and_(Entry.session_id == sess.id, Entry.user_id == user.id)
                )
            )

            if not ent:
                # 初回参加
                db.add(Entry(session_id=sess.id, user_id=user.id, status="confirmed"))

                score = await db.scalar(
                    select(SeasonScore).where(
                        and_(SeasonScore.season_id == season.id,
                             SeasonScore.user_id == user.id)
                    )
                )
                if not score:
                    score = SeasonScore(
                        season_id=season.id, user_id=user.id,
                        entry_points=0.0, win_points=0
                    )
                    db.add(score)
                score.entry_points += 0.5
                await db.commit()
                await inter.response.send_message("参加を受け付けました（+0.5pt）", ephemeral=True)

            else:
                # 既にエントリーあり → ステータスで分岐
                if ent.status == "canceled":
                    # 再参加：confirmed に戻して +0.5pt
                    ent.status = "confirmed"
                    score = await db.scalar(
                        select(SeasonScore).where(
                            and_(SeasonScore.season_id == season.id,
                                 SeasonScore.user_id == user.id)
                        )
                    )
                    if not score:
                        score = SeasonScore(
                            season_id=season.id, user_id=user.id,
                            entry_points=0.0, win_points=0
                        )
                        db.add(score)
                    score.entry_points += 0.5
                    await db.commit()
                    await inter.response.send_message("再参加を受け付けました（+0.5pt）", ephemeral=True)
                elif ent.status == "confirmed":
                    await inter.response.send_message("既に参加登録済みです。", ephemeral=True)
                else:
                    # 他ステータス（waitlist など）を念のため考慮
                    await inter.response.send_message(f"現在の状態: {ent.status}", ephemeral=True)
    
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

        chunks = [confirmed_ids[i:i+8] for i in range(0, len(confirmed_ids), 8)]
        summary_msgs = []

        for idx, chunk in enumerate(chunks):
            if len(chunk) < 8:
                break

            room = ROOM_LABELS[idx]

            # セッション作成
            sess = GameSession(
                season_id=season.id,
                week_number=week,
                room_label=room,
                scheduled_at=datetime.now(timezone.utc),
                status="scheduled",
            )
            db.add(sess)
            await db.commit(); await db.refresh(sess)

            # entries作成
            for uid in chunk:
                db.add(Entry(session_id=sess.id, user_id=uid, status="confirmed"))
            await db.commit()

            # 当日勝数初期化
            await init_session_stats(db, sess.id, chunk)

            # セッション開始
            start_msg = await _start_session(db, sess.id)

            # 第1試合チーム自動生成
            next_msg = await _create_next_match_and_message(db, sess.id)

            # メンション文作成
            mentions = " ".join([
                f"<@{(await db.scalar(select(User).where(User.id == uid))).discord_user_id}>"
                for uid in chunk
            ])

            # 投稿メッセージ構築
            msg = (
                f"**Week {week} 部屋 {room} — Session {sess.id}**\n"
                f"{start_msg}\n\n"
                f"参加者: {mentions}\n\n"
                f"{next_msg}"
            )

            # 各部屋チャンネルへ投稿
            await _post_to_room_channel(inter, room, msg)
            summary_msgs.append(f"部屋 {room} を開始し、チームを発表しました。")

        await inter.response.send_message("\n".join(summary_msgs), ephemeral=False)

@bot.tree.command(description="直近未確定の試合に勝敗を記録")
async def win(inter: Interaction, session_id: int, team: str, stage: str = ""):
    team = team.upper()
    if team not in ("A", "B"):
        await inter.response.send_message("team は A または B", ephemeral=True)
        return

    async with SessionLocal() as db:
        # 終了済みチェック
        sess = await db.get(GameSession, session_id)
        if not sess:
            await inter.response.send_message("セッションが見つかりません。", ephemeral=True)
            return
        if sess.status == "finished":
            await inter.response.send_message(
                f"Session {session_id} は既に終了済みです。", ephemeral=True
            )
            return

        room = sess.room_label  # ← 投稿先チャンネル名の決定に使う

        # winner未設定の最新マッチを取得
        m = await db.scalar(
            select(Match)
            .where(and_(Match.session_id == session_id, Match.winner == None))
            .order_by(Match.match_index.asc())
        )
        if not m:
            # 未確定が無ければ次試合を作って部屋チャンネルへ発表
            msg = await _create_next_match_and_message(db, session_id)
            await _post_to_room_channel(inter, room, msg)
            await inter.response.send_message("次試合を部屋チャンネルに投稿しました。", ephemeral=True)
            return

        # 勝敗を反映
        m.winner = team
        m.stage = stage

        # 勝者側の wins を+1
        ids = list(map(int, (m.team_a_ids if team == "A" else m.team_b_ids).split(",")))
        for uid in ids:
            stat = await db.scalar(
                select(SessionStat).where(
                    and_(SessionStat.session_id == session_id, SessionStat.user_id == uid)
                )
            )
            if stat:
                stat.wins += 1

        await db.commit()

        # 10勝到達のチェック
        ten = await db.scalar(
            select(SessionStat).where(
                and_(SessionStat.session_id == session_id, SessionStat.wins >= 10)
            )
        )

        if ten:
            # 自動終了（シーズン加算＋ステータス変更）
            finish_msg = await _finish_session(db, session_id)
            room_msg = (
                f"**記録OK**: Match #{m.match_index} → Team {team} 勝利\n"
                f"誰かが **10勝** に到達！\n{finish_msg}"
            )
            await _post_to_room_channel(inter, room, room_msg)
            await inter.response.send_message("結果を部屋チャンネルへ投稿し、セッションを終了しました。", ephemeral=True)
        else:
            # 次試合を自動生成・発表
            next_msg = await _create_next_match_and_message(db, session_id)
            room_msg = (
                f"**記録OK**: Match #{m.match_index} → Team {team} 勝利\n\n{next_msg}"
            )
            await _post_to_room_channel(inter, room, room_msg)
            await inter.response.send_message("結果と次試合を部屋チャンネルへ投稿しました。", ephemeral=True)

class UndoModal(ui.Modal, title="最新試合の結果を修正"):
    def __init__(self, session_id: int, match_id: int, room_label: str,
                 current_winner: Optional[str], current_stage: str):
        super().__init__(timeout=180)
        self.session_id = session_id
        self.match_id = match_id
        self.room_label = room_label

        self.winner_input = ui.TextInput(
            label="勝利チーム（A または B）",
            placeholder="A または B",
            default=current_winner or "",
            required=True,
            max_length=1,
        )
        self.stage_input = ui.TextInput(
            label="ステージ名",
            placeholder="例）Museum d'Alfonsino",
            default=current_stage or "",
            required=False,
            max_length=64,
        )
        self.add_item(self.winner_input)
        self.add_item(self.stage_input)

    async def on_submit(self, inter: Interaction):
        async with SessionLocal() as db:
            # 1) 対象試合の取得と結果修正（wins差分も反映）
            m = await db.get(Match, self.match_id)
            if not m:
                await inter.response.send_message("対象の試合が見つかりませんでした。", ephemeral=True)
                return

            msg_edit = await _apply_match_edit(db, m, self.winner_input.value, self.stage_input.value)

            # 2) 10勝到達チェック
            ten = await db.scalar(
                select(SessionStat).where(
                    and_(SessionStat.session_id == self.session_id, SessionStat.wins >= 10)
                )
            )

            if ten:
                # (a) 10勝 → 自動終了
                finish_msg = await _finish_session(db, self.session_id)
                room_msg = (
                    f"📢 **結果修正通知**\n"
                    f"Session {self.session_id} / Match #{m.match_index}\n"
                    f"勝者: {self.winner_input.value.upper()} / "
                    f"ステージ: {self.stage_input.value or '（未設定）'}\n"
                    f"(by {inter.user.mention})\n\n"
                    f"誰かが **10勝** に到達！\n{finish_msg}"
                )
                await _post_to_room_channel(inter, self.room_label, room_msg)
                await inter.response.send_message(
                    f"{msg_edit}\nセッションを終了しました（10勝到達）。",
                    ephemeral=True
                )
                return

            # (b) 未到達 → 未確定Matchを“最新の1件だけ”掃除してから次試合を生成
            pending = await db.scalar(
                select(Match)
                .where(and_(Match.session_id == self.session_id, Match.winner == None))
                .order_by(desc(Match.match_index))
            )
            if pending:
                await db.delete(pending)
                await db.commit()

            # 次試合のチーム編成とレコード生成
            next_msg = await _create_next_match_and_message(db, self.session_id)

            # 部屋チャンネルへ告知（この上は従来どおり）
            room_msg = (
                f"📢 **結果修正通知**\n"
                f"Session {self.session_id} / Match #{m.match_index}\n"
                f"勝者: {self.winner_input.value.upper()} / "
                f"ステージ: {self.stage_input.value or '（未設定）'}\n"
                f"(by {inter.user.mention})\n\n"
                f"{next_msg}"
            )
            await _post_to_room_channel(inter, self.room_label, room_msg)

            await inter.response.send_message(
                f"{msg_edit}\n次試合を部屋チャンネルへ投稿しました。",
                ephemeral=True
            )

@bot.tree.command(description="最新の試合結果を修正")
async def undo(inter: Interaction, session_id: int):
    async with SessionLocal() as db:
        # 最新試合を取得
        latest = await db.scalar(
            select(Match)
            .where(Match.session_id == session_id)
            .order_by(desc(Match.match_index))
        )
        if not latest:
            await inter.response.send_message("このセッションには試合がありません。", ephemeral=True)
            return

        # セッション情報を取得して room_label を取得
        sess = await db.get(GameSession, session_id)
        room_label = sess.room_label if sess else "?"

        # 現在の結果を表示
        info = (
            f"セッション {session_id} の最新試合は **#{latest.match_index}** です。\n"
            f"勝者: {latest.winner or '未設定'} / ステージ: {latest.stage or ''}\n\n"
            f"この内容を修正します。新しい値を入力してください。"
        )
        await inter.response.send_message(info, ephemeral=True)

        # モーダルを開く
        modal = UndoModal(
            session_id=session_id,
            match_id=latest.id,
            room_label=room_label,
            current_winner=latest.winner,
            current_stage=latest.stage or "",
        )
        await inter.followup.send_modal(modal)

# -------------------------
# 任意の試合番号の結果を修正：/modify
# -------------------------

class ModifyModal(ui.Modal, title="指定試合の結果を修正"):
    def __init__(self, session_id: int, match_id: int, match_index: int,
                 current_winner: Optional[str], current_stage: str):
        super().__init__(timeout=180)
        self.session_id = session_id
        self.match_id = match_id
        self.match_index = match_index

        self.winner_input = ui.TextInput(
            label="勝利チーム（A または B）",
            placeholder="A または B",
            default=current_winner or "",
            required=True,
            max_length=1
        )
        self.stage_input = ui.TextInput(
            label="ステージ名",
            placeholder="例）Museum d'Alfonsino",
            default=current_stage or "",
            required=False,
            max_length=64
        )
        self.add_item(self.winner_input)
        self.add_item(self.stage_input)

    async def on_submit(self, inter: Interaction):
        async with SessionLocal() as db:
            m = await db.get(Match, self.match_id)
            if not m:
                await inter.response.send_message("対象の試合が見つかりませんでした。", ephemeral=True)
                return
            msg = await _apply_match_edit(db, m, self.winner_input.value, self.stage_input.value)
            await inter.response.send_message(
                f"セッション {self.session_id} / Match #{self.match_index}\n{msg}",
                ephemeral=True
            )


@bot.tree.command(description="指定した試合番号の結果を修正（管理者）")
@commands.has_permissions(manage_guild=True)
async def modify(inter: Interaction, session_id: int, match_index: int):
    async with SessionLocal() as db:
        m = await db.scalar(
            select(Match)
            .where(and_(Match.session_id == session_id, Match.match_index == match_index))
        )
        if not m:
            await inter.response.send_message("指定の試合が見つかりません。", ephemeral=True)
            return

        # 現状を表示
        info = (f"セッション {session_id} / 試合 **#{match_index}** の現在の結果:\n"
                f"勝者: {m.winner or '未設定'} / ステージ: {m.stage or ''}\n\n"
                f"この内容を修正します。新しい値を入力してください。")
        await inter.response.send_message(info, ephemeral=True)

        # モーダルを開いて入力を受け付ける
        modal = ModifyModal(session_id=session_id,
                            match_id=m.id,
                            match_index=match_index,
                            current_winner=m.winner,
                            current_stage=m.stage or "")
        await inter.followup.send_modal(modal)

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