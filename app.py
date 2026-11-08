import os, hmac, hashlib, secrets, requests, json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, abort
from flask_sqlalchemy import SQLAlchemy
from flask_bcrypt import Bcrypt
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

load_dotenv()
BASE = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE / "data")))
UPLOAD_DIR = DATA_DIR / "uploads"
DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "CHANGE-ME-BEFORE-DEPLOYING")
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", f"sqlite:///{DATA_DIR/'aryancloud.db'}").replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_UPLOAD_MB", "20")) * 1024 * 1024
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SECURE"] = os.getenv("COOKIE_SECURE", "true").lower() == "true"
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
db = SQLAlchemy(app)
bcrypt = Bcrypt(app)

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    is_blocked = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Plan(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)
    price = db.Column(db.Numeric(10,2), nullable=False)
    duration_days = db.Column(db.Integer, default=30)
    bot_limit = db.Column(db.Integer, default=1)
    ram_mb = db.Column(db.Integer, default=256)
    storage_mb = db.Column(db.Integer, default=512)
    active = db.Column(db.Boolean, default=True)

class Subscription(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    plan_id = db.Column(db.Integer, db.ForeignKey("plan.id"), nullable=False)
    starts_at = db.Column(db.DateTime, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    active = db.Column(db.Boolean, default=True)
    user = db.relationship("User")
    plan = db.relationship("Plan")

class Payment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    plan_id = db.Column(db.Integer, db.ForeignKey("plan.id"), nullable=False)
    order_id = db.Column(db.String(100), unique=True, nullable=False, index=True)
    amount = db.Column(db.Numeric(10,2), nullable=False)
    status = db.Column(db.String(20), default="pending")
    checkout_url = db.Column(db.Text)
    utr = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    paid_at = db.Column(db.DateTime)

class ProjectFile(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    filename = db.Column(db.String(255), nullable=False)
    stored_name = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Setting(db.Model):
    key = db.Column(db.String(80), primary_key=True)
    value = db.Column(db.Text, nullable=False)

def setting(key, default=""):
    row = db.session.get(Setting, key)
    return row.value if row else os.getenv(key.upper(), default)

def set_setting(key, value):
    row = db.session.get(Setting, key)
    if row: row.value = value
    else: db.session.add(Setting(key=key, value=value))
    db.session.commit()

def current_user():
    uid = session.get("uid")
    return db.session.get(User, uid) if uid else None

def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not current_user():
            return redirect(url_for("login"))
        if current_user().is_blocked:
            session.clear()
            flash("Account disabled. Contact administrator.", "error")
            return redirect(url_for("login"))
        return fn(*a, **kw)
    return wrapper

def admin_required(fn):
    @wraps(fn)
    @login_required
    def wrapper(*a, **kw):
        if not current_user().is_admin: abort(403)
        return fn(*a, **kw)
    return wrapper

def active_subscription(user):
    return Subscription.query.filter_by(user_id=user.id, active=True).filter(Subscription.expires_at > datetime.utcnow()).order_by(Subscription.expires_at.desc()).first()

def activate_subscription(payment):
    # Idempotent: a paid order can never create duplicate subscriptions.
    if payment.status == "paid": return
    plan = db.session.get(Plan, payment.plan_id)
    if not plan: raise ValueError("Plan missing")
    now = datetime.utcnow()
    payment.status, payment.paid_at = "paid", now
    sub = Subscription(user_id=payment.user_id, plan_id=plan.id, starts_at=now,
                       expires_at=now + timedelta(days=plan.duration_days), active=True)
    db.session.add(sub)
    db.session.commit()

def gateway_headers(api_key):
    return {"X-Api-Key": api_key, "Content-Type": "application/json", "Accept": "application/json"}

@app.context_processor
def inject_globals():
    return {"me": current_user(), "brand": "Aryan Cloud", "active_sub": active_subscription(current_user()) if current_user() else None}

@app.route("/")
def home():
    plans = Plan.query.filter_by(active=True).order_by(Plan.price).all()
    return render_template("home.html", plans=plans)

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name, email = request.form.get("name","").strip(), request.form.get("email","").strip().lower()
        password = request.form.get("password","")
        if len(name) < 2 or len(password) < 8:
            flash("Name and password (minimum 8 characters) are required.", "error")
        elif User.query.filter_by(email=email).first():
            flash("Email already registered.", "error")
        else:
            admin_email = os.getenv("ADMIN_EMAIL", "").lower()
            user = User(name=name, email=email, password_hash=bcrypt.generate_password_hash(password).decode(),
                        is_admin=bool(admin_email and email == admin_email))
            db.session.add(user); db.session.commit()
            session["uid"] = user.id
            return redirect(url_for("dashboard"))
    return render_template("auth.html", mode="register")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = User.query.filter_by(email=request.form.get("email","").strip().lower()).first()
        if user and bcrypt.check_password_hash(user.password_hash, request.form.get("password","")) and not user.is_blocked:
            session.clear(); session["uid"] = user.id
            return redirect(url_for("dashboard"))
        flash("Invalid login or account disabled.", "error")
    return render_template("auth.html", mode="login")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))

@app.route("/dashboard")
@login_required
def dashboard():
    user = current_user()
    plans = Plan.query.filter_by(active=True).order_by(Plan.price).all()
    files = ProjectFile.query.filter_by(user_id=user.id).order_by(ProjectFile.created_at.desc()).all()
    payments = Payment.query.filter_by(user_id=user.id).order_by(Payment.created_at.desc()).limit(10).all()
    sub = active_subscription(user)
    return render_template("dashboard.html", plans=plans, files=files, payments=payments, sub=sub,
                           runner_ready=bool(setting("runner_url")))

@app.route("/upload", methods=["POST"])
@login_required
def upload():
    f = request.files.get("file")
    if not f or not f.filename:
        flash("Choose a Python or ZIP file.", "error"); return redirect(url_for("dashboard"))
    name = secure_filename(f.filename)
    if not (name.endswith(".py") or name.endswith(".zip") or name == "requirements.txt"):
        flash("Only .py, .zip and requirements.txt are allowed.", "error"); return redirect(url_for("dashboard"))
    stored = secrets.token_hex(16) + "_" + name
    user_dir = UPLOAD_DIR / str(current_user().id)
    user_dir.mkdir(parents=True, exist_ok=True)
    f.save(user_dir / stored)
    db.session.add(ProjectFile(user_id=current_user().id, filename=name, stored_name=stored))
    db.session.commit()
    flash("File uploaded. Execution requires a configured isolated runner.", "success")
    return redirect(url_for("dashboard"))

@app.route("/file/<int:file_id>/delete", methods=["POST"])
@login_required
def delete_file(file_id):
    f = ProjectFile.query.filter_by(id=file_id, user_id=current_user().id).first_or_404()
    path = UPLOAD_DIR / str(current_user().id) / f.stored_name
    if path.exists(): path.unlink()
    db.session.delete(f); db.session.commit()
    flash("File deleted.", "success")
    return redirect(url_for("dashboard"))

@app.route("/file/<int:file_id>/run", methods=["POST"])
@login_required
def run_file(file_id):
    if not active_subscription(current_user()):
        flash("Active paid subscription required.", "error"); return redirect(url_for("dashboard"))
    f = ProjectFile.query.filter_by(id=file_id, user_id=current_user().id).first_or_404()
    runner_url, runner_token = setting("runner_url"), setting("runner_token")
    if not runner_url or not runner_token:
        flash("Python runtime is not enabled yet. Configure a dedicated isolated runner service first; this web service will not execute untrusted code directly.", "error")
        return redirect(url_for("dashboard"))
    # The runner endpoint must enforce container isolation, no host mounts, quotas and network policy.
    path = UPLOAD_DIR / str(current_user().id) / f.stored_name
    try:
        with open(path, "rb") as fp:
            response = requests.post(runner_url.rstrip("/") + "/v1/jobs",
                headers={"Authorization": "Bearer " + runner_token},
                files={"file": (f.filename, fp)}, data={"user_id": str(current_user().id)},
                timeout=20)
        response.raise_for_status()
        flash("Run request accepted by isolated runner.", "success")
    except requests.RequestException:
        flash("Runner unavailable. Check runner service configuration.", "error")
    return redirect(url_for("dashboard"))

@app.route("/subscribe/<int:plan_id>", methods=["POST"])
@login_required
def subscribe(plan_id):
    plan = Plan.query.filter_by(id=plan_id, active=True).first_or_404()
    base = setting("gateway_base_url", "https://famgateway.in").rstrip("/")
    api_key = setting("gateway_api_key")
    if not api_key:
        flash("Payment gateway is not configured. Contact administrator.", "error"); return redirect(url_for("dashboard"))
    order_ref = "AC" + secrets.token_hex(8).upper()
    amount = float(plan.price)
    payment = Payment(user_id=current_user().id, plan_id=plan.id, order_id=order_ref, amount=plan.price)
    db.session.add(payment); db.session.commit()
    payload = {"amount": amount, "customer_name": current_user().name,
               "customer_email": current_user().email,
               "redirect_url": url_for("payment_return", _external=True),
               "webhook_url": url_for("fam_webhook", _external=True)}
    try:
        r = requests.post(base + "/api/create-order", json=payload, headers=gateway_headers(api_key), timeout=20)
        r.raise_for_status()
        data = r.json().get("data", {})
        gateway_order = data.get("order_id")
        checkout = data.get("checkout_url")
        if r.json().get("status") != "success" or not gateway_order or not checkout:
            raise ValueError("Gateway did not return a valid checkout order.")
        payment.order_id = gateway_order
        payment.checkout_url = checkout
        # Store internal order ref in gateway order metadata is not supported by API; DB payment is bound to this user's plan.
        db.session.commit()
        return redirect(checkout)
    except Exception as exc:
        payment.status = "failed"; db.session.commit()
        app.logger.warning("FamGateway create order failed: %s", exc)
        flash("Could not create payment. Please retry or contact admin.", "error")
        return redirect(url_for("dashboard"))

def verify_gateway_order(order_id):
    base = setting("gateway_base_url", "https://famgateway.in").rstrip("/")
    key = setting("gateway_api_key")
    if not key: return False, {}
    try:
        r = requests.get(base + "/api/verify-order.php", params={"order_id": order_id},
                         headers={"X-Api-Key": key}, timeout=15)
        r.raise_for_status()
        data = r.json()
        return str(data.get("status","")).lower() == "success", data
    except Exception as exc:
        app.logger.warning("Gateway verify failed: %s", exc)
        return False, {}

@app.route("/payment-return")
@login_required
def payment_return():
    # The signed webhook is the fulfillment authority. Redirects are informational only.
    flash("Payment return received. Subscription activates after server-side gateway verification.", "success")
    return redirect(url_for("dashboard"))

@app.route("/api/fam-webhook", methods=["POST"])
def fam_webhook():
    raw = request.get_data()
    signature = request.headers.get("X-FamGateway-Signature", "")
    key = setting("gateway_api_key")
    if not key or not signature: return jsonify({"error":"missing signature"}), 401
    expected = hmac.new(key.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature): return jsonify({"error":"invalid signature"}), 401
    payload = request.get_json(silent=True) or {}
    if payload.get("event") != "payment.success": return jsonify({"status":"ignored"}), 200
    payment = Payment.query.filter_by(order_id=payload.get("order_id")).first()
    if not payment: return jsonify({"error":"unknown order"}), 404
    # Verify server-to-server as well as validating webhook signature; never trust browser redirect.
    ok, verified = verify_gateway_order(payment.order_id)
    if not ok: return jsonify({"error":"gateway verification pending"}), 409
    try:
        paid_amount = Decimal(str(verified.get("amount", verified.get("payable_amount", payload.get("amount", "0")))))
        if paid_amount != Decimal(str(payment.amount)): return jsonify({"error":"amount mismatch"}), 400
    except InvalidOperation:
        return jsonify({"error":"invalid amount"}), 400
    payment.utr = str(verified.get("utr", payload.get("utr","")))
    activate_subscription(payment)
    return jsonify({"status":"fulfilled"}), 200

@app.route("/admin", methods=["GET", "POST"])
@admin_required
def admin():
    plans = Plan.query.order_by(Plan.price).all()
    users = User.query.order_by(User.created_at.desc()).limit(100).all()
    payments = Payment.query.order_by(Payment.created_at.desc()).limit(100).all()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "gateway":
            set_setting("gateway_base_url", request.form.get("gateway_base_url","https://famgateway.in").strip())
            set_setting("gateway_api_key", request.form.get("gateway_api_key","").strip())
            set_setting("runner_url", request.form.get("runner_url","").strip())
            set_setting("runner_token", request.form.get("runner_token","").strip())
            flash("Settings saved.", "success")
        elif action == "plan":
            name = request.form.get("name","").strip()
            try:
                price = Decimal(request.form.get("price","0"))
                days, bots, ram, storage = [int(request.form.get(k,"0")) for k in ("days","bots","ram","storage")]
                if price < 0 or min(days,bots,ram,storage) < 1: raise ValueError()
                plan = Plan(name=name, price=price, duration_days=days, bot_limit=bots, ram_mb=ram, storage_mb=storage)
                db.session.add(plan); db.session.commit(); flash("Plan created.", "success")
            except Exception:
                db.session.rollback(); flash("Invalid plan details or duplicate name.", "error")
        return redirect(url_for("admin"))
    return render_template("admin.html", plans=plans, users=users, payments=payments,
        gateway_base=setting("gateway_base_url","https://famgateway.in"),
        gateway_key=setting("gateway_api_key"), runner_url=setting("runner_url"), runner_token=setting("runner_token"))

@app.route("/admin/plan/<int:plan_id>/toggle", methods=["POST"])
@admin_required
def toggle_plan(plan_id):
    p = db.session.get(Plan, plan_id) or abort(404)
    p.active = not p.active; db.session.commit()
    return redirect(url_for("admin"))

@app.route("/admin/user/<int:user_id>/toggle", methods=["POST"])
@admin_required
def toggle_user(user_id):
    u = db.session.get(User, user_id) or abort(404)
    if not u.is_admin: u.is_blocked = not u.is_blocked
    db.session.commit()
    return redirect(url_for("admin"))

@app.route("/health")
def health():
    return jsonify({"ok": True, "service": "Aryan Cloud"})

with app.app_context():
    db.create_all()
    if Plan.query.count() == 0:
        for name, price, bots, ram, disk in [
            ("Starter",49,1,256,512),("Pro",99,2,512,1024),
            ("Premium",199,5,1024,2048),("Ultra",399,10,2048,5120)]:
            db.session.add(Plan(name=name, price=price, duration_days=30, bot_limit=bots, ram_mb=ram, storage_mb=disk))
        db.session.commit()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
