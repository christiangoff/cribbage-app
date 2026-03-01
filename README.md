# Family Cribbage Web App

Python web app to collect and store cribbage games throughout the year, then declare an annual winner using a transparent scoring model.

## Features
- Public game logging (anyone can record a game)
- Admin-only management for:
  - adding players
  - removing players
  - deleting games
- Record 1v1 cribbage games with a simple flow: winner, opponent, opponent score, and first crib
- Winner score is fixed at `121` for every game
- Auto-calculate point differential, skunks, and double skunks from the losing score
- Store data in SQLite
- View yearly leaderboard and projected champion
- Explainable winner model:
  - 60% win rate
  - 25% average scoring margin (normalized)
  - 15% participation

## Quick Start
1. Create and activate a virtual environment:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Set admin password (recommended):
   ```bash
   export ADMIN_PASSWORD="your-strong-password"
   ```
4. Run the app:
   ```bash
   flask --app app run --debug
   ```
5. Open in browser:
   ```
   http://127.0.0.1:5000
   ```

## Notes
- Database file is created at `instance/cribbage.db`.
- To initialize manually:
  ```bash
  flask --app app init-db
  ```
- Default admin password fallback is `change-admin-password` if `ADMIN_PASSWORD` is not set.
