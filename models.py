from flask_sqlalchemy import SQLAlchemy
from flask_bcrypt import Bcrypt
from datetime import date, datetime

db = SQLAlchemy()
bcrypt = Bcrypt()

FREE_DAILY_LIMIT = 10

class User(db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=True)
    phone = db.Column(db.String(20), unique=True, nullable=True)
    name = db.Column(db.String(100), nullable=False)
    password_hash = db.Column(db.String(256), nullable=True)
    plan = db.Column(db.String(20), default="free")
    daily_usage = db.Column(db.Integer, default=0)
    usage_reset_date = db.Column(db.Date, default=date.today)
    razorpay_subscription_id = db.Column(db.String(100))
    razorpay_customer_id = db.Column(db.String(100))
    groq_api_key = db.Column(db.String(200))
    otp_code = db.Column(db.String(6), nullable=True)
    otp_expiry = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, password):
        self.password_hash = bcrypt.generate_password_hash(password).decode("utf-8")

    def check_password(self, password):
        if not self.password_hash:
            return False
        return bcrypt.check_password_hash(self.password_hash, password)

    def can_use_ai(self):
        if self.plan == "pro":
            return True
        self._reset_usage_if_new_day()
        return self.daily_usage < FREE_DAILY_LIMIT

    def increment_usage(self):
        self._reset_usage_if_new_day()
        self.daily_usage += 1
        db.session.commit()

    def usage_remaining(self):
        if self.plan == "pro":
            return 999
        self._reset_usage_if_new_day()
        return max(0, FREE_DAILY_LIMIT - self.daily_usage)

    def _reset_usage_if_new_day(self):
        today = date.today()
        if self.usage_reset_date != today:
            self.daily_usage = 0
            self.usage_reset_date = today
            db.session.commit()
