from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import os

from flask import Flask, flash, redirect, render_template, request, session, url_for
from flask_sqlalchemy import SQLAlchemy
from functools import wraps
from sqlalchemy import CheckConstraint, inspect, text

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///cribbage.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SECRET_KEY"] = "change-this-secret-key"
app.config["ADMIN_PASSWORD"] = os.environ.get("ADMIN_PASSWORD", "change-admin-password")
db = SQLAlchemy(app)
WINNING_SCORE = 121
SKUNK_THRESHOLD = 90
DOUBLE_SKUNK_THRESHOLD = 60


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


def is_admin() -> bool:
    return bool(session.get("is_admin"))


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_admin():
            flash("Admin login required.", "error")
            return redirect(url_for("admin_login"))
        return view(*args, **kwargs)

    return wrapped


@app.context_processor
def inject_auth_context():
    return {"is_admin": is_admin()}


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

    active_rows = [row for row in raw_rows if row.games > 0]
    min_margin = min((row.avg_margin for row in active_rows), default=0.0)
    max_margin = max((row.avg_margin for row in active_rows), default=0.0)
    margin_span = max_margin - min_margin

    for row in raw_rows:
        if row.games == 0:
            row.champion_score = 0.0
            continue

        margin_component = (
            (row.avg_margin - min_margin) / margin_span if margin_span > 0 else 0.5
        )
        # Family Champion Model:
        # 60% win rate, 25% scoring margin quality, 15% participation.
        row.champion_score = 100 * (
            (0.60 * row.win_rate) + (0.25 * margin_component) + (0.15 * row.participation)
        )

    ranked = sorted(
        raw_rows,
        key=lambda x: (x.champion_score, x.wins, x.point_diff, x.points_for),
        reverse=True,
    )
    winner = ranked[0] if ranked and ranked[0].games > 0 else None
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


@app.route("/leaderboard")
def leaderboard():
    year = request.args.get("year", type=int) or date.today().year
    ranked, winner = calculate_champion_scores(year)
    return render_template("leaderboard.html", year=year, ranked=ranked, winner=winner)


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        password = request.form.get("password", "")
        if password == app.config["ADMIN_PASSWORD"]:
            session["is_admin"] = True
            flash("Admin login successful.", "success")
            return redirect(url_for("admin_panel"))

        flash("Invalid admin password.", "error")
        return redirect(url_for("admin_login"))

    return render_template("admin_login.html")


@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    session.pop("is_admin", None)
    flash("Logged out.", "success")
    return redirect(url_for("index"))


@app.route("/admin")
@admin_required
def admin_panel():
    all_players = Player.query.order_by(Player.name.asc()).all()
    recent_games = Game.query.order_by(Game.played_on.desc(), Game.created_at.desc()).limit(50).all()
    return render_template("admin.html", players=all_players, recent_games=recent_games)


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


@app.cli.command("init-db")
def init_db() -> None:
    db.create_all()
    ensure_schema_updates()
    print("Database initialized.")


def ensure_schema_updates() -> None:
    inspector = inspect(db.engine)
    if "games" not in inspector.get_table_names():
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
