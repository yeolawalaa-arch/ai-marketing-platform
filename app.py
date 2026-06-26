from flask import Flask, request, jsonify, render_template, session, redirect, url_for
from flask_compress import Compress
from werkzeug.middleware.proxy_fix import ProxyFix
from groq import Groq
from models import db, bcrypt, User, FREE_DAILY_LIMIT
import os, hmac, hashlib, random, smtplib, threading
from email.mime.text import MIMEText
from datetime import datetime, timedelta
import razorpay
import requests as http_req

app = Flask(__name__)
Compress(app)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.secret_key = os.environ.get("SECRET_KEY", "ai-marketing-secret-2024-change-in-prod")

@app.before_request
def force_https():
    if (os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RENDER")) and not request.is_secure:
        url = request.url.replace("http://", "https://", 1)
        return redirect(url, code=301)


# Database
db_url = os.environ.get("DATABASE_URL", "sqlite:///marketing.db")
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_pre_ping": True,
    "pool_recycle": 300,
    "connect_args": {"connect_timeout": 5} if db_url.startswith("postgresql") else {},
}

db.init_app(app)
bcrypt.init_app(app)

MODEL = "llama-3.3-70b-versatile"

RZP_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "")
RZP_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")
RZP_PLAN_ID = os.environ.get("RAZORPAY_PLAN_ID", "")
PRO_PRICE_INR = int(os.environ.get("PRO_PRICE_INR", "999"))

# DB init — run in background AND retry on every request until success
_db_ready = False

def _bg_init_db():
    global _db_ready
    import time
    for attempt in range(10):
        with app.app_context():
            try:
                db.create_all()
                _db_ready = True
                print("✅ Database tables created")
                return
            except Exception as e:
                print(f"⚠️ DB init attempt {attempt+1} failed: {e}")
                time.sleep(3)

threading.Thread(target=_bg_init_db, daemon=True).start()

@app.before_request
def ensure_tables():
    global _db_ready
    if not _db_ready:
        try:
            db.create_all()
            _db_ready = True
        except Exception:
            pass

# ─── Helpers ────────────────────────────────────────────────────────────────

def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return db.session.get(User, uid)

def clean_phone(phone):
    p = phone.strip().replace(" ", "").replace("-", "")
    if p.startswith("+91"):
        p = p[3:]
    elif p.startswith("91") and len(p) == 12:
        p = p[2:]
    return p

def send_otp_email(email, otp):
    gmail_user = os.environ.get("GMAIL_USER", "")
    gmail_pass = os.environ.get("GMAIL_APP_PASSWORD", "")
    body_html = f"""<div style="font-family:Arial;max-width:480px;margin:0 auto;padding:30px;background:#f9f9f9;border-radius:12px">
<h2 style="color:#667eea;margin:0 0 16px">AI Marketing Hub</h2>
<p style="color:#555;margin:0 0 20px">Your OTP code:</p>
<div style="background:#fff;border:2px solid #667eea;border-radius:10px;padding:20px;text-align:center;margin:0 0 20px">
<span style="font-size:42px;font-weight:bold;letter-spacing:12px;color:#333">{otp}</span>
</div>
<p style="color:#888;font-size:13px">This OTP expires in 10 minutes. Do not share it with anyone.</p>
</div>"""
    # Try Gmail SMTP first
    if gmail_user and gmail_pass:
        try:
            msg = MIMEText(body_html, "html")
            msg["Subject"] = f"Your OTP: {otp} — AI Marketing Hub"
            msg["From"] = f"AI Marketing Hub <{gmail_user}>"
            msg["To"] = email
            with smtplib.SMTP("smtp.gmail.com", 587) as server:
                server.starttls()
                server.login(gmail_user, gmail_pass)
                server.sendmail(gmail_user, email, msg.as_string())
            print(f"✅ Email sent via Gmail to {email}")
            return True, None
        except Exception as e:
            print(f"⚠️ Gmail failed: {e}, trying Resend...")
    # Fallback to Resend
    api_key = os.environ.get("RESEND_API_KEY", "")
    if not api_key:
        return False, "Email service not configured. Please contact admin."
    try:
        resp = http_req.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"from": "AI Marketing Hub <onboarding@resend.dev>",
                  "to": [email],
                  "subject": f"Your OTP: {otp} — AI Marketing Hub",
                  "html": body_html},
            timeout=15
        )
        if resp.status_code in (200, 201):
            print(f"✅ Email sent via Resend to {email}")
            return True, None
        print(f"❌ Resend error: {resp.text}")
        return False, f"Email failed: {resp.text}"
    except Exception as e:
        return False, str(e)

def send_otp_sms(phone, otp):
    key = os.environ.get("MSG91_KEY", "")
    sender = os.environ.get("MSG91_SENDER", "MKTGHB")
    if not key:
        return False, "SMS service not configured."
    try:
        # Ensure phone has country code
        if not phone.startswith("+"):
            phone = "91" + phone.lstrip("0")
        else:
            phone = phone.lstrip("+")
        resp = http_req.post(
            "https://api.msg91.com/api/v5/otp",
            headers={"Content-Type": "application/json"},
            json={
                "authkey": key,
                "mobile": phone,
                "otp": otp,
                "sender": sender,
                "otp_expiry": 10,
                "message": f"Your AI Marketing Hub OTP is {otp}. Valid for 10 minutes. Do not share with anyone.",
            },
            timeout=15
        )
        result = resp.json()
        if result.get("type") == "success":
            print(f"✅ SMS sent to {phone}")
            return True, None
        return False, result.get("message", "SMS failed")
    except Exception as e:
        return False, str(e)

def ask_ai(system_prompt, user_message, user=None):
    key = (user.groq_api_key if user else None) or os.environ.get("GROQ_API_KEY", "")
    if not key:
        return None, "No Groq API key configured."
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

# ─── Health Check ────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return "OK", 200

@app.route("/admin/set-pro", methods=["GET", "POST"])
def admin_set_pro():
    token = request.args.get("token") or (request.get_json() or {}).get("token", "")
    email = request.args.get("email") or (request.get_json() or {}).get("email", "")
    if token != app.secret_key:
        return "Forbidden", 403
    email = email.strip().lower()
    user = User.query.filter_by(email=email).first()
    if not user:
        return f"User '{email}' not found", 404
    user.plan = "pro"
    db.session.commit()
    return f"✅ Done! {user.name} is now PRO!", 200

# ─── Auth Routes ─────────────────────────────────────────────────────────────

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        data = request.get_json() or request.form
        name = str(data.get("name", "")).strip()
        email = str(data.get("email", "")).strip().lower()
        password = str(data.get("password", ""))
        phone_raw = str(data.get("phone", "")).strip()
        phone = clean_phone(phone_raw) if phone_raw else None

        if not name or not email or not password:
            return jsonify({"ok": False, "error": "Name, email and password are required"})
        if len(password) < 6:
            return jsonify({"ok": False, "error": "Password must be at least 6 characters"})
        if User.query.filter_by(email=email).first():
            return jsonify({"ok": False, "error": "This email is already registered"})
        if phone and User.query.filter_by(phone=phone).first():
            return jsonify({"ok": False, "error": "This phone number is already registered"})

        user = User(name=name, email=email, phone=phone or None)
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

# ─── OTP Routes ──────────────────────────────────────────────────────────────

@app.route("/send-otp", methods=["POST"])
def send_otp():
    data = request.get_json()
    target = str(data.get("target", "")).strip()
    flow = data.get("flow", "")  # 'login' or 'reset'

    is_phone = target.replace("+", "").replace("-", "").replace(" ", "").isdigit() and len(target.replace("+", "").replace("-", "").replace(" ", "")) >= 10
    phone = clean_phone(target) if is_phone else None

    if flow == "login":
        if not is_phone:
            return jsonify({"ok": False, "error": "Please enter a valid phone number"})
        user = User.query.filter_by(phone=phone).first()
        if not user:
            return jsonify({"ok": False, "error": "No account found with this number. Please register first."})
    elif flow == "reset":
        if is_phone:
            user = User.query.filter_by(phone=phone).first()
        else:
            user = User.query.filter_by(email=target.lower()).first()
        if not user:
            return jsonify({"ok": False, "error": "No account found with this email or phone"})
    else:
        return jsonify({"ok": False, "error": "Invalid request"})

    otp = str(random.randint(100000, 999999))
    user.otp_code = otp
    user.otp_expiry = datetime.utcnow() + timedelta(minutes=10)
    db.session.commit()

    if is_phone:
        ok, err = send_otp_sms(phone, otp)
    else:
        ok, err = send_otp_email(target.lower(), otp)

    if not ok:
        return jsonify({"ok": False, "error": err or "Failed to send OTP. Please try again."})
    return jsonify({"ok": True})

@app.route("/verify-otp", methods=["POST"])
def verify_otp():
    data = request.get_json()
    target = str(data.get("target", "")).strip()
    otp = str(data.get("otp", "")).strip()
    flow = data.get("flow", "")

    is_phone = target.replace("+", "").replace("-", "").replace(" ", "").isdigit() and len(target.replace("+", "").replace("-", "").replace(" ", "")) >= 10
    phone = clean_phone(target) if is_phone else None

    user = User.query.filter_by(phone=phone).first() if is_phone else User.query.filter_by(email=target.lower()).first()

    if not user:
        return jsonify({"ok": False, "error": "User not found"})
    if not user.otp_code or user.otp_code != otp:
        return jsonify({"ok": False, "error": "Invalid OTP. Please check and try again."})
    if user.otp_expiry and datetime.utcnow() > user.otp_expiry:
        return jsonify({"ok": False, "error": "OTP has expired. Please request a new one."})

    user.otp_code = None
    user.otp_expiry = None

    if flow == "login":
        db.session.commit()
        session["user_id"] = user.id
        return jsonify({"ok": True, "action": "redirect"})
    elif flow == "reset":
        reset_token = str(random.randint(10000000, 99999999))
        session["reset_uid"] = user.id
        session["reset_token"] = reset_token
        db.session.commit()
        return jsonify({"ok": True, "action": "new_password", "token": reset_token})

    return jsonify({"ok": False, "error": "Invalid flow"})

@app.route("/reset-password", methods=["POST"])
def reset_password():
    data = request.get_json()
    token = data.get("token", "")
    new_password = data.get("password", "")

    if session.get("reset_token") != token:
        return jsonify({"ok": False, "error": "Session expired. Please try again."})
    uid = session.get("reset_uid")
    if not uid:
        return jsonify({"ok": False, "error": "Session not found. Please try again."})
    if len(new_password) < 6:
        return jsonify({"ok": False, "error": "Password must be at least 6 characters"})

    user = db.session.get(User, uid)
    if not user:
        return jsonify({"ok": False, "error": "User not found"})

    user.set_password(new_password)
    session.pop("reset_uid", None)
    session.pop("reset_token", None)
    session["user_id"] = user.id
    db.session.commit()
    return jsonify({"ok": True})

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
        client.chat.completions.create(model=MODEL, max_tokens=5, messages=[{"role": "user", "content": "hi"}])
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

@app.route("/about")
def about():
    return render_template("about.html")

# ─── Razorpay Payments ────────────────────────────────────────────────────────

@app.route("/create-subscription", methods=["POST"])
def create_subscription():
    user = current_user()
    if not user:
        return jsonify({"ok": False, "error": "Not logged in"})
    if not RZP_KEY_ID or not RZP_PLAN_ID:
        return jsonify({"ok": False, "error": "Razorpay not configured yet."})
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
            user = db.session.get(User, int(uid))
            if user:
                user.plan = "pro"
                db.session.commit()
    if event.get("event") in ("subscription.cancelled", "subscription.expired"):
        notes = event["payload"]["subscription"]["entity"].get("notes", {})
        uid = notes.get("user_id")
        if uid:
            user = db.session.get(User, int(uid))
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
                        "message": f"Free plan limit reached ({FREE_DAILY_LIMIT}/day). Upgrade to Pro!"})
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
    return ai_route(system, prompts.get(data.get("task_type", "keywords"), prompts["keywords"]))

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

@app.route("/api/image-ask", methods=["POST"])
def image_ask():
    user = current_user()
    if not user:
        return jsonify({"ok": False, "error": "Not logged in"})
    if not user.can_use_ai():
        return jsonify({"ok": False, "error": "limit_reached"})

    data = request.get_json()
    image_b64 = data.get("image", "")
    mime = data.get("mime", "image/jpeg")
    question = data.get("question", "What can I do with this image? Suggest AI prompts and ideas.")

    key = (user.groq_api_key if user else None) or os.environ.get("GROQ_API_KEY", "")
    if not key:
        return jsonify({"ok": False, "error": "No Groq API key configured."})

    try:
        client = Groq(api_key=key)
        completion = client.chat.completions.create(
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image_b64}"}},
                    {"type": "text", "text": question}
                ]
            }]
        )
        result = completion.choices[0].message.content
        user.increment_usage()
        return jsonify({"ok": True, "result": result,
                        "plan": user.plan, "usage": user.usage_remaining()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/ask", methods=["POST"])
def ask_anything():
    data = request.get_json()
    question = data.get("question", "")
    system = """You are a knowledgeable, friendly AI assistant who can answer ANY question clearly and helpfully.
Whether it's recipes, how-to guides, science, history, cooking, DIY, fitness, business, technology, travel, art, or anything else — you always give a clear, detailed, and useful answer.
Use simple language. Format your response with bullet points or steps when appropriate. Be warm and helpful."""
    return ai_route(system, question)

@app.route("/api/prompt", methods=["POST"])
def generate_prompt():
    data = request.get_json()
    tool = data.get("tool", "chatgpt")
    purpose = data.get("purpose", "marketing content")
    topic = data.get("topic", "")
    style = data.get("style", "")

    tool_guides = {
        "chatgpt": "ChatGPT or Claude (text AI). Write a detailed, structured text prompt.",
        "midjourney": "Midjourney image AI. Write a visual prompt with art style, lighting, mood, camera angle, subject details. Use comma-separated descriptors. End with --ar 1:1 or --ar 16:9 or --ar 9:16 depending on use.",
        "dalle": "DALL-E image AI. Write a clear, descriptive visual prompt specifying style, subject, colors, lighting, composition.",
        "stable": "Stable Diffusion. Write a highly detailed visual prompt with positive and negative prompts. Include style tags like (photorealistic:1.4), lighting details, artist references.",
        "runway": "Runway or Sora video AI. Write a scene description with camera movement, action, setting, mood, duration cues.",
        "heygen": "HeyGen avatar video. Write a script with natural conversational language, clear sections, call-to-action.",
        "general": "a general AI tool. Write a clear, detailed, and effective prompt."
    }
    guide = tool_guides.get(tool, tool_guides["general"])

    system = f"""You are an expert AI prompt engineer specializing in marketing.
Your job is to write the PERFECT prompt for {guide}
The prompt must be ready to copy-paste directly into the AI tool — no explanation needed, just the prompt itself.
Make it specific, detailed, and highly effective for marketing purposes."""

    user_msg = f"""Create a prompt for: {purpose}
Topic/Details: {topic}
{f'Style/Tone: {style}' if style else ''}

Write ONLY the final prompt — no intro, no explanation, no quotes around it. Just the pure ready-to-use prompt."""

    return ai_route(system, user_msg)

@app.errorhandler(404)
def not_found(e):
    return render_template("404.html", user=current_user()), 404

@app.errorhandler(500)
def server_error(e):
    return render_template("404.html", user=current_user(), error=500), 500

if __name__ == "__main__":
    print("\n🚀 AI Marketing Platform starting...")
    print("📍 http://localhost:5050\n")
    app.run(debug=False, port=5050, host="0.0.0.0")
