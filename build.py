"""
NFL週次おすすめ試合ページ生成スクリプト。
ESPNの無料公開API(APIキー不要)のみを使用し、追加課金は発生しない。

週ごとに別のHTMLファイルを生成し、index.htmlから一覧で辿れるようにする。

判定基準:
  レギュラーシーズン/プレシーズン (Top10ランキング):
    1. 高得点          : 両チームの合計得点
    2. 第4Q逆転        : 第4クォーター開始以降に逆転した点差の大きさ
    3. オフェンス主導   : 得点プレーのうちオフェンス(パス/ラン)が占める割合
    4. プレーオフ影響度 : 両チームの勝率averageと同地区対決ボーナス、シーズン進行度による重み付け

  プレーオフ(ワイルドカード/ディビジョナル/カンファレンスチャンピオンシップ/スーパーボウル):
    全試合をランキングする(Top10で絞らない)。プレーオフ影響度は考慮しない(①②③のみ)。

49ersが絡む試合は、ランキングから除外し別枠で表示する(順位なし)。
"""
import json
import datetime
import random
import urllib.request
import os

SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
SCOREBOARD_URL_PARAMS = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?week={week}&seasontype={season_type}&year={year}"
SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary?event={event_id}"
STANDINGS_URL = "https://site.api.espn.com/apis/v2/sports/football/nfl/standings?season={year}"

NINERS_NAMES = {"49ers", "San Francisco 49ers"}

OFFENSE_KEYWORDS = ["pass", "rush", "run", "yd td", "td pass", "td run"]
DEFENSE_KEYWORDS = ["interception", "fumble", "blocked", "punt return", "kickoff return", "safety", "pick"]

MANIFEST_PATH = "manifest.json"

PLAYOFF_ROUND_LABELS = {
    1: ("ワイルドカード", "wildcard"),
    2: ("ディビジョナルラウンド", "divisional"),
    3: ("カンファレンスチャンピオンシップ", "championship"),
    5: ("スーパーボウル", "superbowl"),
}


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_current_scoreboard():
    return fetch_json(SCOREBOARD_URL)


def get_standings(year):
    try:
        return fetch_json(STANDINGS_URL.format(year=year))
    except Exception:
        return None


def build_winpct_lookup(standings_json):
    lookup = {}
    division_lookup = {}
    if not standings_json:
        return lookup, division_lookup
    try:
        for child in standings_json.get("children", []):
            division_name = child.get("name", "")
            for entry in child.get("standings", {}).get("entries", []):
                team = entry.get("team", {})
                abbr = team.get("abbreviation")
                stats = {s.get("name"): s.get("value") for s in entry.get("stats", [])}
                winpct = stats.get("winPercent")
                if abbr and winpct is not None:
                    lookup[abbr] = float(winpct)
                    division_lookup[abbr] = division_name
    except Exception:
        pass
    return lookup, division_lookup


def classify_scoring_play(play_text):
    text = (play_text or "").lower()
    if any(k in text for k in DEFENSE_KEYWORDS):
        return "defense"
    if any(k in text for k in OFFENSE_KEYWORDS):
        return "offense"
    return "other"


def analyze_game(event_id):
    """試合詳細(得点プレー履歴)を取得し、第4Q逆転とオフェンス比率を計算する。"""
    try:
        data = fetch_json(SUMMARY_URL.format(event_id=event_id))
    except Exception:
        return {"q4_comeback": 0, "offense_ratio": 0.5}

    scoring_plays = data.get("scoringPlays", [])
    if not scoring_plays:
        return {"q4_comeback": 0, "offense_ratio": 0.5}

    offense_count = 0
    classified_count = 0
    max_deficit_overcome = 0

    for play in scoring_plays:
        period = play.get("period", {}).get("number", 0)
        away_score = play.get("awayScore")
        home_score = play.get("homeScore")

        kind = classify_scoring_play(play.get("text", ""))
        if kind in ("offense", "defense"):
            classified_count += 1
            if kind == "offense":
                offense_count += 1

        if away_score is not None and home_score is not None and period >= 4:
            deficit = abs(away_score - home_score)
            max_deficit_overcome = max(max_deficit_overcome, deficit)

    offense_ratio = (offense_count / classified_count) if classified_count else 0.5
    return {"q4_comeback": max_deficit_overcome, "offense_ratio": offense_ratio}


def normalize(values):
    if not values:
        return {}
    lo, hi = min(values.values()), max(values.values())
    if hi == lo:
        return {k: 0.5 for k in values}
    return {k: (v - lo) / (hi - lo) for k, v in values.items()}


def stars_from_rank(index, total):
    """0始まりのindexから5~1の星数を割り当てる(上位ほど星が多い)。"""
    fraction = index / max(total - 1, 1)
    if fraction <= 0.2:
        return 5
    if fraction <= 0.4:
        return 4
    if fraction <= 0.6:
        return 3
    if fraction <= 0.8:
        return 2
    return 1


OPENING_LINES = [
    "{away}と{home}、両軍譲らぬ意地がぶつかり合う。",
    "{venue}に集う者たちが目撃するのは、{away} vs {home}の決着。",
    "{away}が乗り込む{venue}。そこで{home}が待つ。",
    "因縁、誇り、そして次の一歩――{away} vs {home}。",
]
RIVALRY_LINES = [
    "同地区のライバル同士、譲れない一戦に火花が散る。",
    "順位表の行方を左右する、シーズンの分水嶺となる試合。",
    "プレーオフへの道筋がここで動き出すかもしれない。",
]
NON_RIVALRY_LINES = [
    "地区の壁を越えて、誰が真の強者かを証明する舞台。",
    "対戦成績よりも、今この瞬間の戦いがすべてを決める。",
]
PLAYOFF_STAKES_LINES = [
    "負ければ即終了。すべてを賭けた一発勝負。",
    "ここを勝ち抜いた者だけが、次のステージに進む。",
]
HYPE_HIGH_OFFENSE = [
    "両オフェンスが主役を奪い合う、息をのむ攻防が予想される。",
    "点の取り合いになるか――攻撃陣の真価が試される。",
]
HYPE_DEFENSE = [
    "一瞬のすきを突くディフェンスが、試合の流れを変えるかもしれない。",
    "守備陣の意地と意地がせめぎ合う、静かな緊迫戦。",
]
CLOSER_LINES = [
    "最後まで目を離せない、そう確信できる90分。",
    "結末は見るまでわからない。だからこそ、見る価値がある。",
    "歴史に刻まれる一戦になるかどうかは、あなたの目で確かめてほしい。",
]


def generate_teaser(game, is_playoff):
    rng = random.Random(game["id"])
    away, home = game["team_away"], game["team_home"]

    opening = rng.choice(OPENING_LINES).format(away=away, home=home, venue=game["venue"])
    if is_playoff:
        stakes = rng.choice(PLAYOFF_STAKES_LINES)
    else:
        stakes = rng.choice(RIVALRY_LINES if game.get("same_division") else NON_RIVALRY_LINES)
    hype = rng.choice(HYPE_HIGH_OFFENSE if game["offense_ratio"] >= 0.5 else HYPE_DEFENSE)
    closer = rng.choice(CLOSER_LINES)

    return f"{opening}{stakes}\n{hype}{closer}"


def resolve_week_meta(sb):
    """スコアボードのレスポンスから year/season_type/week番号とラベル・ファイル名を決定する。"""
    year = sb.get("season", {}).get("year", datetime.date.today().year)
    season_type = sb.get("season", {}).get("type", 2)
    week_number = sb.get("week", {}).get("number", 0)

    if season_type == 1:
        label = f"プレシーズン Week{week_number}"
        filename = f"{year}-pre{week_number}.html"
        sort_key = (year, 1, week_number)
    elif season_type == 2:
        label = f"レギュラーシーズン Week{week_number}"
        filename = f"{year}-week{week_number}.html"
        sort_key = (year, 2, week_number)
    else:
        round_label, round_slug = PLAYOFF_ROUND_LABELS.get(week_number, (f"プレーオフ Week{week_number}", f"playoff{week_number}"))
        label = round_label
        filename = f"{year}-{round_slug}.html"
        # スーパーボウルが最後に来るよう、ラウンド番号5(SB)を最大値として並べる
        order = {1: 3, 2: 4, 3: 5, 5: 6}.get(week_number, 9)
        sort_key = (year, order, 0)

    is_playoff = season_type == 3
    return year, season_type, week_number, label, filename, sort_key, is_playoff


def collect_games(sb, year, season_type, week_number, is_playoff):
    standings_json = get_standings(year)
    winpct_lookup, division_lookup = build_winpct_lookup(standings_json)

    if season_type == 2:
        progress_weight = min(week_number / 18, 1.0)
    elif season_type == 3:
        progress_weight = 0  # プレーオフでは影響度を考慮しない
    else:
        progress_weight = 0.2

    games = []
    for event in sb.get("events", []):
        competitions = event.get("competitions", [{}])[0]
        status = competitions.get("status", {}).get("type", {}).get("name", "")
        if status != "STATUS_FINAL":
            continue

        competitors = competitions.get("competitors", [])
        if len(competitors) != 2:
            continue
        home = next((c for c in competitors if c.get("homeAway") == "home"), competitors[0])
        away = next((c for c in competitors if c.get("homeAway") == "away"), competitors[1])

        home_team = home.get("team", {})
        away_team = away.get("team", {})
        total_points = int(home.get("score", 0)) + int(away.get("score", 0))

        venue = competitions.get("venue", {})
        venue_name = venue.get("fullName", "")
        address = venue.get("address", {})
        city = address.get("city", "")
        state = address.get("state", "")
        location = f"{city}, {state}" if state else city

        date_str = event.get("date", "")
        try:
            dt = datetime.datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            dt_jst = dt + datetime.timedelta(hours=9)
            date_label = dt_jst.strftime("%Y年%m月%d日(%a)")
        except Exception:
            date_label = date_str

        analysis = analyze_game(event.get("id"))

        home_abbr = home_team.get("abbreviation")
        away_abbr = away_team.get("abbreviation")
        home_wp = winpct_lookup.get(home_abbr, 0.5)
        away_wp = winpct_lookup.get(away_abbr, 0.5)
        same_division = (
            division_lookup.get(home_abbr) == division_lookup.get(away_abbr)
            and division_lookup.get(home_abbr) is not None
        )
        playoff_impact = (((home_wp + away_wp) / 2) + (0.15 if same_division else 0)) * progress_weight

        is_niners = home_team.get("displayName") in NINERS_NAMES or away_team.get("displayName") in NINERS_NAMES

        games.append({
            "id": event.get("id"),
            "team_away": away_team.get("displayName"),
            "team_home": home_team.get("displayName"),
            "matchup": f"{away_team.get('displayName')} vs {home_team.get('displayName')}",
            "venue": f"{venue_name}({location})" if location else venue_name,
            "date": date_label,
            "total_points": total_points,
            "q4_comeback": analysis["q4_comeback"],
            "offense_ratio": analysis["offense_ratio"],
            "playoff_impact": playoff_impact,
            "same_division": same_division,
            "is_niners": is_niners,
        })
    return games


def score_games(games, is_playoff):
    points_n = normalize({g["id"]: g["total_points"] for g in games})
    comeback_n = normalize({g["id"]: g["q4_comeback"] for g in games})
    offense_n = normalize({g["id"]: g["offense_ratio"] for g in games})

    if is_playoff:
        for g in games:
            g["score"] = (
                0.40 * points_n.get(g["id"], 0.5)
                + 0.40 * comeback_n.get(g["id"], 0.5)
                + 0.20 * offense_n.get(g["id"], 0.5)
            )
    else:
        playoff_n = normalize({g["id"]: g["playoff_impact"] for g in games})
        for g in games:
            g["score"] = (
                0.30 * points_n.get(g["id"], 0.5)
                + 0.30 * comeback_n.get(g["id"], 0.5)
                + 0.20 * offense_n.get(g["id"], 0.5)
                + 0.20 * playoff_n.get(g["id"], 0.5)
            )

    games.sort(key=lambda g: g["score"], reverse=True)


def load_manifest():
    if os.path.exists(MANIFEST_PATH):
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_manifest(entries):
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)


def upsert_manifest(filename, label, sort_key):
    entries = load_manifest()
    entries = [e for e in entries if e["filename"] != filename]
    entries.append({"filename": filename, "label": label, "sort_key": sort_key})
    entries.sort(key=lambda e: tuple(e["sort_key"]), reverse=True)
    save_manifest(entries)
    return entries


def stars_html(n):
    return "★" * n + "☆" * (5 - n)


def teaser_html(teaser):
    return "".join(f"<p>{line}</p>" for line in teaser.split("\n"))


PAGE_STYLE = """
  body { font-family: -apple-system, BlinkMacSystemFont, "Hiragino Sans", sans-serif;
         background: #0b1f3a; color: #fff; margin: 0; padding: 16px; }
  a { color: #ffb612; }
  h1 { font-size: 1.3rem; margin-bottom: 4px; }
  .updated { color: #9fb3d1; font-size: 0.8rem; margin-bottom: 16px; }
  .criteria { background: #132c52; padding: 12px; border-radius: 8px; font-size: 0.85rem; margin-bottom: 20px; }
  h2 { font-size: 1.05rem; border-left: 4px solid #ffb612; padding-left: 8px; margin-top: 28px; }
  .card { background: #15335c; border-radius: 10px; padding: 14px 16px; margin-bottom: 10px; }
  .rank { font-weight: bold; color: #ffb612; font-size: 0.9rem; }
  .stars { color: #ffb612; font-size: 1.1rem; letter-spacing: 2px; }
  .matchup { font-size: 1.05rem; font-weight: bold; margin: 4px 0; }
  .venue, .date { font-size: 0.85rem; color: #c9d6ec; }
  .teaser { margin-top: 8px; font-size: 0.85rem; line-height: 1.5; color: #e8edf7; font-style: italic; }
  .teaser p { margin: 2px 0; }
  .footer { margin-top: 30px; font-size: 0.75rem; color: #7a8db0; text-align: center; }
  .week-list { list-style: none; padding: 0; }
  .week-list li { margin-bottom: 8px; }
  .week-list a { display: block; background: #15335c; padding: 12px 16px; border-radius: 8px; text-decoration: none; font-weight: bold; }
"""


def render_week_page(filename, label, niners_game, ranked_games, is_playoff):
    updated_at = (datetime.datetime.utcnow() + datetime.timedelta(hours=9)).strftime("%Y-%m-%d %H:%M JST")

    niners_block = ""
    if niners_game:
        niners_block = f"""
        <section class="niners">
          <h2>ご指定の試合</h2>
          <div class="card">
            <div class="stars">{stars_html(niners_game['stars'])}</div>
            <div class="matchup">{niners_game['matchup']}</div>
            <div class="venue">{niners_game['venue']}</div>
            <div class="date">{niners_game['date']}</div>
            <div class="teaser">{teaser_html(niners_game['teaser'])}</div>
          </div>
        </section>
        """
    else:
        niners_block = '<section class="niners"><p>今回49ersの試合はありません。</p></section>'

    cards = ""
    for i, g in enumerate(ranked_games, start=1):
        cards += f"""
        <div class="card">
          <div class="rank">{i}位</div>
          <div class="stars">{stars_html(g['stars'])}</div>
          <div class="matchup">{g['matchup']}</div>
          <div class="venue">{g['venue']}</div>
          <div class="date">{g['date']}</div>
          <div class="teaser">{teaser_html(g['teaser'])}</div>
        </div>
        """
    if not ranked_games and not niners_game:
        cards = "<p>現在表示できる試合データがありません。</p>"

    ranking_title = "全試合ランキング" if is_playoff else "今週の注目試合 Top10"
    criteria_text = (
        "判定基準：①高得点 ②第4Qの逆転劇 ③オフェンス主導の試合展開"
        if is_playoff
        else "判定基準：①高得点 ②第4Qの逆転劇 ③オフェンス主導の試合展開 ④プレーオフ影響度"
    )

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NFL観戦ガイド - {label}</title>
<style>{PAGE_STYLE}</style>
</head>
<body>
  <p><a href="index.html">&larr; Week一覧へ戻る</a></p>
  <h1>今週のNFL観戦ガイド：{label}</h1>
  <div class="updated">最終更新: {updated_at}</div>
  <div class="criteria">
    {criteria_text}<br>
    ※結果・スコアは記載していません。
  </div>
  {niners_block}
  <h2>{ranking_title}</h2>
  {cards}
  <div class="footer">このページは毎週月曜18時頃(JST)に自動更新されます。</div>
</body>
</html>
"""
    with open(filename, "w", encoding="utf-8") as f:
        f.write(html)


def render_index(entries):
    items = "".join(
        f'<li><a href="{e["filename"]}">{e["label"]}</a></li>' for e in entries
    )
    if not items:
        items = "<li>まだページがありません。シーズン開幕をお待ちください。</li>"

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NFL観戦ガイド - Week一覧</title>
<style>{PAGE_STYLE}</style>
</head>
<body>
  <h1>NFL観戦ガイド</h1>
  <div class="criteria">週ごとに観戦おすすめ試合をランキング形式でまとめています。下の一覧から見たいWeekを選んでください。</div>
  <ul class="week-list">
    {items}
  </ul>
</body>
</html>
"""
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)


def main():
    sb = get_current_scoreboard()
    year, season_type, week_number, label, filename, sort_key, is_playoff = resolve_week_meta(sb)

    games = collect_games(sb, year, season_type, week_number, is_playoff)

    if not games:
        render_index(load_manifest())
        return

    score_games(games, is_playoff)

    niners_game = next((g for g in games if g["is_niners"]), None)
    ranking_games = [g for g in games if not g["is_niners"]]

    ranked_games = ranking_games if is_playoff else ranking_games[:10]
    for i, g in enumerate(ranked_games):
        g["stars"] = stars_from_rank(i, len(ranked_games))
        g["teaser"] = generate_teaser(g, is_playoff)

    if niners_game:
        all_sorted_ids = [g["id"] for g in games]
        idx = all_sorted_ids.index(niners_game["id"])
        niners_game["stars"] = stars_from_rank(idx, len(games))
        niners_game["teaser"] = generate_teaser(niners_game, is_playoff)

    render_week_page(filename, label, niners_game, ranked_games, is_playoff)

    entries = upsert_manifest(filename, label, list(sort_key))
    render_index(entries)


if __name__ == "__main__":
    main()
