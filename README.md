# BankrollPro

A professional sports betting bankroll management application built with Python/Flask. Track bets, analyze performance, and manage risk across all major sports.

## Features

- **Google OAuth** — Private data per user, no passwords
- **Bankroll Tracking** — Deposits, withdrawals, real-time balance
- **Bet Logging** — NHL, NBA, NFL, MLB, UFC, Soccer, Tennis with full bet details
- **Unit System** — Auto-calculated unit sizes based on bankroll %
- **Kelly Criterion Calculator** — Optimal bet sizing with EV analysis
- **Analytics Dashboard** — Charts, win rates, ROI, streak tracking
- **Risk Management** — Daily/weekly/monthly loss limits with warnings
- **Odds Support** — American, decimal, and fractional formats

## Local Setup

1. **Clone and install**
   ```bash
   git clone <repo-url>
   cd bankroll-manager
   python -m venv venv
   source venv/bin/activate  # Windows: venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. **Configure environment**
   ```bash
   cp .env.example .env
   # Edit .env with your Google OAuth credentials
   ```

3. **Set up Google OAuth**
   - Go to [Google Cloud Console](https://console.cloud.google.com/apis/credentials)
   - Create an OAuth 2.0 Client ID (Web application)
   - Add `http://localhost:5000/auth/callback` to Authorized redirect URIs
   - Copy Client ID and Secret to `.env`

4. **Run**
   ```bash
   python app.py
   # Visit http://localhost:5000
   ```

## Deploying to Render

1. Push to GitHub
2. Connect repo in [Render Dashboard](https://render.com)
3. Use the included `render.yaml` for auto-configuration, or manually:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 60`
4. Set environment variables:
   - `GOOGLE_CLIENT_ID`
   - `GOOGLE_CLIENT_SECRET`
   - `SECRET_KEY` (generate with `python -c "import secrets; print(secrets.token_hex(32))"`)
   - `DATABASE_URL` (from a Render PostgreSQL instance)
5. Update Google OAuth authorized redirect URI to your Render URL:
   `https://your-app.onrender.com/auth/callback`

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `GOOGLE_CLIENT_ID` | Yes | Google OAuth client ID |
| `GOOGLE_CLIENT_SECRET` | Yes | Google OAuth client secret |
| `SECRET_KEY` | Yes | Flask session secret (generate randomly) |
| `DATABASE_URL` | No | PostgreSQL URL (defaults to SQLite) |
| `PORT` | No | Server port (defaults to 5000) |

## Tech Stack

- **Backend:** Python 3.11, Flask, SQLAlchemy
- **Auth:** Google OAuth via Authlib + Flask-Login
- **Database:** PostgreSQL (production) / SQLite (development)
- **Frontend:** Vanilla JS, Chart.js, custom CSS
- **Fonts:** Bebas Neue, Rajdhani, Space Mono
- **Deployment:** Render (gunicorn)
