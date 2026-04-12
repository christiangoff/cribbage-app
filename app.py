from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import json
import os
import random
import secrets
from itertools import combinations

from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from functools import wraps
from sqlalchemy import CheckConstraint, inspect, text
from werkzeug.security import check_password_hash, generate_password_hash

# ── Card engine ──────────────────────────────────────────────────────────────

RANKS = list("A23456789TJQK")
SUITS = list("HDCS")
SUIT_SYMBOLS = {"H": "♥", "D": "♦", "C": "♣", "S": "♠"}


def _rank(card: str) -> str:
    return card[:-1]


def _suit(card: str) -> str:
    return card[-1]


def card_rank_value(card: str) -> int:
    """Point value for pegging/counting (face=10, A=1)."""
    r = _rank(card)
    if r in ("T", "J", "Q", "K"):
        return 10
    if r == "A":
        return 1
    return int(r)


def card_order(card: str) -> int:
    """Ordinal 1..13 for run detection."""
    return RANKS.index(_rank(card)) + 1


def card_display(card: str) -> str:
    """Human-readable card, e.g. 'J♥'."""
    r = _rank(card)
    return r + SUIT_SYMBOLS[_suit(card)]


def new_deck() -> list[str]:
    deck = [r + s for s in SUITS for r in RANKS]
    random.shuffle(deck)
    return deck


def deal_hands(deck: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Deal 6 cards each; return (p1_hand, p2_hand, remaining_deck)."""
    return deck[:6], deck[6:12], deck[12:]


def score_pegging_play(pile_cards: list[str], new_card: str) -> tuple[int, list[str]]:
    """Score points earned by playing new_card onto pile_cards.
    Returns (total_points, list_of_reason_strings)."""
    pile = pile_cards + [new_card]
    total = sum(card_rank_value(c) for c in pile)
    points = 0
    reasons: list[str] = []

    # Fifteen / thirty-one
    if total == 15:
        points += 2
        reasons.append("fifteen for 2")
    if total == 31:
        points += 2
        reasons.append("31 for 2")

    # Pairs / trips / quads — check tail of pile
    tail = [pile[-(i + 1)] for i in range(min(4, len(pile)))]
    same_count = 1
    for c in tail[1:]:
        if _rank(c) == _rank(tail[0]):
            same_count += 1
        else:
            break
    pair_pts = {2: 2, 3: 6, 4: 12}
    pair_names = {2: "pair", 3: "three of a kind", 4: "four of a kind"}
    if same_count >= 2:
        points += pair_pts[same_count]
        reasons.append(f"{pair_names[same_count]} for {pair_pts[same_count]}")

    # Runs — longest run at tail of pile (min 3)
    for run_len in range(min(7, len(pile)), 2, -1):
        tail_run = pile[-run_len:]
        orders = sorted(card_order(c) for c in tail_run)
        if orders == list(range(orders[0], orders[0] + run_len)):
            points += run_len
            reasons.append(f"run of {run_len} for {run_len}")
            break

    return points, reasons


def score_hand(hand4: list[str], starter: str, is_crib: bool = False) -> tuple[int, list[str]]:
    """Score a 4-card hand + starter. Returns (points, reasons)."""
    all5 = hand4 + [starter]
    points = 0
    reasons: list[str] = []

    # Fifteens
    fifteen_count = 0
    for r in range(2, 6):
        for combo in combinations(all5, r):
            if sum(card_rank_value(c) for c in combo) == 15:
                fifteen_count += 1
    if fifteen_count:
        pts = fifteen_count * 2
        points += pts
        reasons.append(f"fifteens for {pts}")

    # Pairs
    pair_count = 0
    for a, b in combinations(all5, 2):
        if _rank(a) == _rank(b):
            pair_count += 1
    if pair_count:
        pts = pair_count * 2
        points += pts
        reasons.append(f"pairs for {pts}")

    # Runs
    best_run = 0
    run_count = 0
    for r in range(5, 2, -1):
        for combo in combinations(all5, r):
            orders = sorted(card_order(c) for c in combo)
            if orders == list(range(orders[0], orders[0] + r)):
                if r > best_run:
                    best_run = r
                    run_count = 1
                elif r == best_run:
                    run_count += 1
        if best_run:
            break
    if best_run:
        pts = best_run * run_count
        points += pts
        reasons.append(f"run(s) for {pts}")

    # Flush (4-card flush in hand only; 5-card also counts for crib)
    hand_suits = [_suit(c) for c in hand4]
    if len(set(hand_suits)) == 1:
        if _suit(starter) == hand_suits[0]:
            points += 5
            reasons.append("5-card flush for 5")
        elif not is_crib:
            points += 4
            reasons.append("flush for 4")

    # Nobs (J in hand matching starter suit)
    for c in hand4:
        if _rank(c) == "J" and _suit(c) == _suit(starter):
            points += 1
            reasons.append("nobs for 1")
            break

    return points, reasons


def legal_pegging_plays(hand: list[str], current_count: int) -> list[str]:
    return [c for c in hand if card_rank_value(c) + current_count <= 31]


app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///cribbage.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-this-secret-key")

# When running behind HTTPS, cookies must be Secure + SameSite=None so the
# kiosk app (a different origin) can include them in cross-origin requests.
if os.environ.get("HTTPS") == "1":
    app.config["SESSION_COOKIE_SECURE"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "None"
    app.config["SESSION_COOKIE_HTTPONLY"] = True

# Allow kiosk app (running as a local file or different origin) to use the API
CORS(app, resources={r"/api/*": {"origins": "*"}, r"/play/*": {"origins": "*"}},
     supports_credentials=True)
db = SQLAlchemy(app)
WINNING_SCORE = 121
SKUNK_THRESHOLD = 90
DOUBLE_SKUNK_THRESHOLD = 60
MIN_GAMES_FOR_CHAMPION = 3


class Player(db.Model):
    __tablename__ = "players"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def __repr__(self) -> str:
        return f"<Player {self.name}>"


class Game(db.Model):
    __tablename__ = "games"
    __table_args__ = (
        CheckConstraint("player1_id != player2_id", name="ck_unique_players"),
        CheckConstraint("player1_score >= 0", name="ck_player1_score_nonnegative"),
        CheckConstraint("player2_score >= 0", name="ck_player2_score_nonnegative"),
    )

    id = db.Column(db.Integer, primary_key=True)
    played_on = db.Column(db.Date, nullable=False)
    player1_id = db.Column(db.Integer, db.ForeignKey("players.id"), nullable=False)
    player2_id = db.Column(db.Integer, db.ForeignKey("players.id"), nullable=False)
    player1_score = db.Column(db.Integer, nullable=False)
    player2_score = db.Column(db.Integer, nullable=False)
    winner_id = db.Column(db.Integer, db.ForeignKey("players.id"), nullable=True)
    first_crib_id = db.Column(db.Integer, db.ForeignKey("players.id"), nullable=True)
    notes = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    player1 = db.relationship("Player", foreign_keys=[player1_id])
    player2 = db.relationship("Player", foreign_keys=[player2_id])
    winner = db.relationship("Player", foreign_keys=[winner_id])
    first_crib = db.relationship("Player", foreign_keys=[first_crib_id])

    def __repr__(self) -> str:
        return f"<Game {self.id} {self.played_on}>"

    @property
    def winner_score(self) -> int:
        if self.winner_id == self.player1_id:
            return self.player1_score
        if self.winner_id == self.player2_id:
            return self.player2_score
        return max(self.player1_score, self.player2_score)

    @property
    def loser_score(self) -> int:
        if self.winner_id == self.player1_id:
            return self.player2_score
        if self.winner_id == self.player2_id:
            return self.player1_score
        return min(self.player1_score, self.player2_score)

    @property
    def point_diff(self) -> int:
        return self.winner_score - self.loser_score

    @property
    def is_double_skunk(self) -> bool:
        return self.loser_score <= DOUBLE_SKUNK_THRESHOLD

    @property
    def is_skunk(self) -> bool:
        return DOUBLE_SKUNK_THRESHOLD < self.loser_score <= SKUNK_THRESHOLD


class LiveGame(db.Model):
    __tablename__ = "live_games"
    id = db.Column(db.Integer, primary_key=True)
    player1_id = db.Column(db.Integer, db.ForeignKey("players.id"), nullable=False)
    player2_id = db.Column(db.Integer, db.ForeignKey("players.id"), nullable=True)
    player1_score = db.Column(db.Integer, default=0)
    player2_score = db.Column(db.Integer, default=0)
    phase = db.Column(db.String(20), default="lobby")
    dealer_id = db.Column(db.Integer, db.ForeignKey("players.id"), nullable=True)
    first_dealer_id = db.Column(db.Integer, db.ForeignKey("players.id"), nullable=True)
    state_json = db.Column(db.Text, default="{}")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime, nullable=True)
    winner_id = db.Column(db.Integer, db.ForeignKey("players.id"), nullable=True)

    player1 = db.relationship("Player", foreign_keys=[player1_id])
    player2 = db.relationship("Player", foreign_keys=[player2_id])
    dealer = db.relationship("Player", foreign_keys=[dealer_id])
    winner = db.relationship("Player", foreign_keys=[winner_id])

    def get_state(self) -> dict:
        return json.loads(self.state_json or "{}")

    def set_state(self, state: dict) -> None:
        self.state_json = json.dumps(state)

    def other_player_id(self, pid: int) -> int | None:
        if pid == self.player1_id:
            return self.player2_id
        if pid == self.player2_id:
            return self.player1_id
        return None

    def score_for(self, pid: int) -> int:
        if pid == self.player1_id:
            return self.player1_score
        return self.player2_score

    def add_score(self, pid: int, pts: int) -> None:
        if pid == self.player1_id:
            self.player1_score = min(self.player1_score + pts, 121)
        else:
            self.player2_score = min(self.player2_score + pts, 121)


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    player_id = db.Column(db.Integer, db.ForeignKey("players.id"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    player = db.relationship("Player", foreign_keys=[player_id])

    def __repr__(self) -> str:
        return f"<User {self.username}>"


class InviteCode(db.Model):
    __tablename__ = "invite_codes"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(32), unique=True, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    used_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    used_at = db.Column(db.DateTime, nullable=True)

    created_by = db.relationship("User", foreign_keys=[created_by_id])
    used_by = db.relationship("User", foreign_keys=[used_by_id])

    @property
    def is_used(self) -> bool:
        return self.used_by_id is not None


@dataclass
class LeaderboardRow:
    player: Player
    games: int
    wins: int
    losses: int
    draws: int
    points_for: int
    points_against: int
    point_diff: int
    skunks: int
    double_skunks: int
    first_cribs: int
    win_rate: float
    avg_margin: float
    participation: float
    champion_score: float


def current_user() -> "User | None":
    uid = session.get("user_id")
    if uid:
        return User.query.get(uid)
    return None


def is_admin() -> bool:
    u = current_user()
    return bool(u and u.is_admin)


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            flash("Please log in to access that page.", "error")
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        u = current_user()
        if not u:
            flash("Please log in.", "error")
            return redirect(url_for("login"))
        if not u.is_admin:
            flash("Admin access required.", "error")
            return redirect(url_for("index"))
        return view(*args, **kwargs)

    return wrapped


@app.context_processor
def inject_auth_context():
    u = current_user()
    return {"is_admin": bool(u and u.is_admin), "current_user": u}


def calculate_champion_scores(year: int) -> tuple[list[LeaderboardRow], LeaderboardRow | None]:
    year_games = Game.query.filter(
        Game.played_on >= date(year, 1, 1), Game.played_on <= date(year, 12, 31)
    ).all()
    players = Player.query.order_by(Player.name.asc()).all()
    if not players:
        return [], None

    games_by_player: dict[int, list[Game]] = {player.id: [] for player in players}
    for game in year_games:
        games_by_player[game.player1_id].append(game)
        games_by_player[game.player2_id].append(game)

    max_games_played = max((len(g) for g in games_by_player.values()), default=0)
    raw_rows: list[LeaderboardRow] = []

    for player in players:
        games = games_by_player[player.id]
        wins = 0
        losses = 0
        draws = 0
        points_for = 0
        points_against = 0
        skunks = 0
        double_skunks = 0
        first_cribs = 0
        margins: list[int] = []

        for game in games:
            if game.player1_id == player.id:
                pf = game.player1_score
                pa = game.player2_score
            else:
                pf = game.player2_score
                pa = game.player1_score

            points_for += pf
            points_against += pa
            margin = pf - pa
            margins.append(margin)

            if game.winner_id is None:
                draws += 1
            elif game.winner_id == player.id:
                wins += 1
                if game.is_skunk:
                    skunks += 1
                if game.is_double_skunk:
                    double_skunks += 1
            else:
                losses += 1

            if game.first_crib_id == player.id:
                first_cribs += 1

        games_played = len(games)
        decisive_games = wins + losses
        win_rate = wins / decisive_games if decisive_games else 0.0
        avg_margin = sum(margins) / games_played if games_played else 0.0
        participation = games_played / max_games_played if max_games_played else 0.0

        raw_rows.append(
            LeaderboardRow(
                player=player,
                games=games_played,
                wins=wins,
                losses=losses,
                draws=draws,
                points_for=points_for,
                points_against=points_against,
                point_diff=points_for - points_against,
                skunks=skunks,
                double_skunks=double_skunks,
                first_cribs=first_cribs,
                win_rate=win_rate,
                avg_margin=avg_margin,
                participation=participation,
                champion_score=0.0,
            )
        )

    eligible_rows = [row for row in raw_rows if row.games >= MIN_GAMES_FOR_CHAMPION]
    min_margin = min((row.avg_margin for row in eligible_rows), default=0.0)
    max_margin = max((row.avg_margin for row in eligible_rows), default=0.0)
    margin_span = max_margin - min_margin

    for row in raw_rows:
        if row.games < MIN_GAMES_FOR_CHAMPION:
            row.champion_score = 0.0
            continue

        margin_component = (
            (row.avg_margin - min_margin) / margin_span if margin_span > 0 else 0.5
        )
        # Family Champion Model:
        # 60% win rate, 25% scoring margin quality, 15% participation.
        # Minimum 3 games required to be eligible.
        row.champion_score = 100 * (
            (0.60 * row.win_rate) + (0.25 * margin_component) + (0.15 * row.participation)
        )

    ranked = sorted(
        raw_rows,
        key=lambda x: (x.champion_score, x.wins, x.point_diff, x.points_for),
        reverse=True,
    )
    winner = ranked[0] if ranked and ranked[0].games >= MIN_GAMES_FOR_CHAMPION else None
    return ranked, winner


@app.route("/")
def index():
    current_year = date.today().year
    recent_games = Game.query.order_by(Game.played_on.desc(), Game.created_at.desc()).limit(10).all()
    ranked, winner = calculate_champion_scores(current_year)
    return render_template(
        "index.html",
        current_year=current_year,
        recent_games=recent_games,
        ranked=ranked[:5],
        winner=winner,
    )


@app.route("/players")
def players():
    all_players = Player.query.order_by(Player.name.asc()).all()
    return render_template("players.html", players=all_players)


@app.route("/games", methods=["GET", "POST"])
@login_required
def games():
    all_players = Player.query.order_by(Player.name.asc()).all()
    if request.method == "POST":
        played_on_raw = request.form.get("played_on", "")
        winner_id = request.form.get("winner_id", type=int)
        loser_id = request.form.get("loser_id", type=int)
        loser_score = request.form.get("loser_score", type=int)
        first_crib_id = request.form.get("first_crib_id", type=int)
        notes = request.form.get("notes", "").strip() or None

        if not played_on_raw:
            flash("Game date is required.", "error")
            return redirect(url_for("games"))

        try:
            played_on = datetime.strptime(played_on_raw, "%Y-%m-%d").date()
        except ValueError:
            flash("Invalid date format.", "error")
            return redirect(url_for("games"))

        if winner_id is None or loser_id is None or winner_id == loser_id:
            flash("Choose a winner and a different opponent.", "error")
            return redirect(url_for("games"))

        winner_player = Player.query.get(winner_id)
        loser_player = Player.query.get(loser_id)
        if winner_player is None or loser_player is None:
            flash("Selected players were not found.", "error")
            return redirect(url_for("games"))

        if loser_score is None or loser_score < 0 or loser_score >= WINNING_SCORE:
            flash(f"Losing score must be between 0 and {WINNING_SCORE - 1}.", "error")
            return redirect(url_for("games"))

        if first_crib_id is None or first_crib_id not in {winner_id, loser_id}:
            flash("First crib must match one of the two players in the game.", "error")
            return redirect(url_for("games"))

        db.session.add(
            Game(
                played_on=played_on,
                player1_id=winner_id,
                player2_id=loser_id,
                player1_score=WINNING_SCORE,
                player2_score=loser_score,
                winner_id=winner_id,
                first_crib_id=first_crib_id,
                notes=notes,
            )
        )
        db.session.commit()
        flash("Game saved.", "success")
        return redirect(url_for("games"))

    recent_games = Game.query.order_by(Game.played_on.desc(), Game.created_at.desc()).limit(25).all()
    return render_template(
        "games.html",
        players=all_players,
        recent_games=recent_games,
        today_iso=date.today().isoformat(),
    )


def _compute_champion_scores_from_games(
    players: list[Player], games: list[Game]
) -> dict[int, float]:
    """Compute champion scores from an explicit list of games (no DB query)."""
    if not players or not games:
        return {p.id: 0.0 for p in players}

    games_by_player: dict[int, list[Game]] = {p.id: [] for p in players}
    for game in games:
        if game.player1_id in games_by_player:
            games_by_player[game.player1_id].append(game)
        if game.player2_id in games_by_player:
            games_by_player[game.player2_id].append(game)

    max_games = max((len(g) for g in games_by_player.values()), default=0)
    if max_games == 0:
        return {p.id: 0.0 for p in players}

    raw: list[tuple[int, float, float, float, int]] = []
    for p in players:
        pg = games_by_player[p.id]
        wins = losses = 0
        margins: list[float] = []
        for g in pg:
            pf = g.player1_score if g.player1_id == p.id else g.player2_score
            pa = g.player2_score if g.player1_id == p.id else g.player1_score
            margins.append(float(pf - pa))
            if g.winner_id == p.id:
                wins += 1
            elif g.winner_id is not None:
                losses += 1
        decisive = wins + losses
        raw.append((
            p.id,
            wins / decisive if decisive else 0.0,
            sum(margins) / len(margins) if margins else 0.0,
            len(pg) / max_games,
            len(pg),
        ))

    eligible = [(pid, wr, am, part, cnt) for pid, wr, am, part, cnt in raw if cnt >= MIN_GAMES_FOR_CHAMPION]
    if not eligible:
        return {p.id: 0.0 for p in players}

    min_m = min(am for _, _, am, _, _ in eligible)
    max_m = max(am for _, _, am, _, _ in eligible)
    span = max_m - min_m

    result: dict[int, float] = {}
    for pid, wr, am, part, cnt in raw:
        if cnt < MIN_GAMES_FOR_CHAMPION:
            result[pid] = 0.0
        else:
            mc = (am - min_m) / span if span > 0 else 0.5
            result[pid] = round(100 * (0.60 * wr + 0.25 * mc + 0.15 * part), 2)
    return result


@app.route("/stats")
def stats():
    year = request.args.get("year", type=int) or date.today().year
    year_games = (
        Game.query
        .filter(Game.played_on >= date(year, 1, 1), Game.played_on <= date(year, 12, 31))
        .order_by(Game.played_on, Game.created_at)
        .all()
    )
    all_players = Player.query.order_by(Player.name.asc()).all()
    player_map = {p.id: p for p in all_players}

    # ── games per month ────────────────────────────────────────────────
    games_per_month = [0] * 12
    for g in year_games:
        games_per_month[g.played_on.month - 1] += 1

    # ── champion score over time (one snapshot per unique date) ────────
    unique_dates = sorted(set(g.played_on for g in year_games))
    champion_dates: list[str] = []
    champion_series: dict[str, list[float]] = {p.name: [] for p in all_players}
    for snap_date in unique_dates:
        subset = [g for g in year_games if g.played_on <= snap_date]
        scores = _compute_champion_scores_from_games(all_players, subset)
        champion_dates.append(snap_date.strftime("%b %d"))
        for p in all_players:
            champion_series[p.name].append(scores[p.id])

    # ── win quality breakdown ──────────────────────────────────────────
    standard_wins: dict[int, int] = {p.id: 0 for p in all_players}
    skunk_wins: dict[int, int] = {p.id: 0 for p in all_players}
    double_skunk_wins: dict[int, int] = {p.id: 0 for p in all_players}
    losses_count: dict[int, int] = {p.id: 0 for p in all_players}
    for g in year_games:
        if g.winner_id:
            loser_id = g.player2_id if g.winner_id == g.player1_id else g.player1_id
            if g.is_double_skunk:
                double_skunk_wins[g.winner_id] += 1
            elif g.is_skunk:
                skunk_wins[g.winner_id] += 1
            else:
                standard_wins[g.winner_id] += 1
            if loser_id in losses_count:
                losses_count[loser_id] += 1

    # ── head-to-head ───────────────────────────────────────────────────
    pid_to_idx = {p.id: i for i, p in enumerate(all_players)}
    h2h_wins_raw = [[0] * len(all_players) for _ in all_players]
    h2h_totals_raw = [[0] * len(all_players) for _ in all_players]
    for g in year_games:
        i = pid_to_idx.get(g.player1_id)
        j = pid_to_idx.get(g.player2_id)
        if i is None or j is None:
            continue
        h2h_totals_raw[i][j] += 1
        h2h_totals_raw[j][i] += 1
        if g.winner_id == g.player1_id:
            h2h_wins_raw[i][j] += 1
        elif g.winner_id == g.player2_id:
            h2h_wins_raw[j][i] += 1

    h2h_table = []
    for i, row_player in enumerate(all_players):
        cells = []
        for j, col_player in enumerate(all_players):
            if i == j:
                cells.append({"self": True})
            else:
                w = h2h_wins_raw[i][j]
                t = h2h_totals_raw[i][j]
                cells.append({"self": False, "wins": w, "losses": t - w, "total": t})
        h2h_table.append({"player": row_player.name, "cells": cells})

    # ── first crib advantage ───────────────────────────────────────────
    fc_games = [g for g in year_games if g.first_crib_id is not None and g.winner_id is not None]
    first_crib_won = sum(1 for g in fc_games if g.first_crib_id == g.winner_id)
    first_crib_pct = round(100 * first_crib_won / len(fc_games), 1) if fc_games else None

    # ── fun facts ──────────────────────────────────────────────────────
    games_per_player: dict[int, int] = {p.id: 0 for p in all_players}
    for g in year_games:
        games_per_player[g.player1_id] = games_per_player.get(g.player1_id, 0) + 1
        games_per_player[g.player2_id] = games_per_player.get(g.player2_id, 0) + 1

    most_active_id = max(games_per_player, key=games_per_player.get) if year_games else None
    most_active_name = (
        player_map[most_active_id].name
        if most_active_id and games_per_player[most_active_id] > 0
        else None
    )
    most_active_count = games_per_player.get(most_active_id, 0) if most_active_id else 0

    biggest_win = min(year_games, key=lambda g: g.loser_score) if year_games else None
    closest_game = max(year_games, key=lambda g: g.loser_score) if year_games else None

    total_skunks_per_player = {p.id: skunk_wins[p.id] + double_skunk_wins[p.id] for p in all_players}
    top_skunk_id = max(total_skunks_per_player, key=total_skunks_per_player.get) if all_players else None
    top_skunk = (
        player_map[top_skunk_id].name,
        total_skunks_per_player[top_skunk_id],
    ) if top_skunk_id and total_skunks_per_player[top_skunk_id] > 0 else None

    # current win streak per player (most recent games first)
    streaks: dict[int, int] = {}
    for p in all_players:
        pg = sorted(
            [g for g in year_games if g.player1_id == p.id or g.player2_id == p.id],
            key=lambda g: (g.played_on, g.created_at),
            reverse=True,
        )
        streak = 0
        for g in pg:
            if g.winner_id == p.id:
                streak += 1
            else:
                break
        streaks[p.id] = streak

    best_streak_id = max(streaks, key=streaks.get) if streaks else None
    best_streak = (
        player_map[best_streak_id].name,
        streaks[best_streak_id],
    ) if best_streak_id and streaks.get(best_streak_id, 0) > 1 else None

    _, champion_leader = calculate_champion_scores(year)

    active_players = [p for p in all_players if games_per_player.get(p.id, 0) > 0]
    active_names = [p.name for p in active_players]

    return render_template(
        "stats.html",
        year=year,
        total_games=len(year_games),
        all_player_names=[p.name for p in all_players],
        active_player_names=active_names,
        games_per_month=games_per_month,
        champion_dates=champion_dates,
        champion_series={p.name: champion_series[p.name] for p in active_players},
        win_standard=[standard_wins[p.id] for p in active_players],
        win_skunks=[skunk_wins[p.id] for p in active_players],
        win_double_skunks=[double_skunk_wins[p.id] for p in active_players],
        losses=[losses_count[p.id] for p in active_players],
        h2h_table=h2h_table,
        first_crib_pct=first_crib_pct,
        first_crib_won=first_crib_won,
        first_crib_total=len(fc_games),
        most_active_name=most_active_name,
        most_active_count=most_active_count,
        biggest_win=biggest_win,
        closest_game=closest_game,
        top_skunk=top_skunk,
        best_streak=best_streak,
        champion_leader=champion_leader,
    )


@app.route("/leaderboard")
def leaderboard():
    year = request.args.get("year", type=int) or date.today().year
    ranked, winner = calculate_champion_scores(year)
    return render_template("leaderboard.html", year=year, ranked=ranked, winner=winner)


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user():
        return redirect(url_for("index"))
    invite_code_param = request.args.get("code", "")
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        code_str = request.form.get("invite_code", "").strip()
        if not username or not password:
            flash("Username and password are required.", "error")
            return redirect(url_for("register", code=code_str))
        if password != confirm:
            flash("Passwords do not match.", "error")
            return redirect(url_for("register", code=code_str))
        if User.query.filter_by(username=username).first():
            flash("Username already taken.", "error")
            return redirect(url_for("register", code=code_str))
        invite = InviteCode.query.filter_by(code=code_str).first()
        if not invite or invite.is_used:
            flash("Invalid or already-used invite code.", "error")
            return redirect(url_for("register", code=code_str))
        user = User(
            username=username,
            password_hash=generate_password_hash(password, method="pbkdf2:sha256"),
        )
        db.session.add(user)
        db.session.flush()  # get user.id before commit
        invite.used_by_id = user.id
        invite.used_at = datetime.utcnow()
        db.session.commit()
        session["user_id"] = user.id
        flash(f"Welcome, {username}!", "success")
        return redirect(url_for("index"))
    return render_template("register.html", invite_code=invite_code_param)


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user():
        return redirect(url_for("index"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password_hash, password):
            session["user_id"] = user.id
            flash(f"Welcome back, {user.username}!", "success")
            return redirect(url_for("index"))
        flash("Invalid username or password.", "error")
        return redirect(url_for("login"))
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.pop("user_id", None)
    flash("Logged out.", "success")
    return redirect(url_for("index"))


@app.route("/admin")
@admin_required
def admin_panel():
    all_players = Player.query.order_by(Player.name.asc()).all()
    recent_games = Game.query.order_by(Game.played_on.desc(), Game.created_at.desc()).limit(50).all()
    all_users = User.query.order_by(User.username.asc()).all()
    invites = InviteCode.query.order_by(InviteCode.created_at.desc()).limit(50).all()
    return render_template("admin.html", players=all_players, recent_games=recent_games, users=all_users, invites=invites)


@app.route("/admin/invites/generate", methods=["POST"])
@admin_required
def admin_generate_invite():
    code = secrets.token_urlsafe(12)
    invite = InviteCode(code=code, created_by_id=current_user().id)
    db.session.add(invite)
    db.session.commit()
    flash(f"Invite code generated: {code}", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/players/add", methods=["POST"])
@admin_required
def admin_add_player():
    name = request.form.get("name", "").strip()
    if not name:
        flash("Player name is required.", "error")
        return redirect(url_for("admin_panel"))

    existing = Player.query.filter_by(name=name).first()
    if existing:
        flash("That player already exists.", "error")
        return redirect(url_for("admin_panel"))

    db.session.add(Player(name=name))
    db.session.commit()
    flash(f"Added player: {name}", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/players/<int:player_id>/delete", methods=["POST"])
@admin_required
def admin_delete_player(player_id: int):
    player = Player.query.get_or_404(player_id)
    related_games = Game.query.filter(
        (Game.player1_id == player_id)
        | (Game.player2_id == player_id)
        | (Game.winner_id == player_id)
        | (Game.first_crib_id == player_id)
    ).count()
    if related_games > 0:
        flash(
            f"Cannot delete {player.name}: remove related games first ({related_games} found).",
            "error",
        )
        return redirect(url_for("admin_panel"))

    db.session.delete(player)
    db.session.commit()
    flash(f"Deleted player: {player.name}", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/games/<int:game_id>/delete", methods=["POST"])
@admin_required
def admin_delete_game(game_id: int):
    game = Game.query.get_or_404(game_id)
    db.session.delete(game)
    db.session.commit()
    flash("Game deleted.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/users/<int:user_id>/promote", methods=["POST"])
@admin_required
def admin_promote_user(user_id: int):
    user = User.query.get_or_404(user_id)
    user.is_admin = True
    db.session.commit()
    flash(f"{user.username} is now an admin.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/users/<int:user_id>/demote", methods=["POST"])
@admin_required
def admin_demote_user(user_id: int):
    user = User.query.get_or_404(user_id)
    if user.id == current_user().id:
        flash("You can't demote yourself.", "error")
        return redirect(url_for("admin_panel"))
    user.is_admin = False
    db.session.commit()
    flash(f"{user.username} is no longer an admin.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/users/<int:user_id>/link-player", methods=["POST"])
@admin_required
def admin_link_player(user_id: int):
    user = User.query.get_or_404(user_id)
    player_id = request.form.get("player_id", type=int)
    user.player_id = player_id if player_id else None
    db.session.commit()
    flash(f"Updated player link for {user.username}.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def admin_delete_user(user_id: int):
    user = User.query.get_or_404(user_id)
    if user.id == current_user().id:
        flash("You can't delete yourself.", "error")
        return redirect(url_for("admin_panel"))
    db.session.delete(user)
    db.session.commit()
    flash(f"Deleted user {user.username}.", "success")
    return redirect(url_for("admin_panel"))


# ── Live game helpers ─────────────────────────────────────────────────────────

def _live_player_id() -> int | None:
    u = current_user()
    if u and u.player_id:
        return u.player_id
    return session.get("live_player_id")


def _check_win(lg: LiveGame) -> bool:
    """Return True if game just ended; sets phase/winner/completed_at and saves Game record."""
    winner_id = None
    if lg.player1_score >= WINNING_SCORE:
        winner_id = lg.player1_id
    elif lg.player2_score >= WINNING_SCORE:
        winner_id = lg.player2_id
    if winner_id is None:
        return False

    loser_id = lg.player2_id if winner_id == lg.player1_id else lg.player1_id
    lg.phase = "complete"
    lg.winner_id = winner_id
    lg.completed_at = datetime.utcnow()

    # Save to historical Game table
    if winner_id == lg.player1_id:
        w_score, l_score = lg.player1_score, lg.player2_score
    else:
        w_score, l_score = lg.player2_score, lg.player1_score

    db.session.add(Game(
        played_on=date.today(),
        player1_id=winner_id,
        player2_id=loser_id,
        player1_score=w_score,
        player2_score=l_score,
        winner_id=winner_id,
        first_crib_id=lg.first_dealer_id,
        notes="Live game",
    ))
    return True


def _advance_counting(lg: LiveGame) -> None:
    """Score the current counting subphase and advance. Calls _check_win."""
    state = lg.get_state()
    subphase = state.get("counting_subphase", "p2_hand")

    peg = state.get("pegging") or {}

    if subphase == "p2_hand":
        hand = peg.get("p2_played", state.get("p2_hand", []))
        pid = lg.player2_id
        next_sub = "p1_hand"
    elif subphase == "p1_hand":
        hand = peg.get("p1_played", state.get("p1_hand", []))
        pid = lg.player1_id
        next_sub = "crib"
    else:  # crib
        hand = state["crib"]
        pid = lg.dealer_id
        next_sub = None

    starter = state["starter"]
    pts, reasons = score_hand(hand, starter, is_crib=(subphase == "crib"))
    lg.add_score(pid, pts)

    p = Player.query.get(pid)
    pname = p.name if p else "?"
    reason_str = ", ".join(reasons) if reasons else "nothing"
    events = state.get("events", [])
    events.append(f"{pname} counts {'crib' if subphase == 'crib' else 'hand'}: {reason_str} = {pts} pts")
    events = events[-10:]
    state["events"] = events

    if _check_win(lg):
        lg.set_state(state)
        return

    if next_sub is None:
        # Done counting — start next hand or end
        _start_next_hand(lg, state)
    else:
        state["counting_subphase"] = next_sub
        lg.set_state(state)
        lg.phase = "counting"


def _start_next_hand(lg: LiveGame, state: dict) -> None:
    """Rotate dealer and deal new hand."""
    # Rotate dealer
    if lg.dealer_id == lg.player1_id:
        lg.dealer_id = lg.player2_id
    else:
        lg.dealer_id = lg.player1_id

    deck = new_deck()
    p1_hand, p2_hand, remaining = deal_hands(deck)
    events = state.get("events", [])
    new_state = {
        "p1_hand": p1_hand,
        "p2_hand": p2_hand,
        "crib": [],
        "starter": None,
        "p1_discarded": False,
        "p2_discarded": False,
        "deck": remaining,
        "pegging": None,
        "counting_subphase": None,
        "events": events,
    }
    lg.set_state(new_state)
    lg.phase = "discarding"


# ── Live game routes ──────────────────────────────────────────────────────────

@app.route("/play")
def play_lobby():
    all_players = Player.query.order_by(Player.name.asc()).all()
    me_id = _live_player_id()
    open_games = LiveGame.query.filter_by(player2_id=None, phase="lobby").order_by(LiveGame.created_at.desc()).all()
    my_games = []
    if me_id:
        my_games = LiveGame.query.filter(
            ((LiveGame.player1_id == me_id) | (LiveGame.player2_id == me_id)),
            LiveGame.phase != "complete",
        ).order_by(LiveGame.created_at.desc()).all()
    return render_template("play_lobby.html", players=all_players, me_id=me_id,
                           open_games=open_games, my_games=my_games)


@app.route("/play/set-identity", methods=["POST"])
def play_set_identity():
    pid = request.form.get("player_id", type=int)
    if pid and Player.query.get(pid):
        session["live_player_id"] = pid
    return redirect(url_for("play_lobby"))


@app.route("/play/new", methods=["POST"])
@login_required
def play_new():
    me_id = _live_player_id()
    if not me_id:
        flash("Set your identity first.", "error")
        return redirect(url_for("play_lobby"))
    lg = LiveGame(player1_id=me_id, phase="lobby")
    db.session.add(lg)
    db.session.commit()
    return redirect(url_for("play_game", game_id=lg.id))


@app.route("/play/<int:game_id>/join", methods=["POST"])
@login_required
def play_join(game_id: int):
    me_id = _live_player_id()
    if not me_id:
        flash("Set your identity first.", "error")
        return redirect(url_for("play_lobby"))
    lg = LiveGame.query.get_or_404(game_id)
    if lg.player2_id is not None:
        flash("Game already has two players.", "error")
        return redirect(url_for("play_lobby"))
    if lg.player1_id == me_id:
        flash("You can't join your own game as opponent.", "error")
        return redirect(url_for("play_lobby"))

    lg.player2_id = me_id
    # Randomly assign first dealer
    lg.dealer_id = random.choice([lg.player1_id, lg.player2_id])
    lg.first_dealer_id = lg.dealer_id

    deck = new_deck()
    p1_hand, p2_hand, remaining = deal_hands(deck)
    state = {
        "p1_hand": p1_hand,
        "p2_hand": p2_hand,
        "crib": [],
        "starter": None,
        "p1_discarded": False,
        "p2_discarded": False,
        "deck": remaining,
        "pegging": None,
        "counting_subphase": None,
        "events": ["Game started! Discard 2 cards to the crib."],
    }
    lg.set_state(state)
    lg.phase = "discarding"
    db.session.commit()
    return redirect(url_for("play_game", game_id=lg.id))


@app.route("/play/<int:game_id>")
def play_game(game_id: int):
    lg = LiveGame.query.get_or_404(game_id)
    me_id = _live_player_id()
    all_players = Player.query.order_by(Player.name.asc()).all()
    return render_template("play_game.html", game=lg, me_id=me_id, players=all_players)


@app.route("/play/<int:game_id>/state")
def play_state(game_id: int):
    lg = LiveGame.query.get_or_404(game_id)
    me_id = _live_player_id()
    state = lg.get_state()

    # Determine role
    my_role = None
    if me_id == lg.player1_id:
        my_role = "p1"
    elif me_id == lg.player2_id:
        my_role = "p2"

    my_hand = []
    opp_hand_count = 0
    if my_role == "p1":
        my_hand = state.get("p1_hand", [])
        opp_hand_count = len(state.get("p2_hand", []))
    elif my_role == "p2":
        my_hand = state.get("p2_hand", [])
        opp_hand_count = len(state.get("p1_hand", []))

    pegging = state.get("pegging") or {}
    my_played = pegging.get("p1_played", []) if my_role == "p1" else pegging.get("p2_played", [])

    p1 = Player.query.get(lg.player1_id)
    p2 = Player.query.get(lg.player2_id) if lg.player2_id else None
    dealer = Player.query.get(lg.dealer_id) if lg.dealer_id else None

    return jsonify({
        "phase": lg.phase,
        "my_role": my_role,
        "player1_name": p1.name if p1 else "",
        "player2_name": p2.name if p2 else "Waiting...",
        "player1_score": lg.player1_score,
        "player2_score": lg.player2_score,
        "player1_id": lg.player1_id,
        "player2_id": lg.player2_id,
        "dealer_id": lg.dealer_id,
        "dealer_name": dealer.name if dealer else "",
        "my_hand": my_hand,
        "my_played": my_played,
        "opp_hand_count": opp_hand_count,
        "starter": state.get("starter"),
        "crib_count": len(state.get("crib", [])),
        "pegging": pegging,
        "counting_subphase": state.get("counting_subphase"),
        "counting_p1_hand": pegging.get("p1_played", []),
        "counting_p2_hand": pegging.get("p2_played", []),
        "counting_crib": state.get("crib", []),
        "events": state.get("events", [])[-5:],
        "p1_discarded": state.get("p1_discarded", False),
        "p2_discarded": state.get("p2_discarded", False),
        "winner_id": lg.winner_id,
    })


@app.route("/play/<int:game_id>/discard", methods=["POST"])
@login_required
def play_discard(game_id: int):
    lg = LiveGame.query.get_or_404(game_id)
    me_id = _live_player_id()
    if lg.phase != "discarding":
        return jsonify({"error": "Wrong phase"}), 400

    my_role = "p1" if me_id == lg.player1_id else ("p2" if me_id == lg.player2_id else None)
    if not my_role:
        return jsonify({"error": "Not a player"}), 403

    state = lg.get_state()
    discarded_key = f"{my_role}_discarded"
    if state.get(discarded_key):
        return jsonify({"error": "Already discarded"}), 400

    indices = request.json.get("indices", []) if request.is_json else []
    if len(indices) != 2:
        return jsonify({"error": "Must discard exactly 2 cards"}), 400

    hand_key = f"{my_role}_hand"
    hand = state[hand_key]
    try:
        indices = sorted([int(i) for i in indices], reverse=True)
        discarded = []
        for i in indices:
            discarded.append(hand.pop(i))
    except (IndexError, ValueError):
        return jsonify({"error": "Invalid card indices"}), 400

    state["crib"].extend(discarded)
    state[discarded_key] = True

    events = state.get("events", [])
    p = Player.query.get(me_id)
    events.append(f"{p.name if p else '?'} discarded to crib.")
    state["events"] = events[-10:]

    # If both discarded, move to cutting
    if state["p1_discarded"] and state["p2_discarded"]:
        lg.phase = "cutting"

    lg.set_state(state)
    db.session.commit()
    return jsonify({"ok": True, "phase": lg.phase})


@app.route("/play/<int:game_id>/cut", methods=["POST"])
@login_required
def play_cut(game_id: int):
    lg = LiveGame.query.get_or_404(game_id)
    if lg.phase != "cutting":
        return jsonify({"error": "Wrong phase"}), 400

    state = lg.get_state()
    deck = state["deck"]
    starter = deck.pop(random.randrange(len(deck)))
    state["starter"] = starter
    state["deck"] = deck

    events = state.get("events", [])
    pts = 0
    if _rank(starter) == "J":
        # His heels — dealer gets 2
        lg.add_score(lg.dealer_id, 2)
        pts = 2
        events.append(f"His heels! {card_display(starter)} — dealer gets 2 pts")
        if _check_win(lg):
            lg.set_state(state)
            db.session.commit()
            return jsonify({"ok": True, "starter": starter, "phase": lg.phase})
    else:
        events.append(f"Starter card: {card_display(starter)}")

    # Set up pegging state
    non_dealer = lg.player2_id if lg.dealer_id == lg.player1_id else lg.player1_id
    state["pegging"] = {
        "pile": [],
        "count": 0,
        "turn_player_id": non_dealer,
        "p1_go": False,
        "p2_go": False,
        "p1_played": [],
        "p2_played": [],
    }
    state["events"] = events[-10:]
    lg.set_state(state)
    lg.phase = "pegging"
    db.session.commit()
    return jsonify({"ok": True, "starter": starter, "phase": lg.phase})


@app.route("/play/<int:game_id>/peg", methods=["POST"])
@login_required
def play_peg(game_id: int):
    lg = LiveGame.query.get_or_404(game_id)
    me_id = _live_player_id()
    if lg.phase != "pegging":
        return jsonify({"error": "Wrong phase"}), 400

    state = lg.get_state()
    peg = state["pegging"]

    if peg["turn_player_id"] != me_id:
        return jsonify({"error": "Not your turn"}), 400

    my_role = "p1" if me_id == lg.player1_id else "p2"
    hand_key = f"{my_role}_hand"
    played_key = f"{my_role}_played"

    card = request.json.get("card") if request.is_json else None
    if not card or card not in state[hand_key]:
        return jsonify({"error": "Invalid card"}), 400
    if card_rank_value(card) + peg["count"] > 31:
        return jsonify({"error": "Card would exceed 31"}), 400

    # Play the card
    state[hand_key].remove(card)
    peg[played_key].append(card)
    peg["pile"].append(card)
    peg["count"] += card_rank_value(card)

    pts, reasons = score_pegging_play(peg["pile"][:-1], card)
    events = state.get("events", [])
    p = Player.query.get(me_id)
    pname = p.name if p else "?"
    reason_str = f" ({', '.join(reasons)})" if reasons else ""
    events.append(f"{pname} played {card_display(card)}{reason_str} — count: {peg['count']}")

    if pts:
        lg.add_score(me_id, pts)
        if _check_win(lg):
            state["events"] = events[-10:]
            lg.set_state(state)
            db.session.commit()
            return jsonify({"ok": True, "phase": lg.phase})

    # Reset go flags since a card was played
    peg["p1_go"] = False
    peg["p2_go"] = False

    # Check for 31
    if peg["count"] == 31:
        peg["pile"] = []
        peg["count"] = 0
        events.append("31! Pile reset.")

    # Check if all cards played
    p1_done = len(state["p1_hand"]) == 0 and len(peg["p1_played"]) == 4
    p2_done = len(state["p2_hand"]) == 0 and len(peg["p2_played"]) == 4

    # Actually check remaining hands
    p1_remaining = state["p1_hand"]
    p2_remaining = state["p2_hand"]

    if not p1_remaining and not p2_remaining:
        # Last card point
        lg.add_score(me_id, 1)
        events.append(f"{pname} gets last card (+1)")
        state["events"] = events[-10:]
        lg.set_state(state)
        if _check_win(lg):
            db.session.commit()
            return jsonify({"ok": True, "phase": lg.phase})
        # Move to counting
        state["counting_subphase"] = "p2_hand"
        lg.set_state(state)
        lg.phase = "counting"
        _advance_counting(lg)
        db.session.commit()
        return jsonify({"ok": True, "phase": lg.phase})

    # Switch turn to opponent (if they have legal plays)
    opp_id = lg.other_player_id(me_id)
    opp_role = "p1" if opp_id == lg.player1_id else "p2"
    opp_hand = state[f"{opp_role}_hand"]
    legal = legal_pegging_plays(opp_hand, peg["count"])
    if legal:
        peg["turn_player_id"] = opp_id
    else:
        # Check if current player also has no legal plays
        my_legal = legal_pegging_plays(state[hand_key], peg["count"])
        if not my_legal:
            # Both stuck — reset pile, award go point
            lg.add_score(me_id, 1)
            events.append(f"Go! {pname} gets 1 pt. Pile reset.")
            peg["pile"] = []
            peg["count"] = 0
            peg["p1_go"] = False
            peg["p2_go"] = False
            peg["turn_player_id"] = opp_id
        # else keep turn with current player

    state["events"] = events[-10:]
    state["pegging"] = peg
    lg.set_state(state)
    db.session.commit()
    return jsonify({"ok": True, "phase": lg.phase})


@app.route("/play/<int:game_id>/go", methods=["POST"])
@login_required
def play_go(game_id: int):
    lg = LiveGame.query.get_or_404(game_id)
    me_id = _live_player_id()
    if lg.phase != "pegging":
        return jsonify({"error": "Wrong phase"}), 400

    state = lg.get_state()
    peg = state["pegging"]
    if peg["turn_player_id"] != me_id:
        return jsonify({"error": "Not your turn"}), 400

    my_role = "p1" if me_id == lg.player1_id else "p2"
    hand_key = f"{my_role}_hand"
    go_key = f"{my_role}_go"

    # Verify they truly can't play
    legal = legal_pegging_plays(state[hand_key], peg["count"])
    if legal:
        return jsonify({"error": "You have legal plays"}), 400

    peg[go_key] = True
    events = state.get("events", [])
    p = Player.query.get(me_id)
    pname = p.name if p else "?"
    events.append(f"{pname} says Go!")

    opp_id = lg.other_player_id(me_id)
    opp_role = "p1" if opp_id == lg.player1_id else "p2"
    opp_go_key = f"{opp_role}_go"

    if peg[opp_go_key]:
        # Both said go — find last player to actually play and give them 1 pt
        last_player = me_id  # fallback: award to whoever said go second
        if peg["pile"]:
            # We award to whichever player played the last card in the pile
            # Track by checking whose pile the last card is in
            last_card = peg["pile"][-1]
            if last_card in peg.get("p1_played", []):
                last_player = lg.player1_id
            else:
                last_player = lg.player2_id
        lg.add_score(last_player, 1)
        lp = Player.query.get(last_player)
        events.append(f"Go! {lp.name if lp else '?'} gets 1 pt. Pile reset.")
        peg["pile"] = []
        peg["count"] = 0
        peg["p1_go"] = False
        peg["p2_go"] = False
        peg["turn_player_id"] = opp_id
    else:
        # Switch turn to opponent
        peg["turn_player_id"] = opp_id

    state["events"] = events[-10:]
    state["pegging"] = peg
    lg.set_state(state)

    if _check_win(lg):
        db.session.commit()
        return jsonify({"ok": True, "phase": lg.phase})

    db.session.commit()
    return jsonify({"ok": True, "phase": lg.phase})


@app.route("/play/<int:game_id>/delete", methods=["POST"])
@login_required
def play_delete(game_id: int):
    lg = LiveGame.query.get_or_404(game_id)
    me_id = _live_player_id()
    if me_id not in (lg.player1_id, lg.player2_id):
        flash("You can only delete your own games.", "error")
        return redirect(url_for("play_lobby"))
    if lg.phase == "complete":
        flash("Completed games cannot be deleted here.", "error")
        return redirect(url_for("play_lobby"))
    db.session.delete(lg)
    db.session.commit()
    flash("Game deleted.", "success")
    return redirect(url_for("play_lobby"))


@app.route("/play/<int:game_id>/count", methods=["POST"])
@login_required
def play_count(game_id: int):
    """Advance counting by one subphase (called by either player to trigger auto-scoring)."""
    lg = LiveGame.query.get_or_404(game_id)
    me_id = _live_player_id()
    if lg.phase != "counting":
        return jsonify({"error": "Wrong phase"}), 400
    if me_id not in (lg.player1_id, lg.player2_id):
        return jsonify({"error": "Not a player"}), 403

    _advance_counting(lg)
    db.session.commit()
    return jsonify({"ok": True, "phase": lg.phase})


# ── Kiosk & JSON API ─────────────────────────────────────────────────────────

@app.route("/kiosk")
def kiosk():
    return render_template("kiosk.html")


@app.route("/api/me")
def api_me():
    u = current_user()
    if not u:
        return jsonify({"error": "not_logged_in"}), 401
    return jsonify({
        "id": u.id,
        "username": u.username,
        "player_id": u.player_id,
        "player_name": u.player.name if u.player else None,
        "is_admin": u.is_admin,
    })


@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json() or {}
    username = data.get("username", "").strip()
    password = data.get("password", "")
    user = User.query.filter_by(username=username).first()
    if user and check_password_hash(user.password_hash, password):
        session["user_id"] = user.id
        return jsonify({
            "ok": True,
            "id": user.id,
            "username": user.username,
            "player_id": user.player_id,
            "player_name": user.player.name if user.player else None,
        })
    return jsonify({"error": "Invalid username or password"}), 401


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.pop("user_id", None)
    return jsonify({"ok": True})


@app.route("/api/lobby")
def api_lobby():
    u = current_user()
    if not u:
        return jsonify({"error": "not_logged_in"}), 401
    me_id = u.player_id
    open_games = []
    my_games = []
    if me_id:
        for g in LiveGame.query.filter_by(player2_id=None, phase="lobby").order_by(LiveGame.created_at.desc()).all():
            if g.player1_id != me_id:
                open_games.append({"id": g.id, "creator": g.player1.name})
        for g in LiveGame.query.filter(
            ((LiveGame.player1_id == me_id) | (LiveGame.player2_id == me_id)),
            LiveGame.phase != "complete",
        ).order_by(LiveGame.created_at.desc()).all():
            opp = g.player2 if g.player2_id != me_id else g.player1
            my_games.append({
                "id": g.id,
                "phase": g.phase,
                "opponent": opp.name if opp and opp.id != me_id else None,
                "waiting": g.player2_id is None,
            })
    return jsonify({"player_id": me_id, "open_games": open_games, "my_games": my_games})


@app.route("/api/games/new", methods=["POST"])
def api_new_game():
    u = current_user()
    if not u:
        return jsonify({"error": "not_logged_in"}), 401
    if not u.player_id:
        return jsonify({"error": "no_player_linked"}), 400
    lg = LiveGame(player1_id=u.player_id, phase="lobby")
    db.session.add(lg)
    db.session.commit()
    return jsonify({"ok": True, "game_id": lg.id})


@app.route("/api/games/<int:game_id>/join", methods=["POST"])
def api_join_game(game_id: int):
    u = current_user()
    if not u:
        return jsonify({"error": "not_logged_in"}), 401
    if not u.player_id:
        return jsonify({"error": "no_player_linked"}), 400
    me_id = u.player_id
    lg = LiveGame.query.get_or_404(game_id)
    if lg.player2_id is not None:
        return jsonify({"error": "Game already full"}), 400
    if lg.player1_id == me_id:
        return jsonify({"error": "Cannot join your own game"}), 400
    lg.player2_id = me_id
    lg.dealer_id = random.choice([lg.player1_id, lg.player2_id])
    lg.first_dealer_id = lg.dealer_id
    deck = new_deck()
    p1_hand, p2_hand, remaining = deal_hands(deck)
    state = {
        "p1_hand": p1_hand, "p2_hand": p2_hand, "crib": [],
        "starter": None, "p1_discarded": False, "p2_discarded": False,
        "deck": remaining, "pegging": None, "counting_subphase": None,
        "events": ["Game started! Discard 2 cards to the crib."],
    }
    lg.set_state(state)
    lg.phase = "discarding"
    db.session.commit()
    return jsonify({"ok": True, "game_id": game_id})


@app.cli.command("init-db")
def init_db() -> None:
    db.create_all()
    ensure_schema_updates()
    print("Database initialized.")


def ensure_schema_updates() -> None:
    inspector = inspect(db.engine)
    tables = inspector.get_table_names()
    if "games" not in tables:
        return

    game_columns = {col["name"] for col in inspector.get_columns("games")}
    if "first_crib_id" not in game_columns:
        db.session.execute(text("ALTER TABLE games ADD COLUMN first_crib_id INTEGER"))
        db.session.commit()


with app.app_context():
    db.create_all()
    ensure_schema_updates()


if __name__ == "__main__":
    app.run(debug=True)
