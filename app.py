from flask import Flask, request, jsonify, render_template, session, redirect, url_for, flash
from werkzeug.middleware.proxy_fix import ProxyFix
from groq import Groq
from models import db, bcrypt, User, FREE_DAILY_LIMIT
import os
import razorpay
import hmac
import hashlib

app = Flask(__name__)
# Trust Railway/Heroku reverse proxy so HTTPS works correctly
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.secret_key = os.environ.get("SECRET_KEY", "ai-marketing-secret-2024-change-in-prod")

# Force HTTPS in production
@app.before_request
def force_https():
    if os.environ.get("RAILWAY_ENVIRONMENT") and not request.is_secure:
        url = request.url.replace("http://", "https://", 1)
        return redirect(url, code=301)

# Database
db_url = os.environ.get("DATABASE_URL", "sqlite:///marketing.db")
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)
bcrypt.init_app(app)

MODEL = "llama-3.3-70b-versatile"

# Razorpay config (set these in environment variables)
RZP_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "")
RZP_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")
RZP_PLAN_ID = os.environ.get("RAZORPAY_PLAN_ID", "")  # Create in Razorpay dashboard
PRO_PRICE_INR = int(os.environ.get("PRO_PRICE_INR", "999"))

with app.app_context():
    db.create_all()

# ─── Helpers ────────────────────────────────────────────────────────────────

def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return User.query.get(uid)

def require_login():
    if not current_user():
        return redirect(url_for("login"))
    return None

def ask_ai(system_prompt, user_message, user=None):
    key = (user.groq_api_key if user else None) or os.environ.get("GROQ_API_KEY", "")
    if not key:
        return None, "No Groq API key configured. Please add it in your account settings."
    try:
        client = Groq(api_key=key)
        completion = client.chat.completions.create(
            model=MODEL,
            max_tokens=2048,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ]
        )
        return completion.choices[0].message.content, None
    except Exception as e:
        return None, str(e)

# ─── Auth Routes ─────────────────────────────────────────────────────────────

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        data = request.get_json() or request.form
        name = str(data.get("name", "")).strip()
        email = str(data.get("email", "")).strip().lower()
        password = str(data.get("password", ""))
        if not name or not email or not password:
            return jsonify({"ok": False, "error": "All fields required"})
        if User.query.filter_by(email=email).first():
            return jsonify({"ok": False, "error": "Email already registered"})
        user = User(name=name, email=email)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        session["user_id"] = user.id
        return jsonify({"ok": True})
    return render_template("auth.html", mode="register")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        data = request.get_json() or request.form
        email = str(data.get("email", "")).strip().lower()
        password = str(data.get("password", ""))
        user = User.query.filter_by(email=email).first()
        if not user or not user.check_password(password):
            return jsonify({"ok": False, "error": "Invalid email or password"})
        session["user_id"] = user.id
        return jsonify({"ok": True})
    return render_template("auth.html", mode="login")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/save-groq-key", methods=["POST"])
def save_groq_key():
    user = current_user()
    if not user:
        return jsonify({"ok": False, "error": "Not logged in"})
    key = (request.get_json() or {}).get("api_key", "").strip()
    if not key:
        return jsonify({"ok": False, "error": "Empty key"})
    try:
        client = Groq(api_key=key)
        client.chat.completions.create(
            model=MODEL, max_tokens=5,
            messages=[{"role": "user", "content": "hi"}]
        )
        user.groq_api_key = key
        db.session.commit()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

# ─── Main App ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    user = current_user()
    if not user:
        return redirect(url_for("login"))
    needs_key = not user.groq_api_key and not os.environ.get("GROQ_API_KEY")
    return render_template("index.html", user=user, needs_key=needs_key,
                           free_limit=FREE_DAILY_LIMIT,
                           rzp_key=RZP_KEY_ID, pro_price=PRO_PRICE_INR)

@app.route("/pricing")
def pricing():
    user = current_user()
    return render_template("pricing.html", user=user, pro_price=PRO_PRICE_INR)

# ─── Razorpay Payments ────────────────────────────────────────────────────────

@app.route("/create-subscription", methods=["POST"])
def create_subscription():
    user = current_user()
    if not user:
        return jsonify({"ok": False, "error": "Not logged in"})
    if not RZP_KEY_ID or not RZP_PLAN_ID:
        return jsonify({"ok": False, "error": "Razorpay not configured yet. Add keys in environment variables."})
    try:
        client = razorpay.Client(auth=(RZP_KEY_ID, RZP_KEY_SECRET))
        subscription = client.subscription.create({
            "plan_id": RZP_PLAN_ID,
            "customer_notify": 1,
            "total_count": 12,
            "notes": {"user_id": str(user.id), "email": user.email}
        })
        user.razorpay_subscription_id = subscription["id"]
        db.session.commit()
        return jsonify({"ok": True, "subscription_id": subscription["id"]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/verify-payment", methods=["POST"])
def verify_payment():
    user = current_user()
    if not user:
        return jsonify({"ok": False})
    data = request.get_json()
    try:
        sig = hmac.new(
            RZP_KEY_SECRET.encode(),
            f"{data['razorpay_payment_id']}|{data['razorpay_subscription_id']}".encode(),
            hashlib.sha256
        ).hexdigest()
        if sig == data["razorpay_signature"]:
            user.plan = "pro"
            db.session.commit()
            return jsonify({"ok": True})
    except Exception:
        pass
    return jsonify({"ok": False, "error": "Payment verification failed"})

@app.route("/razorpay-webhook", methods=["POST"])
def razorpay_webhook():
    body = request.get_data()
    sig = request.headers.get("X-Razorpay-Signature", "")
    expected = hmac.new(RZP_KEY_SECRET.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return "Invalid", 400
    event = request.get_json()
    if event.get("event") == "subscription.activated":
        notes = event["payload"]["subscription"]["entity"].get("notes", {})
        uid = notes.get("user_id")
        if uid:
            user = User.query.get(int(uid))
            if user:
                user.plan = "pro"
                db.session.commit()
    if event.get("event") in ("subscription.cancelled", "subscription.expired"):
        notes = event["payload"]["subscription"]["entity"].get("notes", {})
        uid = notes.get("user_id")
        if uid:
            user = User.query.get(int(uid))
            if user:
                user.plan = "free"
                db.session.commit()
    return "OK", 200

# ─── AI API Routes ────────────────────────────────────────────────────────────

def ai_route(system, user_msg):
    user = current_user()
    if not user:
        return jsonify({"ok": False, "error": "Not logged in"})
    if not user.can_use_ai():
        return jsonify({"ok": False, "error": "limit_reached",
                        "message": f"Free plan limit reached ({FREE_DAILY_LIMIT}/day). Upgrade to Pro for unlimited access!"})
    result, error = ask_ai(system, user_msg, user)
    if error:
        return jsonify({"ok": False, "error": error})
    user.increment_usage()
    return jsonify({"ok": True, "result": result,
                    "usage": user.usage_remaining(), "plan": user.plan})

@app.route("/api/content", methods=["POST"])
def generate_content():
    data = request.get_json()
    content_type = data.get("type", "caption")
    topic = data.get("topic", "")
    tone = data.get("tone", "professional")
    platform = data.get("platform", "Instagram")
    extra = data.get("extra", "")
    prompts = {
        "caption": f"Write 5 engaging {platform} captions about: {topic}. Tone: {tone}. {extra}. Format each with emojis and relevant hashtags.",
        "blog": f"Write a full SEO-optimised blog post about: {topic}. Tone: {tone}. {extra}. Include: catchy title, intro, 3-4 sections with subheadings, and a conclusion with CTA.",
        "ad_copy": f"Write 3 high-converting ad copies for: {topic}. Platform: {platform}. Tone: {tone}. {extra}. Each: headline, body (2-3 lines), CTA.",
        "email": f"Write a compelling marketing email about: {topic}. Tone: {tone}. {extra}. Include: subject line, preview text, greeting, body, CTA, sign-off.",
        "hashtags": f"Generate 30 best hashtags for a {platform} post about: {topic}. Group: Popular (10), Medium (10), Niche (10).",
        "product_desc": f"Write 3 compelling product descriptions for: {topic}. Tone: {tone}. {extra}. Each: 2-3 sentences, key benefits, urgency.",
    }
    system = "You are an expert digital marketing AI assistant. Respond with well-structured, ready-to-use content. Use markdown formatting."
    return ai_route(system, prompts.get(content_type, prompts["caption"]))

@app.route("/api/campaign", methods=["POST"])
def plan_campaign():
    data = request.get_json()
    system = "You are a senior digital marketing strategist. Create detailed, actionable marketing campaigns."
    user_msg = f"""Create a complete digital marketing campaign for:
Business: {data.get('business','')}
Goal: {data.get('goal','')}
Budget: {data.get('budget','')}
Duration: {data.get('duration','30 days')}
Platforms: {data.get('platforms','')}

Include: Campaign strategy, week-by-week plan, content calendar, budget breakdown, KPIs, expected results, quick wins for first 7 days.
Format with headings, bullets, and tables."""
    return ai_route(system, user_msg)

@app.route("/api/seo", methods=["POST"])
def seo_research():
    data = request.get_json()
    topic = data.get("topic", "")
    industry = data.get("industry", "")
    prompts = {
        "keywords": f"SEO keyword research for: {topic} in {industry}. List 30 keywords with: keyword, intent, difficulty, content idea. Markdown table.",
        "competitor": f"Competitor SEO analysis for {industry} targeting {topic}. Keywords they rank for, content gaps, backlink strategy, 5 quick wins.",
        "meta": f"SEO meta titles + descriptions for 5 pages about {topic} in {industry}. Each: title (≤60 chars), description (≤155 chars), keyword.",
        "audit": f"SEO audit checklist for a {industry} website. Rate each by impact (H/M/L) and effort (Easy/Med/Hard). Technical, on-page, content, off-page.",
    }
    system = "You are an expert SEO strategist. Give specific, actionable advice. Use markdown tables."
    return ai_route(system, prompts.get(data.get("task_type","keywords"), prompts["keywords"]))

@app.route("/api/report", methods=["POST"])
def generate_report():
    data = request.get_json()
    system = "You are a professional digital marketing reporting specialist. Write polished, client-ready reports."
    user_msg = f"""Professional client marketing report:
Client: {data.get('client_name','')} | Period: {data.get('period','')}
Metrics: {data.get('metrics','')}
Wins: {data.get('wins','')}
Next steps: {data.get('next_steps','')}

Format: Executive Summary, Performance Highlights (table), What Worked, Challenges, Next Month Strategy, Recommendations, Closing note."""
    return ai_route(system, user_msg)

@app.route("/api/strategy", methods=["POST"])
def quick_strategy():
    data = request.get_json()
    system = """You are a brilliant digital marketing AI advisor. Help with: social media, content, branding, paid ads, SEO, email, video, influencer outreach, business growth.
Give specific, actionable advice. Use bullet points and clear structure."""
    return ai_route(system, data.get("question", ""))

@app.errorhandler(404)
def not_found(e):
    user = current_user()
    return render_template("404.html", user=user), 404

@app.errorhandler(500)
def server_error(e):
    return render_template("404.html", user=current_user(), error=500), 500

if __name__ == "__main__":
    print("\n🚀 AI Marketing Platform starting...")
    print("📍 http://localhost:5050\n")
    app.run(debug=False, port=5050, host="0.0.0.0")
