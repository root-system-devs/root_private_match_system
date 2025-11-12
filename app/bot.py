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
from sqlalchemy import select, and_, or_, func, desc, delete, update
from .db import SessionLocal, init_models
from .models import User, Season, EntryBox, EntryApplication, Session as GameSession, Entry, SessionStat, SessionSettlement, SeasonScore, Match, SeasonParticipant
from .team_balance import split_4v4_min_diff
from typing import Optional

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)


ROOM_LABELS = list("123456789")
# 1セッションあたりの人数（テストでは2や4に変更可能）
SESSION_MEMBER_NUM = 2


@bot.event
async def on_ready():
    await init_models()
    await bot.tree.sync()
    bot.add_view(RegisterView())
    print(f"Logged in as {bot.user}")

def compute_initial_rate_from_xp(xp: float) -> float:
    """
    xp から初期レートを作る。（暫定版）
    """
    base = xp - 1000
    if base <= 2500:
        return base
    if base <= 0:
        return 1000.0
    return 2500.0

def calc_delta_rate(user_rate: float, wins: int, avg_rate: float, max_wins: int, k: float) -> float:
                perf = (wins / max_wins) - 0.5
                diff_term = (avg_rate - user_rate) / 400.0
                return k * (perf + diff_term)

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

async def ensure_entry_box(db, season_id: int, week: int) -> EntryBox:
    box = await db.scalar(
        select(EntryBox).where(
            and_(
                EntryBox.season_id == season_id,
                EntryBox.week_number == week,
            )
        )
    )
    if not box:
        box = EntryBox(season_id=season_id, week_number=week, status="open")
        db.add(box)
        await db.commit()
        await db.refresh(box)
    return box

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
        guild.default_role: discord.PermissionOverwrite(view_channel=True, read_message_history=True),
        # 送信:
        guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
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
    uids = [e.user_id for e in ents][:SESSION_MEMBER_NUM] # 8人に制限
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
    if len(players) < SESSION_MEMBER_NUM:
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

    season = await get_active_season(db)
    if not season:
        return "アクティブなシーズンが見つかりません。"

    # --- ① 既存の精算があれば巻き戻す ---
    previous_settlements = (await db.execute(
        select(SessionSettlement).where(
            and_(SessionSettlement.season_id == season.id,
                 SessionSettlement.session_id == session_id)
        )
    )).scalars().all()

    for stl in previous_settlements:
        sc = await db.scalar(select(SeasonScore).where(
            and_(SeasonScore.season_id == season.id,
                 SeasonScore.user_id   == stl.user_id)
        ))
        if sc:
            sc.win_points = int(sc.win_points) - int(stl.win_delta)
            sc.rate       = float(sc.rate)     - float(stl.rate_delta)
        # 履歴は削除（置き換え前提）
        await db.delete(stl)
    if previous_settlements:
        await db.commit()

    # --- ② 最新のセッション成績を取得 ---
    stats = (await db.execute(
        select(SessionStat).where(SessionStat.session_id == session_id)
    )).scalars().all()

    if not stats:
        # 参加者なし：ステータスのみ更新（巻き戻し済みならそのまま）
        sess.status = "finished"
        await db.commit()
        return f"Session {session_id} を終了しました。（参加者なし）"

    participant_ids = [st.user_id for st in stats]

    # SeasonScore / User を用意
    score_rows = (await db.execute(
        select(SeasonScore).where(
            and_(SeasonScore.season_id == season.id,
                 SeasonScore.user_id.in_(participant_ids))
        )
    )).scalars().all()
    score_map = {s.user_id: s for s in score_rows}

    users = (await db.execute(select(User).where(User.id.in_(participant_ids)))).scalars().all()
    user_map = {u.id: u for u in users}

    # SeasonScore が無い人は初期化（rate は xp or 1000）
    for uid in participant_ids:
        if uid not in score_map:
            init_rate = (user_map.get(uid).xp if user_map.get(uid) else None) or 1000.0
            sc = SeasonScore(season_id=season.id, user_id=uid,
                             entry_points=0.0, win_points=0, rate=init_rate)
            db.add(sc)
            score_map[uid] = sc
    await db.commit()

    # 平均レート / 最大勝数
    rates = [score_map[uid].rate for uid in participant_ids]
    avg_rate = sum(rates)/len(rates) if rates else 1000.0
    max_wins = max(int(s.wins) for s in stats) if stats else 1

    k = 20.0

    # --- ③ 最新の結果で再精算し、履歴を記録 ---
    for st in stats:
        uid = st.user_id
        sc  = score_map[uid]

        win_delta  = int(st.wins)                     # 今セッションでの勝数加算
        rate_delta = float(calc_delta_rate(sc.rate, int(st.wins), avg_rate, max_wins, k))

        sc.win_points += win_delta
        sc.rate       += rate_delta

        db.add(SessionSettlement(
            season_id=season.id, session_id=session_id, user_id=uid,
            win_delta=win_delta, rate_delta=rate_delta
        ))

    # セッション終了（※ undo で減って10未満になったら live に戻す仕様にするなら、ここは呼ぶ側で制御）
    sess.status = "finished"
    await db.commit()

    return (f"Session {session_id} を終了し、当日の勝数・レートを精算しました。"
            f"（平均レート: {avg_rate:.1f}, K={k:g}）")

async def _reopen_session_if_finished(db, session_id: int):
    sess = await db.get(GameSession, session_id)
    if not sess or sess.status != "finished":
        return
    season = await get_active_season(db)
    if not season:
        return

    settlements = (await db.execute(
        select(SessionSettlement).where(
            and_(
                SessionSettlement.season_id == season.id,
                SessionSettlement.session_id == session_id,
            )
        )
    )).scalars().all()

    for stl in settlements:
        sc = await db.scalar(select(SeasonScore).where(
            and_(SeasonScore.season_id == season.id,
                 SeasonScore.user_id   == stl.user_id)
        ))
        if sc:
            sc.win_points -= int(stl.win_delta)
            sc.rate       -= float(stl.rate_delta)
        await db.delete(stl)

    sess.status = "live"
    await db.commit()

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
                # 参加者登録
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
                    initial_rate = compute_initial_rate_from_xp(init_rate)

                    score = SeasonScore(
                        season_id=season.id,
                        user_id=user.id,
                        entry_points=0.0,
                        win_points=0,
                        rate=initial_rate,
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
                        msg_tail = f"初期レートは {initial_rate} です。ロール「{role_name}」を付与しました。"
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
@app_commands.checks.has_permissions(manage_guild=True)
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
    
class RateResetModal(ui.Modal, title="XPを入力（レートリセット）"):
    def __init__(self, target_user_id: int, target_season_id: int, season_name: str):
        super().__init__(timeout=180)
        self.target_user_id = target_user_id
        self.target_season_id = target_season_id
        self.season_name = season_name

        self.xp_input = ui.TextInput(
            label="新しいXP",
            placeholder="例）2000",
            required=True,
            max_length=12,
        )
        self.add_item(self.xp_input)

    async def on_submit(self, inter: Interaction):
        # 数値パース
        try:
            xp_val = float(str(self.xp_input.value).strip())
        except ValueError:
            await inter.response.send_message("数値を入力してください。", ephemeral=True)
            return

        async with SessionLocal() as db:
            user = await db.get(User, self.target_user_id)
            season = await db.get(Season, self.target_season_id)
            if not user or not season:
                await inter.response.send_message("対象ユーザーまたはシーズンが見つかりませんでした。", ephemeral=True)
                return

            # ここで「そのシーズンの参加者かどうか」を SeasonScore で判定
            score = await db.scalar(
                select(SeasonScore).where(
                    and_(
                        SeasonScore.season_id == season.id,
                        SeasonScore.user_id == user.id,
                    )
                )
            )

            # 参加者なのでXPとレートを更新
            user.xp = xp_val
            initial_rate = compute_initial_rate_from_xp(xp_val)
            score.rate = initial_rate
            await db.commit()

        await inter.response.send_message(
            f"{user.display_name} さんのXPを **{xp_val}** に更新し、"
            f"シーズン{self.season_name}でのレートを **{initial_rate}** にリセットしました。",
            ephemeral=True,
        )

@bot.tree.command(description="指定ユーザーのXPを入力し、レートをリセット（管理者）")
@app_commands.checks.has_permissions(manage_guild=True)
async def reset_rate(inter: Interaction, season_name: str, discord_id: str):
    # メンション形式でも数値でもOKにする
    raw = discord_id.strip()
    if raw.startswith("<@") and raw.endswith(">"):
        raw = raw[2:-1]
        if raw.startswith("!"):
            raw = raw[1:]

    async with SessionLocal() as db:
        # 1) シーズン取得
        season = await db.scalar(select(Season).where(Season.name == season_name))
        if not season:
            await inter.response.send_message("指定されたシーズンが見つかりません。", ephemeral=True)
            return

        # 2) ユーザー取得（discord_user_idベース）
        user = await db.scalar(select(User).where(User.discord_user_id == raw))
        if not user:
            await inter.response.send_message("指定されたDiscord IDのユーザーが見つかりません。", ephemeral=True)
            return

        # 3) そのシーズンの参加者かどうかを SeasonScore で確認
        score = await db.scalar(
            select(SeasonScore).where(
                and_(
                    SeasonScore.season_id == season.id,
                    SeasonScore.user_id == user.id,
                )
            )
        )
        if not score:
            await inter.response.send_message(
                f"{user.display_name} さんはシーズン「{season.name}」の参加者ではありません。",
                ephemeral=True,
            )
            return

    # 参加していることが分かったのでモーダルを出す
    modal = RateResetModal(
        target_user_id=user.id,
        target_season_id=season.id,
        season_name=season.name,
    )
    await inter.response.send_modal(modal)
    

@bot.tree.command(description="アクティブシーズンを作成（管理者）")
@commands.has_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
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
@app_commands.checks.has_permissions(manage_guild=True)
async def announce(inter: Interaction, week: int):
    async with SessionLocal() as db:
        season = await get_active_season(db)
        if not season:
            await inter.response.send_message("アクティブなシーズンがありません。/create_season で作成してください。", ephemeral=True)
            return
        # 募集箱を用意する（なければ作る）
        await ensure_entry_box(db, season.id, week)

    await inter.channel.send(
        embed=discord.Embed(
            title=f"Week {week} 参加募集",
            description="下のボタンで参加/キャンセル。締切まで変更可。"
        ),
        view=EntryView(week)
    )
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
                await inter.response.send_message("現在アクティブなシーズンがありません。", ephemeral=True)
                return

            # 募集箱を探す（なければ締め切り扱い）
            box = await db.scalar(
                select(EntryBox).where(
                    and_(
                        EntryBox.season_id == season.id,
                        EntryBox.week_number == self.week,
                    )
                )
            )
            if not box or box.status != "open":
                await inter.response.send_message(
                    f"Week {self.week} の募集は締め切られています。",
                    ephemeral=True,
                )
                return

            # シーズン参加者チェック（元のまま）
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
                    f"{inter.user.mention} さんはまだシーズン{season.name}の参加者ではありません。",
                    ephemeral=True,
                )
                return

            # この募集箱でのエントリを探す/作る
            ent = await db.scalar(
                select(EntryApplication).where(
                    and_(
                        EntryApplication.entry_box_id == box.id,
                        EntryApplication.user_id == user.id,
                    )
                )
            )
            if not ent:
                ent = EntryApplication(entry_box_id=box.id, user_id=user.id, status="confirmed")
                db.add(ent)

                # 参加ポイント加算（SeasonScoreは元の流れに合わせておく）
                score = await db.scalar(
                    select(SeasonScore).where(
                        and_(SeasonScore.season_id == season.id, SeasonScore.user_id == user.id)
                    )
                )
                if not score:
                    score = SeasonScore(
                        season_id=season.id,
                        user_id=user.id,
                        entry_points=0.0,
                        win_points=0,
                    )
                    db.add(score)
                score.entry_points += 0.5
                await db.commit()
                await inter.response.send_message("参加を受け付けました（+0.5pt）", ephemeral=True)
            else:
                if ent.status == "canceled":
                    ent.status = "confirmed"
                    score = await db.scalar(
                        select(SeasonScore).where(
                            and_(SeasonScore.season_id == season.id, SeasonScore.user_id == user.id)
                        )
                    )
                    if not score:
                        score = SeasonScore(
                            season_id=season.id,
                            user_id=user.id,
                            entry_points=0.0,
                            win_points=0,
                        )
                        db.add(score)
                    score.entry_points += 0.5
                    await db.commit()
                    await inter.response.send_message("再参加を受け付けました（+0.5pt）", ephemeral=True)
                else:
                    await inter.response.send_message("既に参加登録済みです。", ephemeral=True)

    @ui.button(label="キャンセル", style=discord.ButtonStyle.danger)
    async def cancel(self, inter: Interaction, button: ui.Button):
        async with SessionLocal() as db:
            user = await ensure_user(db, inter.user)
            season = await get_active_season(db)
            # 募集箱が閉じていたらキャンセルも不可
            box = await db.scalar(
                select(EntryBox).where(
                    and_(
                        EntryBox.season_id == season.id,
                        EntryBox.week_number == self.week,
                    )
                )
            )
            if not box or box.status != "open":
                await inter.response.send_message(
                    f"Week {self.week} の募集は締め切られているため、ここからは変更できません。",
                    ephemeral=True,
                )
                return

            ent = await db.scalar(
                select(EntryApplication).where(
                    and_(
                        EntryApplication.entry_box_id == box.id,
                        EntryApplication.user_id == user.id,
                    )
                )
            )
            if ent and ent.status == "confirmed":
                ent.status = "canceled"
                score = await db.scalar(
                    select(SeasonScore).where(
                        and_(SeasonScore.season_id == season.id, SeasonScore.user_id == user.id)
                    )
                )
                if score:
                    score.entry_points -= 0.5
                await db.commit()
                await inter.response.send_message("キャンセルしました（-0.5pt）。", ephemeral=True)
            else:
                await inter.response.send_message("参加登録が見つからないか、すでにキャンセル済みです。", ephemeral=True)

@bot.tree.command(description="締切：優先度→先着→レート順で部屋確定（管理者）")
@app_commands.checks.has_permissions(manage_guild=True)
async def close_entries(inter: Interaction, week: int):
    async with SessionLocal() as db:
        season = await get_active_season(db)
        # 募集箱を取る（無かったらそもそも募集してない）
        box = await db.scalar(
            select(EntryBox).where(
                and_(
                    EntryBox.season_id == season.id,
                    EntryBox.week_number == week,
                )
            )
        )
        if not box:
            await inter.response.send_message("この週の募集箱が見つかりません。", ephemeral=True)
            return
        if box.status != "open":
            await inter.response.send_message("この週はすでに締め切られています。", ephemeral=True)
            return

        # 募集箱の confirmed を全部取る
        rows = await db.execute(
            select(
                EntryApplication.user_id,
                EntryApplication.created_at,
                User.priority,
                User.xp,
                User.discord_user_id,
                User.display_name,
            )
            .join(User, User.id == EntryApplication.user_id)
            .where(
                EntryApplication.entry_box_id == box.id,
                EntryApplication.status == "confirmed",
            )
        )
        records = list(rows.all())

        # 人数チェック（SESSION_MEMBER_NUMはすでに定義済みの想定）
        if len(records) < SESSION_MEMBER_NUM:
            # 募集箱をキャンセル扱いにする
            box.status = "canceled"
            await db.commit()

            # 応募していた人のpriority +1
            for (uid, _ts, prio, _xp, discord_uid, disp) in records:
                await db.execute(
                    update(User).where(User.id == uid).values(priority=prio + 1)
                )
            await db.commit()

            mentions = ", ".join(
                f"{disp}(<@{discord_uid}>)" for (_uid, _ts, _prio, _xp, discord_uid, disp) in records
            )
            await inter.response.send_message(
                f"Week {week} の参加希望者が{SESSION_MEMBER_NUM}人未満だったため、募集をキャンセルしました。\n"
                f"優先度を+1したメンバー: {mentions}",
                ephemeral=False,
            )
            return

        # 以降は今までと同じ「優先度→先着→レート」で切る処理
        records.sort(key=lambda r: (-r.priority, r.created_at))
        num_take = (len(records) // SESSION_MEMBER_NUM) * SESSION_MEMBER_NUM
        selected = records[:num_take]
        dropped = records[num_take:]

        # 落選者 priority +1
        dropped_mentions = []
        for (uid, _ts, prio, _xp, discord_uid, disp) in dropped:
            await db.execute(
                update(User).where(User.id == uid).values(priority=prio + 1)
            )
            dropped_mentions.append(f"{disp}(<@{discord_uid}>)")
        if dropped:
            await db.commit()

        # 選抜者 priority = 0
        if selected:
            await db.execute(
                update(User)
                .where(User.id.in_([r.user_id for r in selected]))
                .values(priority=0)
            )
            await db.commit()

        # レート順に並べてから分割
        selected.sort(key=lambda r: (-r.xp, r.created_at))
        selected_ids = [r.user_id for r in selected]
        chunks = [selected_ids[i:i+SESSION_MEMBER_NUM] for i in range(0, len(selected_ids), SESSION_MEMBER_NUM)]
        summary_msgs = []

        for idx, chunk in enumerate(chunks):
            if len(chunk) < SESSION_MEMBER_NUM:
                break

            room = ROOM_LABELS[idx]
            sess = GameSession(
                season_id=season.id,
                week_number=week,
                room_label=room,
                scheduled_at=datetime.now(timezone.utc),
                status="scheduled",
            )
            db.add(sess)
            await db.commit(); await db.refresh(sess)

            # ここで“本番用のEntry”を今まで通り作る（部屋の参加メンバーとして）
            for uid in chunk:
                db.add(Entry(session_id=sess.id, user_id=uid, status="confirmed"))
            await db.commit()

            # あとは既存と同じでOK
            await init_session_stats(db, sess.id, chunk)
            start_msg = await _start_session(db, sess.id)
            next_msg = await _create_next_match_and_message(db, sess.id)

            mentions = " ".join([
                f"<@{(await db.scalar(select(User.discord_user_id).where(User.id == uid)))}>"
                for uid in chunk
            ])

            msg = (
                f"**Week {week} 部屋 {room} — Session {sess.id}**\n"
                f"{start_msg}\n\n"
                f"参加者: {mentions}\n\n"
                f"{next_msg}"
            )
            await _post_to_room_channel(inter, room, msg)
            summary_msgs.append(f"部屋 {room} を開始し、チームを発表しました。")

        # 募集箱を締め切り
        box.status = "closed"
        await db.commit()

        await inter.response.send_message("\n".join(summary_msgs), ephemeral=False)

        if dropped_mentions:
            await inter.followup.send(
                f"{SESSION_MEMBER_NUM}人に満たず見送りとなったメンバー（priority +1 済み）: "
                + ", ".join(dropped_mentions),
                ephemeral=False
            )
            

@bot.tree.command(description="ドタキャンが出たセッションを再募集モードにする（管理者）")
@app_commands.checks.has_permissions(manage_guild=True)
async def reopen_session(inter: Interaction, session_id: int, dropout_discord_ids: str):
    """
    dropout_discord_ids: カンマ or スペース区切りで複数指定想定
      例: "1234567890, 222222222222" みたいに
    """
    async with SessionLocal() as db:
        sess = await db.get(GameSession, session_id)
        if not sess:
            await inter.response.send_message("指定されたセッションが見つかりません。", ephemeral=True)
            return

        # このセッションは「まだ試合開始前に戻す」ものなので scheduled に戻しておく
        sess.status = "scheduled"

        # 既存で自動生成されていた試合は一旦全部消しておく
        await db.execute(delete(Match).where(Match.session_id == session_id))

        # ドタキャンのDiscord IDをパース
        raw_ids = [x.strip() for x in dropout_discord_ids.replace(",", " ").split() if x.strip()]
        removed_mentions = []
        for disc_id in raw_ids:
            # DBにいるユーザを探す
            u = await db.scalar(select(User).where(User.discord_user_id == disc_id))
            if not u:
                continue

            # entries から落とす
            ent = await db.scalar(
                select(Entry).where(and_(Entry.session_id == session_id, Entry.user_id == u.id))
            )
            if ent:
                await db.delete(ent)

            # その人の session_stats も一応消しておく（init済みだった場合）
            await db.execute(
                delete(SessionStat).where(
                    and_(SessionStat.session_id == session_id, SessionStat.user_id == u.id)
                )
            )

            removed_mentions.append(f"{u.display_name}(<@{u.discord_user_id}>)")

        await db.commit()

    # 再募集用のボタンを表示
    view = RefillSessionView(session_id=session_id)
    msg = (
        f"Session {session_id} でドタキャンがあったため再募集します。\n"
        f"足りない人数が埋まるまでこのボタンで参加してください。\n"
    )
    if removed_mentions:
        msg += "ドタキャン扱い: " + ", ".join(removed_mentions)

    await inter.response.send_message(msg, view=view, ephemeral=False)
    
class RefillSessionView(ui.View):
    def __init__(self, session_id: int):
        super().__init__(timeout=None)
        self.session_id = session_id

    @ui.button(label="この部屋に参加する", style=discord.ButtonStyle.success)
    async def join_session(self, inter: Interaction, button: ui.Button):
        async with SessionLocal() as db:
            sess = await db.get(GameSession, self.session_id)
            if not sess or sess.status in ("canceled", "finished"):
                await inter.response.send_message("このセッションには参加できません。", ephemeral=True)
                return

            season = await db.scalar(select(Season).where(Season.id == sess.season_id))
            if not season:
                await inter.response.send_message("シーズンが見つかりません。", ephemeral=True)
                return

            user = await ensure_user(db, inter.user)

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
                    f"{inter.user.mention} さんはまだシーズン{season.name}の参加者ではありません。",
                    ephemeral=True,
                )
                return

            # いまの参加者(confirmed)を集める
            current_rows = await db.execute(
                select(Entry).where(
                    and_(Entry.session_id == self.session_id)
                )
            )
            entries = current_rows.scalars().all()
            confirmed_ids = [e.user_id for e in entries if e.status == "confirmed"]

            # このユーザのエントリがすでに存在するかどうかを見る
            my_entry = next((e for e in entries if e.user_id == user.id), None)
            if my_entry and my_entry.status == "confirmed":
                # 本当に今も参加中なら弾く
                await inter.response.send_message("このセッションにはすでに参加しています。", ephemeral=True)
                return
            elif my_entry and my_entry.status != "confirmed":
                # 以前いたけどドタキャンなどでconfirmedじゃなくなっている → 復活させる
                my_entry.status = "confirmed"
                await db.commit()
            else:
                # 完全に初参加の場合
                if len(confirmed_ids) >= SESSION_MEMBER_NUM:
                    await inter.response.send_message("このセッションはすでに満員です。", ephemeral=True)
                    return
                db.add(Entry(session_id=self.session_id, user_id=user.id, status="confirmed"))
                await db.commit()

            # statsも念のため初期化（なかった人だけ）
            await init_session_stats(db, self.session_id, [user.id])

            # 最新の参加者数を数え直す
            current_rows = await db.execute(
                select(Entry.user_id).where(
                    and_(Entry.session_id == self.session_id, Entry.status == "confirmed")
                )
            )
            current_user_ids = [r[0] for r in current_rows.all()]

            # まだ足りてなければその旨伝えて終わり
            if len(current_user_ids) < SESSION_MEMBER_NUM:
                await inter.response.send_message(
                    f"参加を受け付けました。現在 {len(current_user_ids)}/{SESSION_MEMBER_NUM} 人です。",
                    ephemeral=True,
                )
                return

            # ここまで来たらちょうど満員
            sess.status = "scheduled"
            await db.commit()

            start_msg = await _start_session(db, self.session_id)
            next_msg = await _create_next_match_and_message(db, self.session_id)

            mentions = " ".join([
                f"<@{(await db.scalar(select(User.discord_user_id).where(User.id == uid)))}>"
                for uid in current_user_ids
            ])

            msg = (
                f"**Week {sess.week_number} 部屋 {sess.room_label} — Session {sess.id}（再募集完了）**\n"
                f"{start_msg}\n\n"
                f"参加者: {mentions}\n\n"
                f"{next_msg}"
            )
            await _post_to_room_channel(inter, sess.room_label, msg)

            await inter.response.send_message(
                "参加を受け付けました。定員に達したためチームを発表しました。",
                ephemeral=True,
            )

@bot.tree.command(description="再募集でも人が集まらなかったセッションをキャンセル（管理者）")
@app_commands.checks.has_permissions(manage_guild=True)
async def cancel_reopen_session(inter: Interaction, session_id: int):
    async with SessionLocal() as db:
        sess = await db.get(GameSession, session_id)
        if not sess:
            await inter.response.send_message("指定されたセッションが見つかりません。", ephemeral=True)
            return

        # 現在残っている参加者を取得
        rows = await db.execute(
            select(Entry.user_id, User.priority, User.display_name, User.discord_user_id)
            .join(User, User.id == Entry.user_id)
            .where(
                and_(Entry.session_id == session_id, Entry.status == "confirmed")
            )
        )
        remains = rows.all()

        # セッションをキャンセル扱いに
        sess.status = "canceled"
        sess.room_label = "CANCELED"
        await db.commit()

        # 残ってた人の priority を +1
        for (uid, prio, disp, disc_id) in remains:
            await db.execute(
                update(User).where(User.id == uid).values(priority=prio + 1)
            )
        await db.commit()

    # 公開で知らせる
    if remains:
        mentions = ", ".join(f"{disp}(<@{disc_id}>)" for (_uid, _prio, disp, disc_id) in remains)
        msg = (
            f"Session {session_id} は再募集でも人数が集まらなかったためキャンセルしました。\n"
            f"以下のメンバーの優先度を+1しました: {mentions}"
        )
    else:
        msg = f"Session {session_id} は再募集でも人数が集まらなかったためキャンセルしました。"

    await inter.response.send_message(msg, ephemeral=False)

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
        if sess.room_label in ("PENDING", "CANCELED") or sess.status in ("scheduled", "canceled"):
            await inter.response.send_message(
                f"Session {session_id} はまだ部屋確定前か、キャンセル済みのため勝敗を登録できません。",
                ephemeral=True
            )
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
            # PENDINGやCANCELEDでは新しい試合は作らない
            if sess.room_label in ("PENDING", "CANCELED") or sess.status != "live":
                await inter.response.send_message(
                    f"Session {session_id} では新しい試合を作成できません。部屋確定後のセッションIDを指定してください。",
                    ephemeral=True
                )
                return

            msg = await _create_next_match_and_message(db, session_id)
            await _post_to_room_channel(inter, sess.room_label, msg)
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

        # 勝者
        self.winner_input = ui.TextInput(
            label=f"勝利チーム（A または B）: 現在={current_winner or '未設定'}",
            placeholder="A または B",
            default=current_winner or "",
            required=True,
            max_length=1,
        )
        # ステージ
        self.stage_input = ui.TextInput(
            label=f"ステージ名: 現在={current_stage or '未設定'}",
            placeholder="例) Museum d'Alfonsino",
            default=current_stage or "",
            required=False,
            max_length=64,
        )
        self.add_item(self.winner_input)
        self.add_item(self.stage_input)

    async def on_submit(self, inter: Interaction):
        async with SessionLocal() as db:
            m = await db.get(Match, self.match_id)
            if not m:
                await inter.response.send_message("対象の試合が見つかりませんでした。", ephemeral=True)
                return

            msg_edit = await _apply_match_edit(db, m, self.winner_input.value, self.stage_input.value)

            # 10勝到達チェック（修正後の状態で判定）
            ten = await db.scalar(
                select(SessionStat).where(
                    and_(SessionStat.session_id == self.session_id,
                         SessionStat.wins >= 10)
                )
            )

            if ten:
                # 10勝 → 冪等finish（内部で「巻き戻し→再精算」）
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

            # ▼▼ ここで呼ぶ：10勝未到達 → もし既に finished 済みなら「巻き戻して live に戻す」 ▼▼
            await _reopen_session_if_finished(db, self.session_id)

            # “最新の未確定1件だけ”掃除
            pending = await db.scalar(
                select(Match)
                .where(and_(Match.session_id == self.session_id, Match.winner == None))
                .order_by(desc(Match.match_index))
            )
            if pending:
                await db.delete(pending)
                await db.commit()

            # 次試合生成
            next_msg = await _create_next_match_and_message(db, self.session_id)

            # 部屋告知
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
                f"{msg_edit}\n最新の未確定1件を掃除し、次試合を部屋チャンネルへ投稿しました。",
                ephemeral=True
            )

@bot.tree.command(description="最新の試合結果を修正")
async def undo(inter: Interaction, session_id: int):
    async with SessionLocal() as db:
        # 1) winner が入っている中で一番新しい試合を取る
        latest_confirmed = await db.scalar(
            select(Match)
            .where(
                Match.session_id == session_id,
                Match.winner.is_not(None)
            )
            .order_by(desc(Match.match_index))
        )

        # なければ一応一番新しい試合を取る（初回保険）
        if latest_confirmed:
            target_match = latest_confirmed
        else:
            target_match = await db.scalar(
                select(Match)
                .where(Match.session_id == session_id)
                .order_by(desc(Match.match_index))
            )

        if not target_match:
            await inter.response.send_message("このセッションには試合がありません。", ephemeral=True)
            return

        # 対象セッションを取得
        sess = await db.get(GameSession, session_id)
        if not sess:
            await inter.response.send_message("セッションが見つかりません。", ephemeral=True)
            return

        # このセッションが属するシーズン
        season = await db.scalar(
            select(Season).where(Season.id == sess.season_id)
        )
        if not season:
            await inter.response.send_message("シーズンが見つかりません。", ephemeral=True)
            return

        # このセッションに参加していたユーザIDを取得（confirmed のみ）
        entry_rows = await db.execute(
            select(Entry.user_id)
            .where(and_(Entry.session_id == session_id, Entry.status == "confirmed"))
        )
        session_user_ids = [r[0] for r in entry_rows.all()]

        # ここから「後続セッションでレートが動いていないか」をチェック
        # 判定基準:
        #   同じシーズン・同じユーザーで、
        #   「このセッションより scheduled_at が後」または「同時刻で session_id が大きい」Session の Settlement で
        #   rate_delta が 0 でないものがあったらアウト
        # つまり “このセッション時点のレート != 現在のレート” とみなす

        # 対象セッションの時刻/IDを固定しておく
        target_ts = sess.scheduled_at
        target_sid = sess.id

        inconsistent = False
        bad_users: list[int] = []

        for uid in session_user_ids:
            later_delta_row = await db.execute(
                select(func.sum(SessionSettlement.rate_delta))
                .join(GameSession, GameSession.id == SessionSettlement.session_id)
                .where(
                    SessionSettlement.season_id == season.id,
                    SessionSettlement.user_id == uid,
                    or_(
                        GameSession.scheduled_at > target_ts,
                        and_(GameSession.scheduled_at == target_ts, GameSession.id > target_sid),
                    )
                )
            )
            later_sum = later_delta_row.scalar() or 0.0
            # ほんの少しの浮動小数誤差は許す
            if abs(later_sum) > 1e-6:
                inconsistent = True
                bad_users.append(uid)

        if inconsistent:
            # 誰かが次の週でレートを動かしているのでこのundoは危険
            # メンションできるようにDiscord IDも引いておく
            bad_discords = []
            for uid in bad_users:
                u = await db.scalar(select(User).where(User.id == uid))
                if u:
                    bad_discords.append(f"{u.display_name}(<@{u.discord_user_id}>)")
            users_text = ", ".join(bad_discords) if bad_discords else "一部参加者"

            await inter.response.send_message(
                f"このセッションの後に別の試合でレートが更新されている参加者がいるため、修正できません。\n"
                f"修正期限切れなので管理者に連絡してください。\n"
                f"該当: {users_text}",
                ephemeral=True,
            )
            return

        # ここまで通ったら「このセッション以降でレートが動いてない」ので安全にモーダルを出す
        room_label = sess.room_label if sess else "?"

        modal = UndoModal(
            session_id=session_id,
            match_id=target_match.id,
            room_label=room_label,
            current_winner=target_match.winner,
            current_stage=target_match.stage or "",
        )
        await inter.response.send_modal(modal)
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

        # ここで「今こうなってますよ」をラベルに含めておく
        self.winner_input = ui.TextInput(
            label=f"勝利チーム（A/B） 現在={current_winner or '未設定'}",
            placeholder="A または B",
            default=current_winner or "",
            required=True,
            max_length=1
        )
        self.stage_input = ui.TextInput(
            label=f"ステージ名 現在={current_stage or '未設定'}",
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
@app_commands.checks.has_permissions(manage_guild=True)
async def modify(inter: Interaction, session_id: int, match_index: int):
    async with SessionLocal() as db:
        m = await db.scalar(
            select(Match)
            .where(and_(Match.session_id == session_id, Match.match_index == match_index))
        )
        if not m:
            await inter.response.send_message("指定の試合が見つかりません。", ephemeral=True)
            return

        # ここで “最初の応答” としてモーダルを出す
        modal = ModifyModal(
            session_id=session_id,
            match_id=m.id,
            match_index=match_index,
            current_winner=m.winner,
            current_stage=m.stage or "",
        )
        await inter.response.send_modal(modal)

@bot.tree.command(description="【危険】指定シーズンのレートをMatchから再計算（管理者専用）")
@app_commands.checks.has_permissions(manage_guild=True)
async def recalc_season_rates(inter: Interaction, season_name: Optional[str] = None):
    """
    想定シナリオ:
      - modify で Match を書き換えた
      - SessionSettlement はもう信用できない
      - なので Match だけを信じて「そのシーズンを最初から」レート/勝数を積み直す
    """
    async with SessionLocal() as db:
        # 1. 対象シーズンの特定
        if season_name:
            season = await db.scalar(select(Season).where(Season.name == season_name))
        else:
            season = await get_active_season(db)
        if not season:
            await inter.response.send_message("シーズンが見つかりません。", ephemeral=True)
            return

        # 2. そのシーズンの参加者とユーザー情報を取る
        part_rows = await db.execute(
            select(SeasonParticipant, User)
            .join(User, User.id == SeasonParticipant.user_id)
            .where(SeasonParticipant.season_id == season.id)
        )
        participants = part_rows.all()  # [(SeasonParticipant, User), ...]

        if not participants:
            await inter.response.send_message("このシーズンには参加者がいません。", ephemeral=True)
            return

        # 3. 旧 SessionSettlement をすべて削除（このシーズン分だけ）
        await db.execute(
            delete(SessionSettlement).where(SessionSettlement.season_id == season.id)
        )
        await db.commit()

        # 4. SeasonScore を初期化（rateとwin_pointsだけ）
        #    entry_pointsはそのまま残す（大会参加回数などを壊さないため）
        current_rates: dict[int, float] = {}
        for _sp, u in participants:
            init_rate = compute_initial_rate_from_xp(u.xp)
            current_rates[u.id] = init_rate

            sc = await db.scalar(
                select(SeasonScore)
                .where(and_(SeasonScore.season_id == season.id, SeasonScore.user_id == u.id))
            )
            if sc:
                # 参加ポイントは保持、勝利ポイントとレートはリセット
                sc.rate = init_rate
                sc.win_points = 0
            else:
                # SeasonScoreがなかった人は新規に作る
                sc = SeasonScore(
                    season_id=season.id,
                    user_id=u.id,
                    entry_points=0.0,
                    win_points=0,
                    rate=init_rate,
                )
                db.add(sc)
        await db.commit()

        # 5. シーズン内のセッションを古い順に回す
        sess_rows = await db.execute(
            select(GameSession)
            .where(GameSession.season_id == season.id)
            .order_by(GameSession.scheduled_at, GameSession.id)
        )
        sessions = sess_rows.scalars().all()

        # CSVパーサ
        def _parse_ids(csv: str) -> list[int]:
            return [int(x) for x in csv.split(",") if x.strip()]

        for sess in sessions:
            # このセッションに参加している人
            entry_rows = await db.execute(
                select(Entry.user_id)
                .where(and_(Entry.session_id == sess.id, Entry.status == "confirmed"))
            )
            session_user_ids = [r[0] for r in entry_rows.all()]

            if not session_user_ids:
                # 誰もいないセッションはスキップ（PENDINGとかCANCELEDの名残り）
                continue

            # このセッションのMatchを試合順に取得
            match_rows = await db.execute(
                select(Match)
                .where(Match.session_id == sess.id)
                .order_by(Match.match_index)
            )
            matches = match_rows.scalars().all()

            # 6. Matchからこのセッションの「勝数」を組み立てる
            #    user_id -> wins_in_this_session
            session_wins: dict[int, int] = {uid: 0 for uid in session_user_ids}

            for m in matches:
                if not m.winner:
                    # winnerが入っていない試合は無視（まだ未確定）
                    continue
                team_a = _parse_ids(m.team_a_ids)
                team_b = _parse_ids(m.team_b_ids)
                if m.winner.upper() == "A":
                    winners = team_a
                else:
                    winners = team_b
                for uid in winners:
                    # セッション参加者に限って加算（保険）
                    if uid in session_wins:
                        session_wins[uid] += 1

            # 7. _finish_session と同じレート式を適用するための値を作る
            #   rates は「この時点の」各参加者のレート
            rates_for_this_session = [current_rates[uid] for uid in session_user_ids]
            avg_rate = sum(rates_for_this_session) / len(rates_for_this_session) if rates_for_this_session else 1000.0
            max_wins = max(session_wins.values()) if session_wins else 1
            if max_wins <= 0:
                max_wins = 1

            k = 20.0


            # 8. SessionStat をこの値で上書き（既存があれば消す/上書きする）
            #    まずこのセッションの古い SessionStat を消す
            await db.execute(
                delete(SessionStat).where(SessionStat.session_id == sess.id)
            )
            await db.commit()

            # 9. 各参加者に対してレートを更新＆SessionStatを作成＆SessionSettlementを作成
            for uid in session_user_ids:
                before_rate = current_rates.get(uid, 0.0)
                wins = session_wins.get(uid, 0)
                delta = calc_delta_rate(before_rate, wins, avg_rate, max_wins, k)
                after_rate = before_rate + delta

                # 現在レートを更新
                current_rates[uid] = after_rate

                # SeasonScore も更新（rate と win_points）
                sc = await db.scalar(
                    select(SeasonScore)
                    .where(and_(SeasonScore.season_id == season.id, SeasonScore.user_id == uid))
                )
                if sc:
                    sc.rate = after_rate
                    sc.win_points = (sc.win_points or 0) + wins

                # SessionStat を追加
                st = SessionStat(
                    session_id=sess.id,
                    user_id=uid,
                    wins=wins,
                )
                db.add(st)

                # SessionSettlement を追加（このセッションでどれだけ増えたか）
                ss = SessionSettlement(
                    season_id=season.id,
                    session_id=sess.id,
                    user_id=uid,
                    win_delta=wins,
                    rate_delta=delta,
                    calculated_at=datetime.now(timezone.utc),
                )
                db.add(ss)

            # このセッションを finished にしておくとわかりやすい
            sess.status = "finished"
            await db.commit()

        # 10. 完了通知
        await inter.response.send_message(
            f"シーズン「{season.name}」のレートと勝数を Match から再計算しました。\n"
            f"※modify実行後の整合性取りに使うことを想定しています。",
            ephemeral=True
        )

@bot.tree.command(description="リーダーボードを表示")
@commands.has_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
async def leaderboard(inter: Interaction, season_name: Optional[str] = None):
    async with SessionLocal() as db:
        # シーズン取得
        if season_name:
            season = await db.scalar(select(Season).where(Season.name == season_name))
        else:
            season = await get_active_season(db)

        if not season:
            await inter.response.send_message("シーズンが見つかりません。", ephemeral=True)
            return

        # ★ レート降順で上位10件だけ
        result = await db.execute(
            select(SeasonScore, User)
            .join(User, User.id == SeasonScore.user_id)
            .where(SeasonScore.season_id == season.id)
            .order_by(desc(SeasonScore.rate))
            .limit(10)
        )
        rows = result.all()

        if not rows:
            await inter.response.send_message("まだスコアがありません。", ephemeral=True)
            return

        lines = [f"**{season.name} Leaderboard (Top 10 / by Rate)**"]
        for i, (sc, u) in enumerate(rows, start=1):
            lines.append(f"{i}. {u.display_name} — {sc.rate:.1f}")

        await inter.response.send_message("\n".join(lines), ephemeral=False)


if __name__ == "__main__":
    bot.run(TOKEN)