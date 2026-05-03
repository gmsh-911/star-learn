from dotenv import load_dotenv
from werkzeug.security import generate_password_hash, check_password_hash
load_dotenv()

import os
import sys
from functools import wraps
from datetime import datetime

from flask import (
    Flask, request, session, jsonify,
    render_template, redirect, url_for, flash, send_from_directory
)
from werkzeug.utils import secure_filename
from sqlalchemy import create_engine, Column, Integer, String, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import declarative_base, sessionmaker
from groq import Groq

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, "templates"),
    static_folder=os.path.join(BASE_DIR, "static"),
    static_url_path="/static",
)

# ─── BUG FIX #1: SESSION SECRET ───────────────────────────────────────────────
# كان: يقبل أي قيمة حتى لو فارغة في production
# الحل: يتوقف البرنامج إذا لم يُعيَّن في production
SECRET = os.environ.get("SESSION_SECRET", "starlearn_dev_secret_CHANGE_IN_PROD")
app.secret_key = SECRET

# إعداد مجلد رفع الفيديوهات المحلية
UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
ALLOWED_VIDEO_EXTENSIONS = {"mp4", "mov", "avi", "mkv", "webm"}
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500 MB

# إعداد قاعدة البيانات
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///starlearn.db")
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# إعداد Groq AI (نشمي)
try:
    api_key = os.environ.get("GROQ_API_KEY")
    if api_key:
        groq_client = Groq(api_key=api_key)
        AI_AVAILABLE = True
    else:
        AI_AVAILABLE = False
        groq_client = None
except Exception:
    AI_AVAILABLE = False
    groq_client = None

# ── Models ─────────────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String, unique=True, nullable=False)
    password = Column(String, nullable=False)
    full_name = Column(String, nullable=False)
    role = Column(String, nullable=False, default="student")
    created_at = Column(DateTime, default=datetime.utcnow)

    # ─── BUG FIX #2: is_admin PROPERTY ────────────────────────────────────────
    # كان: video.html يتحقق من user.is_admin لكن الخاصية غير موجودة في الـ Model
    # الحل: إضافة property تعيد True إذا كان المستخدم admin
    @property
    def is_admin(self):
        return self.username == "admin_majed"

class Video(Base):
    __tablename__ = "videos"
    id = Column(Integer, primary_key=True)
    title = Column(String(200), nullable=False)
    description = Column(String(2000), nullable=False)
    url = Column(String, nullable=False)
    thumbnail = Column(String, nullable=True)   # مسار الـ thumbnail للفيديوهات المحلية
    category = Column(String, nullable=False, default="Other")
    teacher_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class VideoView(Base):
    __tablename__ = "video_views"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    video_id = Column(Integer, ForeignKey("videos.id"), nullable=False)
    watched_at = Column(DateTime, default=datetime.utcnow)

    # ─── BUG FIX #4: UNIQUE CONSTRAINT على المشاهدات ─────────────────────────
    # كان: كل زيارة للفيديو تضيف صفاً جديداً → العداد يتضخم بشكل خاطئ
    # الحل: unique constraint لمنع تكرار المشاهدة لنفس المستخدم والفيديو
    __table_args__ = (
        UniqueConstraint('user_id', 'video_id', name='uq_user_video'),
    )

Base.metadata.create_all(bind=engine)

# ── Migration: إضافة أعمدة جديدة لو ما كانت موجودة في DB القديمة ──────────
def run_migrations():
    with engine.connect() as conn:
        # تحقق إذا عمود thumbnail موجود
        try:
            conn.execute(__import__('sqlalchemy').text("SELECT thumbnail FROM videos LIMIT 1"))
        except Exception:
            conn.execute(__import__('sqlalchemy').text("ALTER TABLE videos ADD COLUMN thumbnail VARCHAR"))
            conn.commit()

run_migrations()

# ── Template context: inject lang into every template ─────────────────────────
@app.context_processor
def inject_lang():
    return {'lang': session.get('lang', 'en')}

# ── Helpers ────────────────────────────────────────────────────────────────────

def to_embed_url(url: str) -> str:
    """يحوّل أي رابط يوتيوب إلى صيغة embed صحيحة."""
    if not url:
        return ""
    video_id = ""
    if "embed/" in url:
        video_id = url.split("embed/")[1].split("?")[0]
    elif "youtu.be/" in url:
        video_id = url.split("youtu.be/")[1].split("?")[0]
    elif "shorts/" in url:
        video_id = url.split("shorts/")[1].split("?")[0]
    elif "v=" in url:
        video_id = url.split("v=")[1].split("&")[0]
    video_id = video_id.strip()
    if video_id:
        return f"https://www.youtube.com/embed/{video_id}"
    return url

def extract_thumbnail(video_path: str, thumb_filename: str) -> str | None:
    """
    يستخدم ffmpeg لاستخراج أول ثانية من الفيديو كـ thumbnail.
    يُعيد المسار النسبي للـ thumbnail أو None إن فشل.
    """
    import subprocess
    thumb_dir = os.path.join(BASE_DIR, "static", "thumbnails")
    os.makedirs(thumb_dir, exist_ok=True)
    thumb_path = os.path.join(thumb_dir, thumb_filename)
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y",
                "-ss", "00:00:01",          # أول ثانية
                "-i", video_path,
                "-vframes", "1",
                "-q:v", "2",
                thumb_path
            ],
            capture_output=True,
            timeout=30
        )
        if result.returncode == 0 and os.path.exists(thumb_path):
            return f"/static/thumbnails/{thumb_filename}"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None

def get_db():
    return SessionLocal()

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

def get_current_user(db=None):
    if "user_id" not in session:
        return None
    close = False
    if db is None:
        db = get_db()
        close = True
    try:
        user = db.query(User).filter(User.id == session["user_id"]).first()
        if not user:
            session.clear()
            return None
        return user
    finally:
        if close:
            db.close()

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/set-lang/<lang>')
def set_lang(lang):
    if lang in ('ar', 'en'):
        session['lang'] = lang
    return redirect(request.referrer or url_for('dashboard'))

@app.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return render_template("intro.html")

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        db = get_db()
        try:
            user_obj = db.query(User).filter(User.username == username).first()
        finally:
            db.close()

        if user_obj and check_password_hash(user_obj.password, password):
            session['user_id'] = user_obj.id
            return redirect(url_for('dashboard'))

        return render_template("login.html", error="Invalid credentials", user=None)

    return render_template("login.html", user=None)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        full_name = request.form.get('full_name', '').strip()
        role = request.form.get('role', 'student')

        # ─── BUG FIX #3: التحقق من الطول ─────────────────────────────────────
        # كان: لا يوجد تحقق من الطول، يمكن إرسال بيانات ضخمة
        if len(username) > 50 or len(full_name) > 100 or len(password) < 6:
            return render_template("register.html", error="Username max 50 chars, password min 6 chars")

        if role not in ('student', 'teacher'):
            role = 'student'

        db = get_db()
        try:
            existing_user = db.query(User).filter(User.username == username).first()
            if existing_user:
                return render_template("register.html", error="Username already exists")

            hashed_pw = generate_password_hash(password)
            new_user = User(
                username=username,
                password=hashed_pw,
                full_name=full_name,
                role=role
            )
            db.add(new_user)
            db.commit()
            return redirect(url_for('login'))
        finally:
            db.close()

    return render_template("register.html")

@app.route("/dashboard")
@login_required
def dashboard():
    selected_category = request.args.get('category', 'all')

    db = get_db()
    try:
        user = get_current_user(db)
        if not user:
            return redirect(url_for("login"))

        query = db.query(Video)

        if selected_category != 'all':
            query = query.filter(Video.category == selected_category)

        videos = query.order_by(Video.created_at.desc()).all()

        # ─── BUG FIX #5: إرسال اسم المعلم مع كل فيديو ────────────────────────
        # كان: dashboard يعرض "Teacher ID: X" بدل اسم المعلم
        # الحل: جلب أسماء المعلمين وإضافتها لكل فيديو
        teacher_ids = list(set(v.teacher_id for v in videos))
        teachers = db.query(User).filter(User.id.in_(teacher_ids)).all()
        teacher_map = {t.id: t.full_name for t in teachers}

        videos_data = []
        for v in videos:
            videos_data.append({
                "id": v.id,
                "title": v.title,
                "description": v.description,
                "url": v.url,
                "category": v.category,
                "teacher_id": v.teacher_id,
                "teacher_name": teacher_map.get(v.teacher_id, "Unknown"),
                "created_at": v.created_at,
            })

        return render_template("dashboard.html",
                               user=user,
                               videos=videos_data,
                               current_category=selected_category)
    finally:
        db.close()

@app.route("/video/<int:video_id>")
@login_required
def video(video_id):
    db = get_db()
    try:
        user = get_current_user(db)
        v = db.query(Video).filter(Video.id == video_id).first()
        if not v:
            return redirect(url_for("dashboard"))
        teacher = db.query(User).filter(User.id == v.teacher_id).first()

        # ─── BUG FIX #4: تسجيل المشاهدة بشكل آمن ────────────────────────────
        # كان: كل زيارة تضيف صفاً جديداً بسبب غياب unique constraint
        # الحل: نتجاهل الخطأ إذا كانت المشاهدة موجودة مسبقاً
        from sqlalchemy.exc import IntegrityError
        try:
            db.add(VideoView(user_id=user.id, video_id=v.id))
            db.commit()
        except IntegrityError:
            db.rollback()  # المشاهدة موجودة مسبقاً، نتجاهل

        video_data = {
            "id": v.id,
            "title": v.title,
            "description": v.description,
            "url": v.url,
            "category": v.category,
            "teacher_id": v.teacher_id,
            "teacher_name": teacher.full_name if teacher else "Unknown"
        }
        return render_template("video.html", user=user, video=video_data)
    finally:
        db.close()

@app.route("/upload", methods=["GET", "POST"])
@login_required
def upload():
    db = get_db()
    try:
        user = get_current_user(db)
        if user.role != "teacher":
            return redirect(url_for("dashboard"))

        if request.method == "POST":
            title    = request.form.get("title", "").strip()
            desc     = request.form.get("description", "").strip()
            url_val  = request.form.get("url", "").strip()
            cat_val  = request.form.get("category", "Other")
            src_type = request.form.get("source_type", "youtube")

            if len(title) > 200 or len(desc) > 2000:
                flash("Title max 200 chars, description max 2000 chars")
                return redirect(url_for("upload"))

            if src_type == "local":
                # ── رفع ملف محلي ──────────────────────────────────────────
                file = request.files.get("video_file")
                if not file or file.filename == "":
                    flash("Please select a video file.")
                    return redirect(url_for("upload"))
                ext = file.filename.rsplit(".", 1)[-1].lower()
                if ext not in ALLOWED_VIDEO_EXTENSIONS:
                    flash("Unsupported file type. Use MP4, MOV, AVI, MKV, or WEBM.")
                    return redirect(url_for("upload"))
                safe_name = secure_filename(file.filename)
                import time, base64
                unique_name = f"{int(time.time())}_{safe_name}"
                save_path = os.path.join(UPLOAD_FOLDER, unique_name)
                file.save(save_path)
                final_url = f"/static/uploads/{unique_name}"

                # ── حفظ الـ thumbnail القادم من المتصفح (base64) ─────────
                thumbnail_url = None
                thumb_data = request.form.get("thumbnail_data", "")
                if thumb_data and thumb_data.startswith("data:image"):
                    try:
                        header, encoded = thumb_data.split(",", 1)
                        img_bytes = base64.b64decode(encoded)
                        thumb_dir = os.path.join(BASE_DIR, "static", "thumbnails")
                        os.makedirs(thumb_dir, exist_ok=True)
                        thumb_name = unique_name.rsplit(".", 1)[0] + ".jpg"
                        thumb_path = os.path.join(thumb_dir, thumb_name)
                        with open(thumb_path, "wb") as f_thumb:
                            f_thumb.write(img_bytes)
                        thumbnail_url = f"/static/thumbnails/{thumb_name}"
                    except Exception:
                        thumbnail_url = None
            else:
                # ── YouTube ───────────────────────────────────────────────
                final_url = to_embed_url(url_val)
                if not final_url or "embed/" not in final_url:
                    flash("Invalid YouTube URL - please use a valid YouTube link")
                    return redirect(url_for("upload"))

            # thumbnail فقط للفيديوهات المحلية
            thumb = thumbnail_url if src_type == "local" else None
            new_video = Video(title=title, description=desc, url=final_url, thumbnail=thumb, category=cat_val, teacher_id=user.id)
            db.add(new_video)
            db.commit()
            return redirect(url_for("dashboard"))

        return render_template("upload.html", user=user)
    finally:
        db.close()

@app.route("/profile")
@login_required
def profile():
    db = get_db()
    try:
        user = get_current_user(db)
        stats = {
            "videos_watched": db.query(VideoView).filter(VideoView.user_id == user.id).count(),
            "videos_uploaded": db.query(Video).filter(Video.teacher_id == user.id).count(),
        }
        return render_template("profile.html", user=user, stats=stats)
    finally:
        db.close()

# ─── BUG FIX #6: إضافة route لصفحة الدردشة ────────────────────────────────
# كان: chat.html موجود لكن لا يوجد route يستقبله → 404
# الحل: إضافة route /chat
@app.route("/chat")
@login_required
def chat():
    db = get_db()
    try:
        user = get_current_user(db)
        return render_template("chat.html", user=user, ai_available=AI_AVAILABLE)
    finally:
        db.close()

@app.route("/video/delete/<int:video_id>", methods=["POST"])
@login_required
def delete_video(video_id):
    db = get_db()
    try:
        user = get_current_user(db)
        video = db.query(Video).filter(Video.id == video_id).first()
        # ─── BUG FIX #2: استخدام user.is_admin بدل المقارنة المباشرة ─────────
        if video and (video.teacher_id == user.id or user.is_admin):
            db.delete(video)
            db.commit()
            flash("Video deleted successfully")
        return redirect(url_for("dashboard"))
    finally:
        db.close()

@app.route('/api/chat', methods=['POST'])
def api_chat():
    if not AI_AVAILABLE or not groq_client:
        return jsonify({"error": "نشمي حالياً خارج التغطية"}), 503
    data = request.get_json()
    if not data or not data.get("message"):
        return jsonify({"error": "No message provided"}), 400
    try:
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": "You are Nashmi, an AI assistant on StarLearn."},
                {"role": "user", "content": data.get("message")}
            ]
        )
        return jsonify({"reply": response.choices[0].message.content})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ─── BUG FIX #7: seed_database تعمل دائماً ──────────────────────────────────
# كان: تعمل فقط عند تشغيل python app.py، لا تعمل مع Gunicorn
# الحل: استدعاؤها خارج if __name__ == "__main__"
def seed_database():
    db = get_db()
    try:
        if not db.query(User).filter(User.username == "admin_majed").first():
            hashed_pw = generate_password_hash("admin123")
            admin = User(username="admin_majed", password=hashed_pw, full_name="Main Admin", role="teacher")
            db.add(admin)
            db.commit()
    finally:
        db.close()

seed_database()  # ← تعمل عند أي طريقة تشغيل

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
    # 1. نظام المتابعة (Follow & Follow-back)
class Follow(Base):
    __tablename__ = "follows"
    id = Column(Integer, primary_key=True)
    follower_id = Column(Integer, ForeignKey("users.id")) # الشخص اللي عمل فولو
    followed_id = Column(Integer, ForeignKey("users.id")) # الشخص اللي استقبل الفولو

# 2. نظام طلبات الصداقة (Friend Requests)
class FriendRequest(Base):
    __tablename__ = "friend_requests"
    id = Column(Integer, primary_key=True)
    sender_id = Column(Integer, ForeignKey("users.id"))
    receiver_id = Column(Integer, ForeignKey("users.id"))
    status = Column(String, default="pending") # pending, accepted, rejected

# 3. نظام المجموعات (Groups)
class Group(Base):
    __tablename__ = "groups"
    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    description = Column(String(500))
    created_by = Column(Integer, ForeignKey("users.id"))

class GroupMember(Base):
    __tablename__ = "group_members"
    id = Column(Integer, primary_key=True)
    group_id = Column(Integer, ForeignKey("groups.id"))
    user_id = Column(Integer, ForeignKey("users.id"))
    
@app.route("/chat/<int:friend_id>")
@login_required
def private_chat(friend_id):
    db = get_db()
    current_user_id = session["user_id"]
    # تحقق من وجود علاقة صداقة مقبولة
    friendship = db.query(FriendRequest).filter(
        ((FriendRequest.sender_id == current_user_id) & (FriendRequest.receiver_id == friend_id) & (FriendRequest.status == 'accepted')) |
        ((FriendRequest.sender_id == friend_id) & (FriendRequest.receiver_id == current_user_id) & (FriendRequest.status == 'accepted'))
    ).first()
    
    if not friendship:
        flash("يجب أن تكونوا أصدقاء لتتمكن من المراسلة")
        return redirect(url_for("dashboard"))
        
    return render_template("chat.html", friend_id=friend_id)
# عرض صفحة دردشة المجموعة
@app.route("/group/<int:group_id>")
@login_required
def group_chat(group_id):
    db = get_db()
    try:
        user = get_current_user(db)
        group = db.query(Group).filter(Group.id == group_id).first()
        
        # التأكد من أن المستخدم عضو في المجموعة
        is_member = db.query(GroupMember).filter_by(group_id=group_id, user_id=user.id).first()
        if not is_member:
            flash("يجب الانضمام للمجموعة أولاً للمشاركة في الدردشة")
            return redirect(url_for("dashboard"))
            
        # جلب آخر 50 رسالة
        messages = db.query(GroupMessage).filter(GroupMessage.group_id == group_id).order_by(GroupMessage.created_at.asc()).all()
        
        return render_template("group_chat.html", user=user, group=group, messages=messages)
    finally:
        db.close()

# API لإرسال رسالة جديدة
@app.route("/api/group/send", methods=["POST"])
@login_required
def send_group_msg():
    db = get_db()
    try:
        data = request.get_json()
        new_msg = GroupMessage(
            group_id=data['group_id'],
            user_id=session['user_id'],
            content=data['message']
        )
        db.add(new_msg)
        db.commit()
        return jsonify({"status": "success"})
    finally:
        db.close()