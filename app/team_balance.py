from itertools import combinations

def split_4v4_min_diff(players):
    """
    players: list[dict] 例: [{'user_id': 1, 'wins': 2}, ...]
      - 人数は「正の偶数」で、bot側の SESSION_MEMBER_NUM と同じ数が渡される想定
    戻り: (teamA_user_ids, teamB_user_ids)
    """
    n = len(players)
    if n <= 0 or n % 2 != 0:
        raise ValueError("players の人数は正の偶数である必要があります。")

    half = n // 2
    idxs = list(range(n))
    total_wins = sum(p['wins'] for p in players)

    best_idxs = None
    best_diff = float("inf")

    # 半分サイズ（half）の全組合せから、wins 差が最小になる二分割を探索
    for comb in combinations(idxs, half):
        sumA = sum(players[i]['wins'] for i in comb)
        diff = abs(total_wins - 2 * sumA)  # |(sumA) - (sumB)| の等価表現
        if diff < best_diff:
            best_diff = diff
            best_idxs = set(comb)

    teamA = [players[i]['user_id'] for i in range(n) if i in best_idxs]
    teamB = [players[i]['user_id'] for i in range(n) if i not in best_idxs]
    return teamA, teamB