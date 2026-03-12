import os
import threading
import math
from datetime import datetime, timedelta, date
from flask import Flask, render_template, request, jsonify, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from authlib.integrations.flask_client import OAuth
from werkzeug.middleware.proxy_fix import ProxyFix
from dotenv import load_dotenv

load_dotenv()

# Detect production environment (set PRODUCTION=true in Koyeb env vars)
IS_PRODUCTION = bool(os.environ.get("PRODUCTION") or os.environ.get("DATABASE_URL"))

app = Flask(__name__)
# Trust X-Forwarded-Proto / X-Forwarded-Host from Koyeb's proxy
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-insecure-key-change-me")

# ── Session / cookie security ─────────────────────────────────────────────────
# Secure=True ensures the cookie is only sent over HTTPS in production.
# SameSite=Lax allows the cookie to be sent on top-level cross-site navigations
# (the GET redirect back from Google), which is required for OAuth state to survive.
app.config["SESSION_COOKIE_SECURE"] = IS_PRODUCTION
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# Make url_for() produce https:// URLs in production even if the internal
# request arrives as http (gunicorn behind Koyeb's TLS terminator).
if IS_PRODUCTION:
    app.config["PREFERRED_URL_SCHEME"] = "https"

# ── Database ──────────────────────────────────────────────────────────────────
_data_dir = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
os.makedirs(_data_dir, exist_ok=True)

_raw_db_url = os.environ.get("DATABASE_URL", "")
_engine_opts: dict = {"pool_pre_ping": True}

if _raw_db_url.startswith("postgres://") or _raw_db_url.startswith("postgresql://"):
    # psycopg2 handles sslmode=require natively in the URL — no stripping needed.
    # Normalize legacy "postgres://" scheme to "postgresql://" for SQLAlchemy.
    _db_url = _raw_db_url.replace("postgres://", "postgresql://", 1)
else:
    # Local SQLite fallback
    _db_url = _raw_db_url or f"sqlite:///{os.path.join(_data_dir, 'bankroll.db')}"

app.config["SQLALCHEMY_DATABASE_URI"] = _db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = _engine_opts
db = SQLAlchemy(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"

# ── Google OAuth ──────────────────────────────────────────────────────────────
oauth = OAuth(app)
google = oauth.register(
    name="google",
    client_id=os.environ.get("GOOGLE_CLIENT_ID"),
    client_secret=os.environ.get("GOOGLE_CLIENT_SECRET"),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)


# ── Models ────────────────────────────────────────────────────────────────────

class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    google_id = db.Column(db.String(128), unique=True, nullable=False)
    name = db.Column(db.String(256))
    email = db.Column(db.String(256))
    picture = db.Column(db.String(512))


class BankrollSettings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, unique=True)
    starting_bankroll = db.Column(db.Float, default=0.0)
    unit_size_pct = db.Column(db.Float, default=2.0)
    daily_limit = db.Column(db.Float, default=0.0)
    weekly_limit = db.Column(db.Float, default=0.0)
    monthly_limit = db.Column(db.Float, default=0.0)
    odds_format = db.Column(db.String(20), default="american")


class BankrollTransaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    type = db.Column(db.String(20), nullable=False)  # deposit / withdrawal
    amount = db.Column(db.Float, nullable=False)
    date = db.Column(db.Date, nullable=False)
    notes = db.Column(db.String(500))


class Bet(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    sport = db.Column(db.String(50), nullable=False)
    event = db.Column(db.String(200), nullable=False)
    bet_type = db.Column(db.String(50), nullable=False)
    odds_american = db.Column(db.Integer, nullable=False)
    stake = db.Column(db.Float, nullable=False)
    units = db.Column(db.Float, nullable=False, default=1.0)
    result = db.Column(db.String(20), nullable=False, default="Pending")
    profit_loss = db.Column(db.Float, default=0.0)
    date_placed = db.Column(db.Date, nullable=False)
    notes = db.Column(db.String(500))
    closing_line_odds = db.Column(db.Integer, nullable=True)


class BankrollHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    date = db.Column(db.Date, nullable=False)
    balance = db.Column(db.Float, nullable=False)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# ── Helpers ───────────────────────────────────────────────────────────────────

def american_to_decimal(odds):
    if odds >= 0:
        return (odds / 100) + 1
    return (-100 / odds) + 1


def american_to_fractional(odds):
    from math import gcd
    if odds >= 0:
        num, den = int(odds), 100
    else:
        num, den = 100, int(-odds)
    g = gcd(num, den)
    return f"{num // g}/{den // g}"


def decimal_to_american(d):
    if d >= 2:
        return int(round((d - 1) * 100))
    return int(round(-100 / (d - 1)))


def fractional_to_american(frac_str):
    try:
        parts = frac_str.replace(" ", "").split("/")
        num, den = float(parts[0]), float(parts[1])
        return decimal_to_american((num / den) + 1)
    except Exception:
        return 0


def calc_clv(bet_odds_american, closing_odds_american):
    """CLV% = (bet_decimal - closing_decimal) / closing_decimal * 100. Positive = beat the closing line."""
    if closing_odds_american is None:
        return None
    closing_decimal = american_to_decimal(closing_odds_american)
    if closing_decimal <= 1:
        return None
    bet_decimal = american_to_decimal(bet_odds_american)
    return round((bet_decimal - closing_decimal) / closing_decimal * 100, 2)


def calc_profit(odds_american, stake, result):
    if result == "Win":
        return round(stake * (american_to_decimal(odds_american) - 1), 2)
    elif result == "Loss":
        return round(-stake, 2)
    return 0.0


def get_settings(user_id):
    s = BankrollSettings.query.filter_by(user_id=user_id).first()
    if not s:
        s = BankrollSettings(user_id=user_id, starting_bankroll=0.0, unit_size_pct=2.0,
                              daily_limit=0.0, weekly_limit=0.0, monthly_limit=0.0,
                              odds_format="american")
        db.session.add(s)
        db.session.commit()
    return s


def get_current_bankroll(user_id):
    settings = get_settings(user_id)
    total = settings.starting_bankroll
    for t in BankrollTransaction.query.filter_by(user_id=user_id).all():
        total += t.amount if t.type == "deposit" else -t.amount
    for b in Bet.query.filter_by(user_id=user_id).filter(Bet.result != "Pending").all():
        total += b.profit_loss
    return round(total, 2)


def snapshot_bankroll(user_id):
    balance = get_current_bankroll(user_id)
    today = date.today()
    existing = BankrollHistory.query.filter_by(user_id=user_id, date=today).first()
    if existing:
        existing.balance = balance
    else:
        db.session.add(BankrollHistory(user_id=user_id, date=today, balance=balance))
    db.session.commit()


def get_loss_totals(user_id):
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)

    def sum_losses(start_date):
        bets = Bet.query.filter_by(user_id=user_id).filter(
            Bet.result == "Loss", Bet.date_placed >= start_date
        ).all()
        return round(sum(abs(b.profit_loss) for b in bets), 2)

    return {
        "daily": sum_losses(today),
        "weekly": sum_losses(week_start),
        "monthly": sum_losses(month_start),
    }


def _init_db():
    import time
    for attempt in range(5):
        try:
            with app.app_context():
                db.create_all()
            print(f"[startup] database tables ready (attempt {attempt + 1})", flush=True)
            return
        except Exception as e:
            wait = 2 ** attempt
            print(f"[startup] db attempt {attempt + 1}/5 failed: {e} — retrying in {wait}s", flush=True)
            if attempt < 4:
                time.sleep(wait)
    print("[startup] db.create_all() failed after 5 attempts — app may error on first DB use", flush=True)


threading.Thread(target=_init_db, daemon=True).start()


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.route("/login")
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    return render_template("login.html")


@app.route("/auth/google")
def auth_google():
    # _scheme='https' forces the correct URI on Koyeb regardless of whether
    # ProxyFix has had a chance to set the scheme on this request.
    # authorize_access_token() must be called with NO redirect_uri argument —
    # Authlib reads it back from the session state automatically.
    scheme = "https" if IS_PRODUCTION else "http"
    redirect_uri = url_for("auth_callback", _external=True, _scheme=scheme)
    return google.authorize_redirect(redirect_uri)


@app.route("/auth/callback")
def auth_callback():
    import traceback
    try:
        print("[auth_callback] fetching access token", flush=True)
        token = google.authorize_access_token()
        userinfo = token.get("userinfo")
        if not userinfo:
            print("[auth_callback] no userinfo in token", flush=True)
            return redirect(url_for("login"))
        google_id = userinfo["sub"]
        user = User.query.filter_by(google_id=google_id).first()
        if not user:
            user = User(google_id=google_id, name=userinfo.get("name"),
                        email=userinfo.get("email"), picture=userinfo.get("picture"))
            db.session.add(user)
        else:
            user.name = userinfo.get("name")
            user.picture = userinfo.get("picture")
        db.session.commit()
        login_user(user)
        return redirect(url_for("dashboard"))
    except Exception as exc:
        tb = traceback.format_exc()
        print(f"[auth_callback] ERROR: {exc}\n{tb}", flush=True)
        # In production show a clean message; the full error is in Render logs.
        # In dev mode show the traceback directly so it's easy to diagnose.
        if IS_PRODUCTION:
            return (
                "<h2>Login error</h2>"
                f"<p>Something went wrong during Google login. "
                f"Check the Koyeb logs for details.</p>"
                f"<p><code>{exc}</code></p>"
                f"<p><a href='/login'>Try again</a></p>"
            ), 500
        return f"<pre>{tb}</pre>", 500


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


@app.route("/health")
def health():
    return jsonify({"status": "ok"}), 200


# ── Page routes ───────────────────────────────────────────────────────────────

@app.route("/")
@login_required
def dashboard():
    return render_template("dashboard.html")


@app.route("/bets")
@login_required
def bets_page():
    return render_template("bets.html")


@app.route("/analytics")
@login_required
def analytics_page():
    return render_template("analytics.html")


@app.route("/kelly")
@login_required
def kelly_page():
    return render_template("kelly.html")


@app.route("/settings")
@login_required
def settings_page():
    return render_template("settings.html")


# ── API: Dashboard ────────────────────────────────────────────────────────────

@app.route("/api/dashboard")
@login_required
def api_dashboard():
    uid = current_user.id
    bankroll = get_current_bankroll(uid)
    settings = get_settings(uid)
    unit_size = round(bankroll * settings.unit_size_pct / 100, 2)

    pending = Bet.query.filter_by(user_id=uid, result="Pending").order_by(Bet.date_placed.desc()).all()
    pending_out = []
    for b in pending:
        dec = american_to_decimal(b.odds_american)
        pending_out.append({
            "id": b.id, "sport": b.sport, "event": b.event, "bet_type": b.bet_type,
            "odds_american": b.odds_american,
            "odds_decimal": round(dec, 2),
            "odds_fractional": american_to_fractional(b.odds_american),
            "stake": b.stake, "units": b.units,
            "potential_payout": round(b.stake * dec, 2),
            "potential_profit": round(b.stake * (dec - 1), 2),
            "date_placed": b.date_placed.isoformat(),
        })

    recent = Bet.query.filter_by(user_id=uid).filter(
        Bet.result != "Pending"
    ).order_by(Bet.date_placed.desc(), Bet.id.desc()).limit(10).all()
    recent_out = [{
        "id": b.id, "sport": b.sport, "event": b.event, "bet_type": b.bet_type,
        "odds_american": b.odds_american, "stake": b.stake, "units": b.units,
        "result": b.result, "profit_loss": b.profit_loss,
        "date_placed": b.date_placed.isoformat(),
    } for b in recent]

    losses = get_loss_totals(uid)
    history = BankrollHistory.query.filter_by(user_id=uid).order_by(BankrollHistory.date).all()

    all_settled = Bet.query.filter_by(user_id=uid).filter(Bet.result != "Pending").all()
    wins = [b for b in all_settled if b.result == "Win"]
    losses_bets = [b for b in all_settled if b.result == "Loss"]
    total_pl = sum(b.profit_loss for b in all_settled)
    total_wagered = sum(b.stake for b in all_settled if b.result in ("Win", "Loss"))
    roi = (total_pl / total_wagered * 100) if total_wagered > 0 else 0
    win_rate = (len(wins) / (len(wins) + len(losses_bets)) * 100) if (wins or losses_bets) else 0

    snapshot_bankroll(uid)

    return jsonify({
        "bankroll": bankroll,
        "starting_bankroll": settings.starting_bankroll,
        "unit_size_pct": settings.unit_size_pct,
        "unit_size": unit_size,
        "pending_bets": pending_out,
        "recent_bets": recent_out,
        "limits": {
            "daily": {"limit": settings.daily_limit, "used": losses["daily"]},
            "weekly": {"limit": settings.weekly_limit, "used": losses["weekly"]},
            "monthly": {"limit": settings.monthly_limit, "used": losses["monthly"]},
        },
        "stats": {
            "total_bets": len(all_settled),
            "wins": len(wins),
            "losses": len(losses_bets),
            "win_rate": round(win_rate, 1),
            "total_pl": round(total_pl, 2),
            "roi": round(roi, 1),
        },
        "bankroll_history": [
            {"date": h.date.isoformat(), "balance": h.balance} for h in history
        ],
        "user": {"name": current_user.name, "picture": current_user.picture},
    })


# ── API: Bets ─────────────────────────────────────────────────────────────────

@app.route("/api/bets", methods=["GET"])
@login_required
def api_bets():
    uid = current_user.id
    q = Bet.query.filter_by(user_id=uid)

    sport = request.args.get("sport")
    bet_type = request.args.get("bet_type")
    result = request.args.get("result")
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")

    if sport and sport != "All":
        q = q.filter(Bet.sport == sport)
    if bet_type and bet_type != "All":
        q = q.filter(Bet.bet_type == bet_type)
    if result and result != "All":
        q = q.filter(Bet.result == result)
    if date_from:
        try:
            q = q.filter(Bet.date_placed >= datetime.strptime(date_from, "%Y-%m-%d").date())
        except ValueError:
            pass
    if date_to:
        try:
            q = q.filter(Bet.date_placed <= datetime.strptime(date_to, "%Y-%m-%d").date())
        except ValueError:
            pass

    bets = q.order_by(Bet.date_placed.desc(), Bet.id.desc()).all()
    settings = get_settings(uid)
    bankroll = get_current_bankroll(uid)

    return jsonify({
        "bets": [{
            "id": b.id, "sport": b.sport, "event": b.event, "bet_type": b.bet_type,
            "odds_american": b.odds_american,
            "odds_decimal": round(american_to_decimal(b.odds_american), 2),
            "odds_fractional": american_to_fractional(b.odds_american),
            "stake": b.stake, "units": b.units,
            "result": b.result, "profit_loss": b.profit_loss,
            "date_placed": b.date_placed.isoformat(),
            "notes": b.notes or "",
            "closing_line_odds": b.closing_line_odds,
            "clv": calc_clv(b.odds_american, b.closing_line_odds),
        } for b in bets],
        "unit_size": round(bankroll * settings.unit_size_pct / 100, 2),
        "unit_size_pct": settings.unit_size_pct,
        "bankroll": bankroll,
    })


@app.route("/api/bets", methods=["POST"])
@login_required
def api_add_bet():
    uid = current_user.id
    body = request.json or {}

    odds_format = body.get("odds_format", "american")
    odds_value = body.get("odds_value", "")
    try:
        if odds_format == "american":
            odds_american = int(float(str(odds_value)))
        elif odds_format == "decimal":
            odds_american = decimal_to_american(float(odds_value))
        elif odds_format == "fractional":
            odds_american = fractional_to_american(str(odds_value))
        else:
            return jsonify({"error": "Invalid odds format"}), 400
    except (ValueError, ZeroDivisionError):
        return jsonify({"error": "Invalid odds value"}), 400

    try:
        stake = float(body.get("stake", 0))
        units = float(body.get("units", 1))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid stake or units"}), 400

    if stake <= 0:
        return jsonify({"error": "Stake must be greater than 0"}), 400

    sport = body.get("sport", "").strip()
    event = body.get("event", "").strip()
    bet_type = body.get("bet_type", "").strip()
    result = body.get("result", "Pending")
    notes = body.get("notes", "").strip()

    date_str = body.get("date_placed", "")
    try:
        date_placed = datetime.strptime(date_str, "%Y-%m-%d").date() if date_str else date.today()
    except ValueError:
        date_placed = date.today()

    if not sport or not event or not bet_type:
        return jsonify({"error": "Sport, event, and bet type are required"}), 400

    profit_loss = calc_profit(odds_american, stake, result)

    bet = Bet(user_id=uid, sport=sport, event=event, bet_type=bet_type,
              odds_american=odds_american, stake=stake, units=units,
              result=result, profit_loss=profit_loss, date_placed=date_placed, notes=notes)
    db.session.add(bet)
    db.session.commit()
    snapshot_bankroll(uid)
    return jsonify({"message": "Bet added", "id": bet.id})


@app.route("/api/bets/<int:bet_id>", methods=["PUT"])
@login_required
def api_update_bet(bet_id):
    uid = current_user.id
    bet = Bet.query.filter_by(id=bet_id, user_id=uid).first()
    if not bet:
        return jsonify({"error": "Bet not found"}), 404

    body = request.json or {}

    for field in ("sport", "event", "bet_type", "notes"):
        if field in body:
            setattr(bet, field, body[field])

    if "result" in body:
        bet.result = body["result"]

    if "stake" in body:
        try:
            bet.stake = float(body["stake"])
        except (TypeError, ValueError):
            pass

    if "units" in body:
        try:
            bet.units = float(body["units"])
        except (TypeError, ValueError):
            pass

    if "odds_value" in body:
        odds_format = body.get("odds_format", "american")
        try:
            if odds_format == "american":
                bet.odds_american = int(float(str(body["odds_value"])))
            elif odds_format == "decimal":
                bet.odds_american = decimal_to_american(float(body["odds_value"]))
            elif odds_format == "fractional":
                bet.odds_american = fractional_to_american(str(body["odds_value"]))
        except Exception:
            pass

    if "date_placed" in body:
        try:
            bet.date_placed = datetime.strptime(body["date_placed"], "%Y-%m-%d").date()
        except ValueError:
            pass

    if "closing_line_odds" in body:
        val = body["closing_line_odds"]
        if val in (None, "", "null"):
            bet.closing_line_odds = None
        else:
            try:
                bet.closing_line_odds = int(float(str(val)))
            except (TypeError, ValueError):
                pass

    bet.profit_loss = calc_profit(bet.odds_american, bet.stake, bet.result)
    db.session.commit()
    snapshot_bankroll(uid)
    return jsonify({"message": "Bet updated"})


@app.route("/api/bets/<int:bet_id>", methods=["DELETE"])
@login_required
def api_delete_bet(bet_id):
    uid = current_user.id
    bet = Bet.query.filter_by(id=bet_id, user_id=uid).first()
    if not bet:
        return jsonify({"error": "Bet not found"}), 404
    db.session.delete(bet)
    db.session.commit()
    snapshot_bankroll(uid)
    return jsonify({"message": "Bet deleted"})


# ── API: Transactions ─────────────────────────────────────────────────────────

@app.route("/api/transactions", methods=["GET"])
@login_required
def api_transactions():
    uid = current_user.id
    txns = BankrollTransaction.query.filter_by(user_id=uid).order_by(
        BankrollTransaction.date.desc()).all()
    return jsonify([{
        "id": t.id, "type": t.type, "amount": t.amount,
        "date": t.date.isoformat(), "notes": t.notes or "",
    } for t in txns])


@app.route("/api/transactions", methods=["POST"])
@login_required
def api_add_transaction():
    uid = current_user.id
    body = request.json or {}
    txn_type = body.get("type", "").lower()
    if txn_type not in ("deposit", "withdrawal"):
        return jsonify({"error": "Type must be deposit or withdrawal"}), 400
    try:
        amount = float(body.get("amount", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid amount"}), 400
    if amount <= 0:
        return jsonify({"error": "Amount must be greater than 0"}), 400

    date_str = body.get("date", "")
    try:
        txn_date = datetime.strptime(date_str, "%Y-%m-%d").date() if date_str else date.today()
    except ValueError:
        txn_date = date.today()

    t = BankrollTransaction(user_id=uid, type=txn_type, amount=amount,
                             date=txn_date, notes=body.get("notes", "").strip())
    db.session.add(t)
    db.session.commit()
    snapshot_bankroll(uid)
    return jsonify({"message": "Transaction added", "new_balance": get_current_bankroll(uid)})


@app.route("/api/transactions/<int:txn_id>", methods=["DELETE"])
@login_required
def api_delete_transaction(txn_id):
    uid = current_user.id
    t = BankrollTransaction.query.filter_by(id=txn_id, user_id=uid).first()
    if not t:
        return jsonify({"error": "Transaction not found"}), 404
    db.session.delete(t)
    db.session.commit()
    snapshot_bankroll(uid)
    return jsonify({"message": "Transaction deleted"})


# ── API: Settings ─────────────────────────────────────────────────────────────

@app.route("/api/settings", methods=["GET"])
@login_required
def api_get_settings():
    uid = current_user.id
    s = get_settings(uid)
    return jsonify({
        "starting_bankroll": s.starting_bankroll,
        "unit_size_pct": s.unit_size_pct,
        "daily_limit": s.daily_limit,
        "weekly_limit": s.weekly_limit,
        "monthly_limit": s.monthly_limit,
        "odds_format": s.odds_format or "american",
    })


@app.route("/api/settings", methods=["POST"])
@login_required
def api_update_settings():
    uid = current_user.id
    body = request.json or {}
    s = get_settings(uid)

    if "starting_bankroll" in body:
        try:
            s.starting_bankroll = float(body["starting_bankroll"])
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid starting bankroll"}), 400

    if "unit_size_pct" in body:
        try:
            pct = float(body["unit_size_pct"])
            if not (0.1 <= pct <= 100):
                return jsonify({"error": "Unit size must be between 0.1% and 100%"}), 400
            s.unit_size_pct = pct
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid unit size"}), 400

    for field in ("daily_limit", "weekly_limit", "monthly_limit"):
        if field in body:
            try:
                setattr(s, field, float(body[field]))
            except (TypeError, ValueError):
                pass

    if "odds_format" in body:
        if body["odds_format"] in ("american", "decimal", "fractional"):
            s.odds_format = body["odds_format"]

    db.session.commit()
    snapshot_bankroll(uid)
    return jsonify({"message": "Settings updated"})


# ── API: Analytics ────────────────────────────────────────────────────────────

@app.route("/api/analytics")
@login_required
def api_analytics():
    uid = current_user.id
    all_bets = Bet.query.filter_by(user_id=uid).all()
    settled = [b for b in all_bets if b.result != "Pending"]
    wins = [b for b in settled if b.result == "Win"]
    losses_list = [b for b in settled if b.result == "Loss"]
    pushes = [b for b in settled if b.result in ("Push", "Void")]

    total_pl = sum(b.profit_loss for b in settled)
    total_wagered = sum(b.stake for b in settled if b.result in ("Win", "Loss"))
    roi = (total_pl / total_wagered * 100) if total_wagered > 0 else 0
    win_rate = (len(wins) / (len(wins) + len(losses_list)) * 100) if (wins or losses_list) else 0
    avg_odds = (sum(b.odds_american for b in settled) / len(settled)) if settled else 0
    avg_stake = (sum(b.stake for b in settled) / len(settled)) if settled else 0
    biggest_win = max((b.profit_loss for b in wins), default=0)
    biggest_loss = min((b.profit_loss for b in losses_list), default=0)

    # By sport
    sport_stats = {}
    for b in settled:
        s = b.sport
        if s not in sport_stats:
            sport_stats[s] = {"wins": 0, "losses": 0, "pl": 0, "wagered": 0}
        if b.result == "Win":
            sport_stats[s]["wins"] += 1
        elif b.result == "Loss":
            sport_stats[s]["losses"] += 1
        sport_stats[s]["pl"] += b.profit_loss
        if b.result in ("Win", "Loss"):
            sport_stats[s]["wagered"] += b.stake

    sport_out = []
    for sport, stats in sport_stats.items():
        total_s = stats["wins"] + stats["losses"]
        sport_out.append({
            "sport": sport, "wins": stats["wins"], "losses": stats["losses"],
            "win_rate": round(stats["wins"] / total_s * 100, 1) if total_s > 0 else 0,
            "pl": round(stats["pl"], 2),
            "roi": round(stats["pl"] / stats["wagered"] * 100, 1) if stats["wagered"] > 0 else 0,
        })

    # By bet type
    type_stats = {}
    for b in settled:
        t = b.bet_type
        if t not in type_stats:
            type_stats[t] = {"wins": 0, "losses": 0, "pl": 0, "wagered": 0}
        if b.result == "Win":
            type_stats[t]["wins"] += 1
        elif b.result == "Loss":
            type_stats[t]["losses"] += 1
        type_stats[t]["pl"] += b.profit_loss
        if b.result in ("Win", "Loss"):
            type_stats[t]["wagered"] += b.stake

    type_out = []
    for bt, stats in type_stats.items():
        total_t = stats["wins"] + stats["losses"]
        type_out.append({
            "bet_type": bt, "wins": stats["wins"], "losses": stats["losses"],
            "win_rate": round(stats["wins"] / total_t * 100, 1) if total_t > 0 else 0,
            "pl": round(stats["pl"], 2),
            "roi": round(stats["pl"] / stats["wagered"] * 100, 1) if stats["wagered"] > 0 else 0,
        })

    # Streaks
    sorted_bets = sorted(settled, key=lambda b: (b.date_placed, b.id))
    max_win_streak = max_loss_streak = temp_win = temp_loss = 0
    for b in sorted_bets:
        if b.result == "Win":
            temp_win += 1
            temp_loss = 0
            max_win_streak = max(max_win_streak, temp_win)
        elif b.result == "Loss":
            temp_loss += 1
            temp_win = 0
            max_loss_streak = max(max_loss_streak, temp_loss)
        else:
            temp_win = temp_loss = 0

    current_streak = 0
    current_streak_type = None
    if sorted_bets:
        last = sorted_bets[-1]
        if last.result in ("Win", "Loss"):
            current_streak_type = "W" if last.result == "Win" else "L"
            for b in reversed(sorted_bets):
                if b.result == last.result:
                    current_streak += 1
                else:
                    break

    # P&L over time
    cumulative = 0
    pl_over_time = []
    for b in sorted_bets:
        cumulative += b.profit_loss
        pl_over_time.append({
            "date": b.date_placed.isoformat(),
            "pl": round(cumulative, 2),
            "event": b.event,
        })

    # Period stats
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)

    def period_stats(start_date):
        pb = [b for b in settled if b.date_placed >= start_date]
        return {
            "bets": len(pb),
            "wins": sum(1 for b in pb if b.result == "Win"),
            "losses": sum(1 for b in pb if b.result == "Loss"),
            "pl": round(sum(b.profit_loss for b in pb), 2),
        }

    # Max drawdown
    peak = running = max_drawdown = 0
    for b in sorted_bets:
        running += b.profit_loss
        if running > peak:
            peak = running
        dd = peak - running
        if dd > max_drawdown:
            max_drawdown = dd

    # CLV stats
    bets_with_clv = sorted(
        [b for b in settled if b.closing_line_odds is not None],
        key=lambda b: (b.date_placed, b.id)
    )
    clv_values = [c for b in bets_with_clv for c in [calc_clv(b.odds_american, b.closing_line_odds)] if c is not None]
    avg_clv = round(sum(clv_values) / len(clv_values), 2) if clv_values else None
    clv_beat_pct = round(sum(1 for c in clv_values if c > 0) / len(clv_values) * 100, 1) if clv_values else None
    clv_over_time = []
    for b in bets_with_clv:
        c = calc_clv(b.odds_american, b.closing_line_odds)
        if c is not None:
            clv_over_time.append({"date": b.date_placed.isoformat(), "clv": c, "event": b.event})

    settings = get_settings(uid)
    bankroll = get_current_bankroll(uid)
    avg_bet_pl = total_pl / len(settled) if settled else 0

    return jsonify({
        "record": {
            "wins": len(wins), "losses": len(losses_list),
            "pushes": len(pushes), "total": len(settled),
            "win_rate": round(win_rate, 1),
        },
        "financials": {
            "total_pl": round(total_pl, 2),
            "total_wagered": round(total_wagered, 2),
            "roi": round(roi, 1),
            "avg_odds": round(avg_odds, 0),
            "avg_stake": round(avg_stake, 2),
            "biggest_win": round(biggest_win, 2),
            "biggest_loss": round(biggest_loss, 2),
        },
        "streaks": {
            "current_streak": current_streak,
            "current_streak_type": current_streak_type,
            "max_win_streak": max_win_streak,
            "max_loss_streak": max_loss_streak,
            "max_drawdown": round(max_drawdown, 2),
        },
        "by_sport": sport_out,
        "by_type": type_out,
        "pl_over_time": pl_over_time,
        "period_stats": {
            "daily": period_stats(today),
            "weekly": period_stats(week_start),
            "monthly": period_stats(month_start),
        },
        "projection": {
            "bankroll": bankroll,
            "unit_size": round(bankroll * settings.unit_size_pct / 100, 2),
            "avg_bet_pl": round(avg_bet_pl, 2),
            "roi_pct": round(roi, 1),
        },
        "clv": {
            "avg_clv": avg_clv,
            "beat_closing_pct": clv_beat_pct,
            "total_with_clv": len(clv_values),
            "over_time": clv_over_time,
        },
    })


# ── API: Kelly Calculator ─────────────────────────────────────────────────────

@app.route("/api/kelly", methods=["POST"])
@login_required
def api_kelly():
    body = request.json or {}
    odds_format = body.get("odds_format", "american")
    odds_value = body.get("odds_value", "")
    win_prob_pct = body.get("win_prob", 50)

    try:
        win_prob = float(win_prob_pct) / 100
        if not (0 < win_prob < 1):
            return jsonify({"error": "Win probability must be between 1% and 99%"}), 400
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid win probability"}), 400

    try:
        if odds_format == "american":
            odds_american = int(float(str(odds_value)))
        elif odds_format == "decimal":
            odds_american = decimal_to_american(float(odds_value))
        elif odds_format == "fractional":
            odds_american = fractional_to_american(str(odds_value))
        else:
            return jsonify({"error": "Invalid odds format"}), 400
    except (ValueError, ZeroDivisionError):
        return jsonify({"error": "Invalid odds value"}), 400

    decimal_odds = american_to_decimal(odds_american)
    b = decimal_odds - 1  # net profit per $1 staked on win
    q = 1 - win_prob

    kelly_fraction = (b * win_prob - q) / b if b > 0 else 0
    kelly_fraction = max(0, kelly_fraction)

    ev = (win_prob * b) - q

    uid = current_user.id
    bankroll = get_current_bankroll(uid)
    settings = get_settings(uid)
    unit_size = bankroll * settings.unit_size_pct / 100

    return jsonify({
        "odds_american": odds_american,
        "odds_decimal": round(decimal_odds, 3),
        "odds_fractional": american_to_fractional(odds_american),
        "win_prob": round(win_prob * 100, 1),
        "loss_prob": round(q * 100, 1),
        "implied_prob": round(1 / decimal_odds * 100, 1),
        "ev_pct": round(ev * 100, 2),
        "ev_positive": ev > 0,
        "kelly_pct": {
            "full": round(kelly_fraction * 100, 2),
            "half": round(kelly_fraction * 50, 2),
            "quarter": round(kelly_fraction * 25, 2),
        },
        "kelly_amount": {
            "full": round(bankroll * kelly_fraction, 2),
            "half": round(bankroll * kelly_fraction * 0.5, 2),
            "quarter": round(bankroll * kelly_fraction * 0.25, 2),
        },
        "kelly_units": {
            "full": round(bankroll * kelly_fraction / unit_size, 2) if unit_size > 0 else 0,
            "half": round(bankroll * kelly_fraction * 0.5 / unit_size, 2) if unit_size > 0 else 0,
            "quarter": round(bankroll * kelly_fraction * 0.25 / unit_size, 2) if unit_size > 0 else 0,
        },
        "bankroll": bankroll,
        "unit_size": round(unit_size, 2),
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
