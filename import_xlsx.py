"""
Import historical games from Crubbage.xlsx into the cribbage database.
Run from project root: python import_xlsx.py
"""
import sys
from datetime import datetime
import openpyxl
from app import app, db, Player, Game

WINNING_SCORE = 121
XLSX_PATH = "Crubbage.xlsx"


def get_or_create_player(name, players_cache):
    if name not in players_cache:
        player = Player.query.filter_by(name=name).first()
        if not player:
            player = Player(name=name)
            db.session.add(player)
            db.session.flush()
            print(f"  Created player: {name}")
        else:
            print(f"  Found existing player: {name}")
        players_cache[name] = player
    return players_cache[name]


def main():
    wb = openpyxl.load_workbook(XLSX_PATH)
    ws = wb["Games"]

    rows = list(ws.iter_rows(min_row=2, values_only=True))
    print(f"Found {len(rows)} games to import.\n")

    with app.app_context():
        players_cache = {}
        imported = 0
        skipped = 0

        for i, row in enumerate(rows, start=2):
            (_, created, year, number, event, winner_name, loser_name,
             loser_score, margin, skunk, double_skunk, first_crib_col) = row

            if not winner_name or not loser_name or loser_score is None:
                print(f"  Row {i}: skipping incomplete row")
                skipped += 1
                continue

            winner = get_or_create_player(winner_name.strip(), players_cache)
            loser = get_or_create_player(loser_name.strip(), players_cache)

            played_on = created.date() if isinstance(created, datetime) else None
            if not played_on:
                print(f"  Row {i}: skipping — no date")
                skipped += 1
                continue

            # Resolve first_crib_id
            if first_crib_col == "WInner":
                first_crib_id = winner.id
            elif first_crib_col == "Loser":
                first_crib_id = loser.id
            else:
                first_crib_id = None

            game = Game(
                played_on=played_on,
                player1_id=winner.id,
                player2_id=loser.id,
                player1_score=WINNING_SCORE,
                player2_score=int(loser_score),
                winner_id=winner.id,
                first_crib_id=first_crib_id,
                notes=event.strip() if event else None,
                created_at=created if isinstance(created, datetime) else datetime.utcnow(),
            )
            db.session.add(game)
            imported += 1
            print(f"  Game {int(number) if number else i}: {winner_name} def. {loser_name} {WINNING_SCORE}-{int(loser_score)}")

        db.session.commit()
        print(f"\nDone! Imported {imported} games, skipped {skipped}.")


if __name__ == "__main__":
    main()
