from itertools import combinations


def split_4v4_min_diff(players):
    """
    players: list[dict] 例: [{'user_id':1,'wins':2}, ...]（8人想定）
    戻り: (teamA_ids, teamB_ids)
    """
    idxs = list(range(len(players)))
    total = sum(p['wins'] for p in players)
    best, best_diff = None, 10**9
    for comb in combinations(idxs, 4):
        sumA = sum(players[i]['wins'] for i in comb)
        diff = abs(total - 2*sumA)
        if diff < best_diff:
            best_diff, best = diff, comb
    teamA = [players[i]['user_id'] for i in best]
    teamB = [players[i] for i in idxs if i not in best]
    # 上の行は user_id で返したいので修正
    teamB = [players[i]['user_id'] for i in idxs if i not in best]
    return teamA, teamB