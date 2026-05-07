from fastapi import FastAPI, Request, Form, HTTPException, Depends, BackgroundTasks, UploadFile, File
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean, Float, func, UniqueConstraint, text, event
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.exc import IntegrityError
from datetime import datetime, timedelta
import os
import pandas as pd
from io import BytesIO
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo
from contextlib import contextmanager
from uuid import uuid4
from itsdangerous import URLSafeTimedSerializer
import smtplib
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr
import random
import secrets
import string
import time
import json
import tempfile
from pathlib import Path
from openpyxl.styles import Alignment, Font
try:
    from tool_processors import (
        assign_invigilators,
        calculate_cet_pass_rates,
        extract_grade_from_filename,
        generate_seat_labels_pdf,
        merge_excel_sheets,
        PASS_SCORE,
        EXCLUDE_MAJORS,
    )
except ImportError:
    from app.tool_processors import (
        assign_invigilators,
        calculate_cet_pass_rates,
        extract_grade_from_filename,
        generate_seat_labels_pdf,
        merge_excel_sheets,
        PASS_SCORE,
        EXCLUDE_MAJORS,
    )


# ==================== 北京时间配置 ====================
BEIJING_TZ = ZoneInfo("Asia/Shanghai")

def now_beijing():
    return datetime.now(BEIJING_TZ).replace(tzinfo=None)

def load_env_file():
    """Load a local .env for development without overriding real environment variables."""
    candidates = [
        Path(__file__).resolve().parent / ".env",
        Path(__file__).resolve().parent.parent / ".env",
    ]
    for env_path in candidates:
        if not env_path.exists():
            continue
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value

load_env_file()

# ==================== 核心配置 ====================
ADMIN_USERNAME = os.getenv("KAOWU_ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("KAOWU_ADMIN_PASSWORD", "change_this_immediately")
SECRET_KEY = os.getenv("KAOWU_SECRET_KEY", "kaowu_2026_secret")
if SECRET_KEY == "kaowu_2026_secret":
    print("WARNING: Using default SECRET_KEY. Set KAOWU_SECRET_KEY env var for production.")
serializer = URLSafeTimedSerializer(SECRET_KEY)
TOOLS_PIN = os.getenv("KAOWU_TOOLS_PIN")
TOOLS_UNLOCK_MAX_AGE = int(os.getenv("KAOWU_TOOLS_UNLOCK_MAX_AGE", 3600))

# 新增邮箱配置
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", 465))

# ==================== FastAPI 初始化 ====================
app = FastAPI(title="考务报名系统")

# 新增：配置模板目录（关键！必须加）
templates = Jinja2Templates(directory="templates")

@app.middleware("http")
async def add_csrf_token(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/admin"):
        if "kaowu_csrf" not in request.cookies:
            token = str(uuid4())
            response.set_cookie(
                key="kaowu_csrf",
                value=token,
                httponly=False,
                max_age=3600 * 24,
                samesite="lax"
            )
    return response
app.mount("/static", StaticFiles(directory="static"), name="static")

# ==================== 数据库 ====================
DB_DIR = os.getenv("DB_DIR", os.path.join(os.path.dirname(__file__), "db"))
DB_PATH = os.path.join(DB_DIR, "kaowu.db")
os.makedirs(DB_DIR, exist_ok=True)
engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})

@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# ==================== 模型（新增QQ字段） ====================
class Recruitment(Base):
    __tablename__ = "recruitment"
    id = Column(Integer, primary_key=True)
    exam_name = Column(String(100), nullable=False)
    need_num = Column(Integer, nullable=False)
    create_time = Column(DateTime, default=now_beijing)
    is_active = Column(Boolean, default=True)
    qq_group = Column(String(300), nullable=True)  # QQ加群链接（qm.qq.com 或 tencent:// 协议）
    general_supervisor_id = Column(Integer, nullable=True)  # 总负责人，关联 registration.id
    has_floor_supervisors = Column(Boolean, default=False)  # 是否需要楼栋负责人（大考/小考自适应）
    end_time = Column(DateTime, nullable=True)   # 北京时间

class RecruitmentClassroom(Base):
    """招募-教室关联：记录本次考试使用了哪些教室以及单/双模式"""
    __tablename__ = "recruitment_classrooms"
    id = Column(Integer, primary_key=True)
    recruitment_id = Column(Integer, nullable=False)
    classroom_id = Column(Integer, nullable=False)
    exam_mode = Column(String(10), nullable=False, default="single")  # 'single' 或 'double'
    exam_number_start = Column(Integer, nullable=False)  # 该教室考场起始号
    __table_args__ = (UniqueConstraint('recruitment_id', 'classroom_id', name='uq_recruitment_classroom'),)


class RecruitmentGroup(Base):
    """分组：按栋/区划分的组"""
    __tablename__ = "recruitment_groups"
    id = Column(Integer, primary_key=True)
    recruitment_id = Column(Integer, nullable=False)
    zone_name = Column(String(20), nullable=True)
    is_supervisor = Column(Boolean, default=False)


class RecruitmentGroupMember(Base):
    """组成员"""
    __tablename__ = "recruitment_group_members"
    id = Column(Integer, primary_key=True)
    group_id = Column(Integer, nullable=False)
    registration_id = Column(Integer, nullable=False)
    __table_args__ = (UniqueConstraint('group_id', 'registration_id', name='uq_group_member'),)


class RecruitmentGroupClassroom(Base):
    """组-教室分配"""
    __tablename__ = "recruitment_group_classrooms"
    id = Column(Integer, primary_key=True)
    group_id = Column(Integer, nullable=False)
    recruitment_classroom_id = Column(Integer, nullable=False)
    __table_args__ = (UniqueConstraint('recruitment_classroom_id', name='uq_group_classroom_assignment'),)


class TaskProgress(Base):
    """布置/恢复任务进度记录"""
    __tablename__ = "task_progress"
    id = Column(Integer, primary_key=True)
    recruitment_classroom_id = Column(Integer, nullable=False)
    item_key = Column(String(50), nullable=False)
    item_name = Column(String(100), nullable=False)
    is_completed = Column(Boolean, default=False)
    is_auto_skip = Column(Boolean, default=False)
    completed_by = Column(Integer, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    task_type = Column(String(20), default="setup")  # "setup" or "recovery"


class Registration(Base):
    __tablename__ = "registration"
    id = Column(Integer, primary_key=True)
    recruitment_id = Column(Integer, nullable=False)
    student_id = Column(String(20), nullable=False)
    name = Column(String(50), nullable=False)
    phone = Column(String(20), nullable=False)
    qq = Column(String(20), nullable=False)  # 新增QQ字段
    ip_address = Column(String(50), nullable=False)
    create_time = Column(DateTime, default=now_beijing)
    # 新增字段：经验（建议用布尔，简单；或者用字符串 "有经验"/"无经验" 更直观）
    has_experience = Column(Boolean, nullable=False, default=False)  # True=有经验, False=无经验
    gender = Column(String(4), nullable=False, default="男")  # "男" 或 "女"
    __table_args__ = (UniqueConstraint('recruitment_id', 'student_id', name='uq_registration_recruitment_student'),)

# 新增验证码记录表（可选，替代内存存储）
class VerifyCode(Base):
    __tablename__ = "verify_code"
    id = Column(Integer, primary_key=True)
    reg_id = Column(Integer, nullable=False)
    code = Column(String(6), nullable=False)
    email = Column(String(100), nullable=False)
    create_time = Column(DateTime, default=now_beijing)
    is_used = Column(Boolean, default=False)

# ==================== 考场基础数据模型 ====================
class Building(Base):
    """教学楼"""
    __tablename__ = "buildings"
    id = Column(Integer, primary_key=True)
    name = Column(String(50), unique=True, nullable=False)  # 如"树人楼""综合楼"


class Classroom(Base):
    """教室"""
    __tablename__ = "classrooms"
    id = Column(Integer, primary_key=True)
    building_id = Column(Integer, nullable=False)
    name = Column(String(50), nullable=False)  # 如"B101""102"
    is_fixed_seats = Column(Boolean, default=False)   # True=固定桌椅
    can_double_exam = Column(Boolean, default=False)  # True=具备双考场条件
    is_enabled = Column(Boolean, default=True)         # True=可用作考场
    __table_args__ = (UniqueConstraint('building_id', 'name', name='uq_building_classroom'),)



class AcceptanceRecord(Base):
    """验收记录"""
    __tablename__ = "acceptance_records"
    id = Column(Integer, primary_key=True)
    recruitment_classroom_id = Column(Integer, nullable=False)
    status = Column(String(20), default="pending")    # pending/rejected/passed/sealed
    reviewer_type = Column(String(20), nullable=True)
    reviewer_id = Column(Integer, nullable=True)
    note = Column(String(500), nullable=True)
    created_at = Column(DateTime, default=now_beijing)
    updated_at = Column(DateTime, default=now_beijing, onupdate=now_beijing)
    __table_args__ = (UniqueConstraint('recruitment_classroom_id', name='uq_acceptance_record_classroom'),)


class CetExamBatch(Base):
    """四六级考试批次"""
    __tablename__ = "cet_exam_batches"
    id = Column(Integer, primary_key=True)
    exam_year = Column(Integer, nullable=False)
    exam_term = Column(String(20), nullable=False)       # spring/fall/other
    exam_level = Column(String(10), nullable=False)      # cet4/cet6
    batch_variant = Column(String(20), nullable=False, default="normal")
    batch_name = Column(String(100), nullable=False)
    recognition_status = Column(String(20), nullable=False, default="auto")
    source_filenames = Column(String(1000), nullable=True)
    record_count = Column(Integer, nullable=False, default=0)
    upload_time = Column(DateTime, default=now_beijing)
    __table_args__ = (UniqueConstraint('exam_year', 'exam_term', 'exam_level', 'batch_variant', name='uq_cet_exam_batch'),)


class CetScore(Base):
    """四六级成绩记录"""
    __tablename__ = "cet_scores"
    id = Column(Integer, primary_key=True)
    batch_id = Column(Integer, nullable=False)
    ticket_no = Column(String(30), nullable=False)
    id_card = Column(String(30), nullable=False)
    student_no = Column(String(40), nullable=True)
    name = Column(String(50), nullable=True)
    college = Column(String(100), nullable=True)
    school_name = Column(String(100), nullable=True)
    listening_score = Column(Float, nullable=True)
    reading_score = Column(Float, nullable=True)
    writing_score = Column(Float, nullable=True)
    total_score = Column(Float, nullable=True)
    raw_level = Column(String(10), nullable=True)
    upload_time = Column(DateTime, default=now_beijing)
    __table_args__ = (UniqueConstraint('batch_id', 'id_card', name='uq_cet_score_batch_id_card'),)


class GraduateBatch(Base):
    """毕业生届别批次"""
    __tablename__ = "graduate_batches"
    id = Column(Integer, primary_key=True)
    grade_name = Column(String(50), unique=True, nullable=False)
    source_filename = Column(String(300), nullable=True)
    record_count = Column(Integer, nullable=False, default=0)
    upload_time = Column(DateTime, default=now_beijing)


class GraduateRecord(Base):
    """毕业生基础数据"""
    __tablename__ = "graduate_records"
    id = Column(Integer, primary_key=True)
    batch_id = Column(Integer, nullable=False)
    id_card = Column(String(30), nullable=False)
    student_no = Column(String(40), nullable=True)
    name = Column(String(50), nullable=True)
    major = Column(String(100), nullable=True)
    education_level = Column(String(50), nullable=True)
    college = Column(String(100), nullable=True)
    __table_args__ = (UniqueConstraint('batch_id', 'id_card', name='uq_graduate_record_batch_id_card'),)


class BuildingSupervisor(Base):
    """楼栋负责人"""
    __tablename__ = "building_supervisors"
    id = Column(Integer, primary_key=True)
    recruitment_id = Column(Integer, nullable=False)
    zone_name = Column(String(20), nullable=False)    # 如 "B栋"
    registration_id = Column(Integer, nullable=False)  # 关联 registration.id
    __table_args__ = (UniqueConstraint('recruitment_id', 'zone_name', name='uq_building_supervisor_zone'),)


Base.metadata.create_all(bind=engine)

# 数据库迁移：手动分组相关字段
try:
    with engine.connect() as conn:
        conn.execute(text("ALTER TABLE recruitment ADD COLUMN general_supervisor_id INTEGER DEFAULT NULL"))
        conn.commit()
except Exception:
    pass  # 字段已存在
try:
    with engine.connect() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS building_supervisors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recruitment_id INTEGER NOT NULL,
                zone_name VARCHAR(20) NOT NULL,
                registration_id INTEGER NOT NULL
            )
        """))
        conn.commit()
except Exception:
    pass  # 表已存在

# 数据库迁移：新增字段
try:
    with engine.connect() as conn:
        conn.execute(text("ALTER TABLE classrooms ADD COLUMN is_enabled BOOLEAN DEFAULT 1"))
        conn.commit()
except Exception:
    pass  # 字段已存在

# 数据库迁移：组成员唯一约束
try:
    with engine.connect() as conn:
        conn.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_group_member
            ON recruitment_group_members (group_id, registration_id)
        """))
        conn.commit()
except Exception:
    pass  # 索引已存在

# 数据库迁移：has_floor_supervisors 字段
try:
    with engine.connect() as conn:
        conn.execute(text("ALTER TABLE recruitment ADD COLUMN has_floor_supervisors BOOLEAN DEFAULT 0"))
        conn.commit()
except Exception:
    pass  # 字段已存在

# 数据库约束/索引：为 SQLite 既有库补上关键唯一约束和查询索引
try:
    with engine.connect() as conn:
        conn.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_registration_recruitment_student
            ON registration (recruitment_id, student_id)
        """))
        conn.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_recruitment_classroom
            ON recruitment_classrooms (recruitment_id, classroom_id)
        """))
        conn.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_group_classroom_assignment
            ON recruitment_group_classrooms (recruitment_classroom_id)
        """))
        conn.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_acceptance_record_classroom
            ON acceptance_records (recruitment_classroom_id)
        """))
        conn.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_building_supervisor_zone
            ON building_supervisors (recruitment_id, zone_name)
        """))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_registration_student_phone ON registration (student_id, phone)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_recruitment_classrooms_classroom ON recruitment_classrooms (classroom_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_task_progress_rc_type ON task_progress (recruitment_classroom_id, task_type)"))
        conn.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_cet_exam_batch
            ON cet_exam_batches (exam_year, exam_term, exam_level, batch_variant)
        """))
        conn.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_cet_score_batch_id_card
            ON cet_scores (batch_id, id_card)
        """))
        conn.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_graduate_record_batch_id_card
            ON graduate_records (batch_id, id_card)
        """))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_cet_scores_id_card ON cet_scores (id_card)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_graduate_records_id_card ON graduate_records (id_card)"))
        conn.commit()
except Exception as e:
    print(f"WARNING: failed to create database indexes/constraints: {e}")

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@contextmanager
def db_lock(db: Session):
    try:
        yield db
        db.commit()
    except:
        db.rollback()
        raise

# ==================== 工具函数 ====================

# 获取真实客户端 IP（支持反代和 Docker 环境）
def get_client_ip(request: Request) -> str:
    # 优先取 X-Forwarded-For 中的第一个 IP（原始客户端 IP）
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    # 其次取 X-Real-IP（Nginx 常用）
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip
    # 兜底取直连 IP
    return request.client.host or "unknown"

# 简易内存级限流器
_RATE_LIMITS: dict[str, list[float]] = {}
_RATE_LIMIT_CLEANUP_AT = 0.0

def rate_limit(key: str, max_requests: int = 10, window: int = 60):
    global _RATE_LIMIT_CLEANUP_AT
    now = time.time()
    if now - _RATE_LIMIT_CLEANUP_AT > 60:
        _cleanup_rate_limits(now)
        _RATE_LIMIT_CLEANUP_AT = now
    if key not in _RATE_LIMITS:
        _RATE_LIMITS[key] = []
    _RATE_LIMITS[key] = [t for t in _RATE_LIMITS[key] if now - t < window]
    if len(_RATE_LIMITS[key]) >= max_requests:
        raise HTTPException(429, f"请求过于频繁，请{window}秒后再试")
    _RATE_LIMITS[key].append(now)

# 定期清理过期限流记录
def _cleanup_rate_limits(now: float | None = None):
    now = now or time.time()
    for key in list(_RATE_LIMITS.keys()):
        _RATE_LIMITS[key] = [t for t in _RATE_LIMITS[key] if now - t < 120]
        if not _RATE_LIMITS[key]:
            del _RATE_LIMITS[key]

# ==================== 考场工具函数 ====================

def detect_zone(classroom_name: str) -> str | None:
    """从教室名称推导"栋"信息。
    规则：
    - 首字母为英文字母 → 返回该字母 + "栋"（B101 → B栋）
    - 首字母为数字 → 返回 None（综合楼 101 → 无分区）
    """
    if not classroom_name or not classroom_name.strip():
        return None
    first_char = classroom_name.strip()[0]
    if 'A' <= first_char.upper() <= 'Z':
        return f"{first_char.upper()}栋"
    return None


def normalize_qq_group_url(value: str | None) -> str | None:
    if not value or not value.strip():
        return None
    url = value.strip()
    parsed = urlparse(url)
    if parsed.scheme in ("http", "https") and parsed.netloc.lower().endswith("qm.qq.com"):
        return url
    if parsed.scheme == "tencent":
        return url
    raise HTTPException(400, "QQ加群链接仅支持 qm.qq.com 或 tencent:// 链接")


def serialize_recruit_classrooms(recruit_id: int, db: Session) -> list[dict]:
    """查询某个招募的所有考场配置"""
    rcs = db.query(RecruitmentClassroom).filter(
        RecruitmentClassroom.recruitment_id == recruit_id
    ).order_by(RecruitmentClassroom.exam_number_start).all()

    if not rcs:
        return []

    classroom_ids = [rc.classroom_id for rc in rcs]
    classrooms = {c.id: c for c in db.query(Classroom).filter(Classroom.id.in_(classroom_ids)).all()}
    building_ids = {c.building_id for c in classrooms.values()}
    buildings = {b.id: b.name for b in db.query(Building).filter(Building.id.in_(building_ids)).all()}

    result = []
    for rc in rcs:
        cr = classrooms.get(rc.classroom_id)
        if not cr:
            continue
        exam_numbers = []
        if rc.exam_mode == "double":
            exam_numbers = [rc.exam_number_start, rc.exam_number_start + 1]
        else:
            exam_numbers = [rc.exam_number_start]

        result.append({
            "id": rc.id,
            "recruitment_id": rc.recruitment_id,
            "classroom_id": rc.classroom_id,
            "classroom_name": cr.name,
            "building_name": buildings.get(cr.building_id, ""),
            "exam_mode": rc.exam_mode,
            "exam_number_start": rc.exam_number_start,
            "exam_numbers": exam_numbers,
        })
    return result


# 标准布置清单（8项）
STANDARD_SETUP_ITEMS = [
    ("door_post", "张贴门帖（核对门牌号）", False),
    ("forbidden_items", "设置禁带物品放置处", False),
    ("count_desks", "清点桌椅数量/补齐缺额", True),
    ("clean_room", "教室环境清理（黑板/窗帘/课桌）", False),
    ("check_clock", "核对时钟", False),
    ("check_broadcast", "检查广播声音（听够3分钟）", False),
    ("seat_labels", "张贴座位号", False),
    ("self_check", "自查（对照标准逐项确认）", False),
]

# 考后恢复清单（5项）
RECOVERY_ITEMS = [
    ("return_chairs", "将门外椅子搬回室内", False),
    ("remove_door_post", "撕除门帖（不留痕迹）", False),
    ("remove_seat_labels", "撕除座位贴", False),
    ("remove_forbidden_sign", "撕除禁带物品标识", False),
    ("clean_tape", "清理胶带残留", False),
]


def init_task_progress(recruit_id: int, db: Session):
    """为已分组的教室创建布置清单进度记录"""
    rcs = db.query(RecruitmentClassroom).filter(
        RecruitmentClassroom.recruitment_id == recruit_id
    ).all()
    if not rcs:
        return

    rc_ids = [rc.id for rc in rcs]
    db.query(TaskProgress).filter(
        TaskProgress.recruitment_classroom_id.in_(rc_ids),
        TaskProgress.task_type == "setup"
    ).delete(synchronize_session=False)

    cr_map = {}
    classroom_ids = [rc.classroom_id for rc in rcs]
    for cr in db.query(Classroom).filter(Classroom.id.in_(classroom_ids)).all():
        cr_map[cr.id] = cr.is_fixed_seats

    for rc in rcs:
        is_fixed = cr_map.get(rc.classroom_id, False)
        for item_key, item_name, skip_for_fixed in STANDARD_SETUP_ITEMS:
            tp = TaskProgress(
                recruitment_classroom_id=rc.id,
                item_key=item_key,
                item_name=item_name,
                is_auto_skip=(skip_for_fixed and is_fixed),
                is_completed=(skip_for_fixed and is_fixed),
                task_type="setup",
            )
            db.add(tp)

    db.commit()


def init_acceptance_records(recruit_id: int, db: Session):
    """初始化验收记录（为所有已分组的考场创建）"""
    rcs = db.query(RecruitmentClassroom).filter(
        RecruitmentClassroom.recruitment_id == recruit_id
    ).all()
    for rc in rcs:
        existing = db.query(AcceptanceRecord).filter(
            AcceptanceRecord.recruitment_classroom_id == rc.id
        ).first()
        if not existing:
            db.add(AcceptanceRecord(
                recruitment_classroom_id=rc.id,
                status="pending",
            ))
    db.commit()


def init_recovery_tasks(recruit_id: int, db: Session):
    """为已封门的考场创建恢复任务"""
    rcs = db.query(RecruitmentClassroom).filter(
        RecruitmentClassroom.recruitment_id == recruit_id
    ).all()
    if not rcs:
        return

    rc_ids = [rc.id for rc in rcs]
    db.query(TaskProgress).filter(
        TaskProgress.recruitment_classroom_id.in_(rc_ids),
        TaskProgress.task_type == "recovery"
    ).delete(synchronize_session=False)

    for rc in rcs:
        for item_key, item_name, _ in RECOVERY_ITEMS:
            tp = TaskProgress(
                recruitment_classroom_id=rc.id,
                item_key=item_key,
                item_name=item_name,
                task_type="recovery",
            )
            db.add(tp)

    db.commit()




# 生成6位数字验证码
def generate_verify_code():
    return ''.join(random.choices(string.digits, k=6))

# 发送邮箱验证码
def send_verify_email(to_email: str, code: str):
    if not SMTP_USER or not SMTP_PASS:
        raise HTTPException(400, "邮箱配置未完成，无法发送验证码")
    
    subject = "考务报名取消验证"
    content = f"""
    <p>你正在取消考务报名，验证码为：<strong>{code}</strong></p>
    <p>验证码有效期5分钟，请及时使用</p>
    <p>如非本人操作，请忽略此邮件</p>
    """
    msg = MIMEText(content, 'html', 'utf-8')
    msg['Subject'] = Header(subject, 'utf-8')  # 主题也建议用Header处理
    msg['From'] = formataddr(("教务处考务组", SMTP_USER))
    msg['To'] = to_email

    try:
        # 改成 SMTP_SSL + 去掉 starttls
        server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT)
        server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg)
        server.quit()
        return True
    except Exception as e:
        import traceback
        print(f"发送邮件失败: {e}")
        print(traceback.format_exc())
        raise HTTPException(500, f"发送邮件失败：{str(e)}")

# ==================== 登录验证 ====================
def check_admin_login(request: Request):
    session = request.cookies.get("kaowu_admin")
    if not session:
        raise HTTPException(status_code=307, detail="请先登录", headers={"Location": "/admin/login"})
    try:
        data = serializer.loads(session, max_age=3600)
        if data != ADMIN_USERNAME:
            raise HTTPException(status_code=307, detail="登录失效", headers={"Location": "/admin/login"})
    except:
        raise HTTPException(status_code=307, detail="登录失效", headers={"Location": "/admin/login"})

def check_csrf(request: Request):
    csrf_token = request.cookies.get("kaowu_csrf")
    client_token = request.headers.get("X-CSRF-Token")
    if not csrf_token or csrf_token != client_token:
        raise HTTPException(status_code=403, detail="CSRF token 校验失败")

def check_tools_unlocked(request: Request):
    if not TOOLS_PIN:
        raise HTTPException(status_code=403, detail="工具页 PIN 未配置，请先设置 KAOWU_TOOLS_PIN")
    token = request.cookies.get("kaowu_tools")
    if not token:
        raise HTTPException(status_code=403, detail="请先输入工具页 PIN")
    try:
        data = serializer.loads(token, max_age=TOOLS_UNLOCK_MAX_AGE)
        if data != f"tools:{ADMIN_USERNAME}":
            raise HTTPException(status_code=403, detail="工具页解锁已失效，请重新输入 PIN")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=403, detail="工具页解锁已失效，请重新输入 PIN")

# ==================== 页面路由 ====================
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):  # 新增 Request 参数，供模板使用
    # 渲染 templates 目录下的 index.html 模板
    return templates.TemplateResponse(
        request=request,
        name="index.html"  # 对应你的 templates/index.html 文件
    )

@app.get("/student", response_class=HTMLResponse)
async def student_page():
    return FileResponse("static/student.html")

@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page():
    return FileResponse("static/admin_login.html")

@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    check_admin_login(request)
    return FileResponse("static/admin.html")

# ==================== 接口路由 ====================
# 管理员登录/登出（不变）
@app.post("/api/admin/login")
async def admin_login(request: Request, form: OAuth2PasswordRequestForm = Depends()):
    rate_limit(f"login_{get_client_ip(request)}", max_requests=5, window=60)
    if form.username == ADMIN_USERNAME and form.password == ADMIN_PASSWORD:
        session = serializer.dumps(form.username)
        response = RedirectResponse(url="/admin", status_code=302)
        response.set_cookie(key="kaowu_admin", value=session, httponly=True, max_age=3600)
        return response
    raise HTTPException(status_code=401, detail="账号/密码错误")

@app.get("/api/admin/logout")
async def admin_logout():
    response = RedirectResponse(url="/admin/login")
    response.delete_cookie("kaowu_admin")
    return response

# 发布招募（不变）
@app.post("/api/recruit/add")
async def add_recruit(
    request: Request,
    exam_name: str = Form(...),
    need_num: int = Form(...),
    end_time_str: str = Form(None),
    qq_group: str = Form(None),
    classroom_ids: str = Form(""),      # 逗号分隔的教室ID
    exam_modes: str = Form(""),         # 逗号分隔的模式
    has_floor_supervisors: bool = Form(False),
    db: Session = Depends(get_db)
):
    check_admin_login(request)
    check_csrf(request)
    rate_limit(f"recruit_add_{get_client_ip(request)}", max_requests=10, window=60)
    if need_num < 1:
        raise HTTPException(400, "人数必须≥1")

    end_time = None
    if end_time_str:
        cleaned = end_time_str.replace("T", " ").strip()
        try:
            end_time = datetime.strptime(cleaned, "%Y-%m-%d %H:%M")
        except ValueError:
            try:
                end_time = datetime.strptime(cleaned, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                raise HTTPException(
                    400,
                    detail=f"结束时间格式错误（收到: {end_time_str}），应为 YYYY-MM-DD HH:MM 或 YYYY-MM-DDTHH:MM"
                )

    recruit = Recruitment(exam_name=exam_name.strip(), need_num=need_num, end_time=end_time, qq_group=normalize_qq_group_url(qq_group), has_floor_supervisors=has_floor_supervisors)
    db.add(recruit)
    db.commit()
    db.refresh(recruit)

    # 处理考场配置
    if classroom_ids and exam_modes:
        ids_list = [x.strip() for x in classroom_ids.split(",") if x.strip()]
        modes_list = [x.strip() for x in exam_modes.split(",") if x.strip()]
        if len(ids_list) != len(modes_list):
            raise HTTPException(400, "教室ID与模式数量不匹配")

        exam_no = 1
        for cid_str, mode in zip(ids_list, modes_list):
            try:
                cid = int(cid_str)
            except ValueError:
                continue
            if mode not in ("single", "double"):
                raise HTTPException(400, f"考场模式无效：{mode}")
            classroom = db.query(Classroom).filter(Classroom.id == cid).first()
            if not classroom:
                raise HTTPException(400, f"教室ID {cid} 不存在")
            if mode == "double" and not classroom.can_double_exam:
                raise HTTPException(400, f"教室 {classroom.name} 不具备双考场条件")

            rc = RecruitmentClassroom(
                recruitment_id=recruit.id,
                classroom_id=cid,
                exam_mode=mode,
                exam_number_start=exam_no
            )
            db.add(rc)
            exam_no += 2 if mode == "double" else 1

        db.commit()

    return {"code": 0, "msg": "发布成功"}

# 编辑招募
@app.put("/api/recruit/{recruit_id}")
async def edit_recruit(
    request: Request,
    recruit_id: int,
    exam_name: str = Form(...),
    need_num: int = Form(...),
    end_time_str: str = Form(None),
    qq_group: str = Form(None),
    classroom_ids: str = Form(""),      # 逗号分隔的教室ID
    exam_modes: str = Form(""),         # 逗号分隔的模式
    has_floor_supervisors: bool = Form(False),
    db: Session = Depends(get_db)
):
    check_admin_login(request)
    check_csrf(request)
    recruit = db.query(Recruitment).filter(Recruitment.id == recruit_id).first()
    if not recruit:
        raise HTTPException(404, "招募不存在")

    if need_num < 1:
        raise HTTPException(400, "人数必须≥1")

    current_count = db.query(func.count(Registration.id)).filter(Registration.recruitment_id == recruit_id).scalar()
    if need_num < current_count:
        raise HTTPException(400, f"已有 {current_count} 人报名，不能调低")

    recruit.exam_name = exam_name.strip()
    recruit.need_num = need_num
    if end_time_str:
        try:
            recruit.end_time = datetime.strptime(end_time_str, "%Y-%m-%d %H:%M")
        except:
            raise HTTPException(400, "结束时间格式错误")
    recruit.qq_group = normalize_qq_group_url(qq_group)
    recruit.has_floor_supervisors = has_floor_supervisors

    # 更新考场配置（如果有提供）
    if classroom_ids and exam_modes:
        ids_list = [x.strip() for x in classroom_ids.split(",") if x.strip()]
        modes_list = [x.strip() for x in exam_modes.split(",") if x.strip()]
        if len(ids_list) != len(modes_list):
            raise HTTPException(400, "教室ID与模式数量不匹配")

        # 先删除旧配置
        db.query(RecruitmentClassroom).filter(
            RecruitmentClassroom.recruitment_id == recruit_id
        ).delete()

        exam_no = 1
        for cid_str, mode in zip(ids_list, modes_list):
            try:
                cid = int(cid_str)
            except ValueError:
                continue
            if mode not in ("single", "double"):
                raise HTTPException(400, f"考场模式无效：{mode}")
            classroom = db.query(Classroom).filter(Classroom.id == cid).first()
            if not classroom:
                raise HTTPException(400, f"教室ID {cid} 不存在")
            if mode == "double" and not classroom.can_double_exam:
                raise HTTPException(400, f"教室 {classroom.name} 不具备双考场条件")

            rc = RecruitmentClassroom(
                recruitment_id=recruit_id,
                classroom_id=cid,
                exam_mode=mode,
                exam_number_start=exam_no
            )
            db.add(rc)
            exam_no += 2 if mode == "double" else 1

    db.commit()
    return {"code": 0, "msg": "修改成功"}

# 手动开启/关闭招募（不变）
@app.post("/api/recruit/{recruit_id}/toggle")
async def toggle_recruit(request: Request, recruit_id: int, db: Session = Depends(get_db)):
    check_admin_login(request)
    check_csrf(request)
    recruit = db.query(Recruitment).filter(Recruitment.id == recruit_id).first()
    if not recruit:
        raise HTTPException(404, "招募不存在")
    recruit.is_active = not recruit.is_active
    db.commit()
    status = "开启" if recruit.is_active else "关闭"
    return {"code": 0, "msg": f"招募已{status}"}

# 删除招募（同时删除关联报名记录）
@app.delete("/api/recruit/{recruit_id}")
async def delete_recruit(request: Request, recruit_id: int, db: Session = Depends(get_db)):
    check_admin_login(request)
    check_csrf(request)
    recruit = db.query(Recruitment).filter(Recruitment.id == recruit_id).first()
    if not recruit:
        raise HTTPException(404, "招募不存在")
    # 删除关联：验收记录、任务进度
    rc_ids = [rc.id for rc in db.query(RecruitmentClassroom).filter(RecruitmentClassroom.recruitment_id == recruit_id).all()]
    if rc_ids:
        db.query(AcceptanceRecord).filter(AcceptanceRecord.recruitment_classroom_id.in_(rc_ids)).delete(synchronize_session=False)
        db.query(TaskProgress).filter(TaskProgress.recruitment_classroom_id.in_(rc_ids)).delete(synchronize_session=False)

    # 删除关联：分组（含成员和教室分配）
    group_ids = [g.id for g in db.query(RecruitmentGroup).filter(RecruitmentGroup.recruitment_id == recruit_id).all()]
    if group_ids:
        db.query(RecruitmentGroupMember).filter(RecruitmentGroupMember.group_id.in_(group_ids)).delete(synchronize_session=False)
        db.query(RecruitmentGroupClassroom).filter(RecruitmentGroupClassroom.group_id.in_(group_ids)).delete(synchronize_session=False)
        db.query(RecruitmentGroup).filter(RecruitmentGroup.id.in_(group_ids)).delete(synchronize_session=False)

    # 删除关联：教室配置、楼栋负责人
    db.query(RecruitmentClassroom).filter(RecruitmentClassroom.recruitment_id == recruit_id).delete(synchronize_session=False)
    db.query(BuildingSupervisor).filter(BuildingSupervisor.recruitment_id == recruit_id).delete(synchronize_session=False)

    # 先删关联的报名记录
    db.query(Registration).filter(Registration.recruitment_id == recruit_id).delete(synchronize_session=False)
    # 再删招募
    db.query(Recruitment).filter(Recruitment.id == recruit_id).delete(synchronize_session=False)
    db.commit()
    return {"code": 0, "msg": "删除成功"}

# 学生端招募列表
@app.get("/api/recruit/list")
async def get_recruit_list(db: Session = Depends(get_db)):
    recruits = db.query(Recruitment).filter(Recruitment.is_active == True).all()
    if not recruits:
        return []
    recruit_ids = [r.id for r in recruits]
    counts = dict(
        db.query(Registration.recruitment_id, func.count(Registration.id))
        .filter(Registration.recruitment_id.in_(recruit_ids))
        .group_by(Registration.recruitment_id)
        .all()
    )
    result = []
    for r in recruits:
        registered = counts.get(r.id, 0)
        remaining = max(r.need_num - registered, 0)
        result.append({
            "id": r.id,
            "exam_name": r.exam_name,
            "need_num": r.need_num,
            "remaining": remaining,
            "end_time": r.end_time.strftime("%Y-%m-%d %H:%M") if r.end_time else "不限时"
        })
    return result

# 管理员招募列表
@app.get("/api/recruit/admin-list")
async def get_admin_recruit_list(request: Request, db: Session = Depends(get_db)):
    check_admin_login(request)
    recruits = db.query(Recruitment).all()
    if not recruits:
        return []
    recruit_ids = [r.id for r in recruits]
    counts = dict(
        db.query(Registration.recruitment_id, func.count(Registration.id))
        .filter(Registration.recruitment_id.in_(recruit_ids))
        .group_by(Registration.recruitment_id)
        .all()
    )
    now = now_beijing()
    result = []
    for r in recruits:
        count_val = counts.get(r.id, 0)
        status = "已关闭" if not r.is_active else ("已截止" if r.end_time and now > r.end_time else "进行中")
        classrooms_info = serialize_recruit_classrooms(r.id, db)
        total_exam_rooms = sum(len(c["exam_numbers"]) for c in classrooms_info)
        result.append({
            "id": r.id,
            "exam_name": r.exam_name,
            "need_num": r.need_num,
            "registered": count_val,
            "end_time": r.end_time.strftime("%Y-%m-%d %H:%M") if r.end_time else None,
            "status": status,
            "qq_group": r.qq_group,
            "has_floor_supervisors": r.has_floor_supervisors,
            "classrooms": classrooms_info,
            "total_exam_rooms": total_exam_rooms,
        })
    return result

# 查看报名名单（管理端页面内查看）
@app.get("/api/recruit/{recruit_id}/registrations")
async def view_registrations(request: Request, recruit_id: int, db: Session = Depends(get_db)):
    check_admin_login(request)
    recruit = db.query(Recruitment).filter(Recruitment.id == recruit_id).first()
    if not recruit:
        raise HTTPException(404, "招募不存在")
    regs = db.query(Registration).filter(
        Registration.recruitment_id == recruit_id
    ).order_by(Registration.create_time.desc()).all()
    return [{
        "id": r.id,
        "student_id": r.student_id,
        "name": r.name,
        "gender": r.gender,
        "phone": r.phone,
        "qq": r.qq,
        "has_experience": r.has_experience,
        "ip_address": r.ip_address,
        "create_time": r.create_time.strftime("%Y-%m-%d %H:%M") if r.create_time else None
    } for r in regs]

# 学生报名（新增QQ字段校验）
@app.post("/api/reg")
async def student_register(
    request: Request,
    recruitment_id: int = Form(...),
    student_id: str = Form(...),
    name: str = Form(...),
    phone: str = Form(...),
    qq: str = Form(...),  # 新增QQ字段
    has_experience: bool = Form(...),   # 新增：前端会传 "true"/"false" 或 "1"/"0"，FastAPI 会转 bool
    gender: str = Form(...),
    db: Session = Depends(get_db)
):
    rate_limit(f"reg_{get_client_ip(request)}", max_requests=10, window=60)
    # 校验学号
    if not (student_id.isdigit() and len(student_id) == 8):
        raise HTTPException(400, "学号必须是8位纯数字")
    # 校验手机号
    if not (phone.isdigit() and len(phone) == 11):
        raise HTTPException(400, "手机号必须是11位纯数字")
    # 校验QQ号
    if not qq.isdigit():
        raise HTTPException(400, "QQ号必须是纯数字")
    # 校验性别
    if gender not in ("男", "女"):
        raise HTTPException(400, "性别必须是男或女")

    ip = get_client_ip(request)
    
    # 加行锁查询招募记录
    recruit = db.query(Recruitment).filter(Recruitment.id == recruitment_id).with_for_update().first()
    if not recruit:
        raise HTTPException(400, "该招募不存在")

    now = now_beijing()

    # 三种关闭判断
    if not recruit.is_active:
        raise HTTPException(400, "该招募已手动关闭")
    if recruit.end_time and now > recruit.end_time:
        recruit.is_active = False
        db.commit()
        raise HTTPException(400, "报名已截止（时间到期，北京时间）")

    is_full = False
    try:
        with db_lock(db):
            # SQLite 不支持行级锁，依赖事务内重查 + 唯一索引兜底重复报名
            current_count = db.query(func.count(Registration.id)).filter(Registration.recruitment_id == recruitment_id).scalar()
            if current_count >= recruit.need_num:
                recruit.is_active = False
                is_full = True
            else:
                reg = Registration(
                    recruitment_id=recruitment_id,
                    student_id=student_id,
                    name=name,
                    phone=phone,
                    qq=qq,
                    has_experience=has_experience,
                    gender=gender,
                    ip_address=ip
                )
                db.add(reg)
                if current_count + 1 >= recruit.need_num:
                    recruit.is_active = False
    except IntegrityError:
        raise HTTPException(400, "此学号已报名过该考试")

    if is_full:
        raise HTTPException(400, "报名人数已满")

    return {"code": 0, "msg": "报名成功", "qq_group": recruit.qq_group}

# 查询我的报名记录（新增可取消标识+报名ID+QQ号）
@app.post("/api/my-registrations")
async def my_registrations(
    student_id: str = Form(...),
    phone: str = Form(...),
    db: Session = Depends(get_db)
):
    if not (student_id.isdigit() and len(student_id) == 8):
        raise HTTPException(400, "学号格式错误")
    if not (phone.isdigit() and len(phone) == 11):
        raise HTTPException(400, "手机号格式错误")

    regs = db.query(Registration).filter(
        Registration.student_id == student_id,
        Registration.phone == phone
    ).all()

    recruit_ids = list({reg.recruitment_id for reg in regs})
    recruit_map = {}
    if recruit_ids:
        recruit_map = {
            r.id: r
            for r in db.query(Recruitment).filter(Recruitment.id.in_(recruit_ids)).all()
        }

    result = []
    now = now_beijing()
    for reg in regs:
        recruit = recruit_map.get(reg.recruitment_id)
        if recruit:
            # 判断是否可取消：招募未截止 + 未手动关闭
            can_cancel = False
            if recruit.is_active:
                if not recruit.end_time or now < recruit.end_time:
                    can_cancel = True
            
            result.append({
                "reg_id": reg.id,  # 新增报名ID
                "recruit_id": recruit.id,  # 新增招募ID
                "exam_name": recruit.exam_name,
                "create_time": reg.create_time.strftime("%Y-%m-%d %H:%M"),
                "status": "已报名",
                "can_cancel": can_cancel,  # 新增可取消标识
                "qq": reg.qq,  # 新增QQ号
                "gender": reg.gender,
                "qq_group": recruit.qq_group  # 考务QQ群号
            })
    return {"code": 0, "data": result}

# 发送验证码接口
@app.post("/api/send-verify-code")
async def send_verify_code(
    background_tasks: BackgroundTasks,
    reg_id: int = Form(...),
    recruit_id: int = Form(...),
    email: str = Form(...),
    db: Session = Depends(get_db)
):
    rate_limit(f"send_code_{reg_id}", max_requests=3, window=300)
    reg = db.query(Registration).filter(Registration.id == reg_id).first()
    if not reg:
        raise HTTPException(404, "报名记录不存在")

    recruit = db.query(Recruitment).filter(Recruitment.id == recruit_id).first()
    if not recruit:
        raise HTTPException(404, "招募记录不存在")
    if not recruit.is_active or (recruit.end_time and now_beijing() > recruit.end_time):
        raise HTTPException(400, "该招募已截止，无法取消报名")

    if not email.endswith("@qq.com") or not email.split("@")[0].isdigit():
        raise HTTPException(400, "请输入正确的QQ邮箱")

    code = generate_verify_code()
    db_code = VerifyCode(
        reg_id=reg_id,
        code=code,
        email=email
    )
    db.add(db_code)
    db.commit()

    background_tasks.add_task(send_verify_email, email, code)
    return {"code": 0, "msg": "验证码发送成功"}

# 取消报名接口
@app.post("/api/cancel-reg")
async def cancel_reg(
    request: Request,
    reg_id: int = Form(...),
    verify_code: str = Form(...),
    db: Session = Depends(get_db)
):
    rate_limit(f"cancel_reg_{get_client_ip(request)}", max_requests=5, window=300)
    db_code = db.query(VerifyCode).filter(
        VerifyCode.reg_id == reg_id,
        VerifyCode.is_used == False,
        VerifyCode.create_time >= now_beijing() - timedelta(minutes=5)
    ).order_by(VerifyCode.create_time.desc()).first()
    if not db_code or db_code.code != verify_code:
        raise HTTPException(400, "验证码错误或已过期")
    db_code.is_used = True

    reg = db.query(Registration).filter(Registration.id == reg_id).with_for_update().first()
    if not reg:
        raise HTTPException(404, "报名记录不存在")

    recruit = db.query(Recruitment).filter(Recruitment.id == reg.recruitment_id).with_for_update().first()
    if not recruit:
        raise HTTPException(404, "招募记录不存在")
    if not recruit.is_active or (recruit.end_time and now_beijing() > recruit.end_time):
        raise HTTPException(400, "该招募已截止，无法取消报名")

    with db_lock(db):
        db.delete(reg)
        if not recruit.is_active:
            current_count = db.query(func.count(Registration.id)).filter(Registration.recruitment_id == recruit.id).scalar()
            if current_count < recruit.need_num:
                recruit.is_active = True

    return {"code": 0, "msg": "取消报名成功"}

# 导出 Excel（不变，新增QQ字段导出）
@app.get("/api/export/{recruit_id}")
async def export_excel(request: Request, recruit_id: int, db: Session = Depends(get_db)):
    check_admin_login(request)
    recruit = db.query(Recruitment).filter(Recruitment.id == recruit_id).first()
    if not recruit:
        raise HTTPException(404, "招募不存在")

    regs = db.query(Registration).filter(Registration.recruitment_id == recruit_id).all()
    if not regs:
        raise HTTPException(400, "暂无报名数据")

    data = []
    for reg in regs:
        data.append({
            "学号": reg.student_id,
            "姓名": reg.name,
            "性别": "男" if reg.gender == "男" else "女",
            "手机号": reg.phone,
            "QQ号": reg.qq,  # 新增QQ号导出
            "是否有经验": "有" if reg.has_experience else "无",   # ← 新增，友好显示
            "IP地址": reg.ip_address,
            "报名时间": reg.create_time.strftime("%Y-%m-%d %H:%M")
        })

    df = pd.DataFrame(data)
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name="报名列表")
    output.seek(0)

    safe_name = "".join(c for c in recruit.exam_name if c.isalnum() or c in " _-")[:50]
    filename_utf8 = f"{safe_name}_{recruit_id}.xlsx"
    filename_ascii = f"kaowu_{recruit_id}.xlsx"

    headers = {
        "Content-Disposition": f'attachment; filename="{filename_ascii}"; filename*=UTF-8\'\'{quote(filename_utf8)}',
        "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    }

    return StreamingResponse(output, headers=headers, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ==================== 管理端工具 API ====================

@app.get("/api/tools/status")
async def tools_status(request: Request):
    check_admin_login(request)
    if not TOOLS_PIN:
        return {"configured": False, "unlocked": False}
    try:
        check_tools_unlocked(request)
        return {"configured": True, "unlocked": True}
    except HTTPException:
        return {"configured": True, "unlocked": False}


@app.post("/api/tools/unlock")
async def tools_unlock(request: Request, pin: str = Form(...)):
    check_admin_login(request)
    check_csrf(request)
    rate_limit(f"tools_unlock_{get_client_ip(request)}", max_requests=5, window=300)
    if not TOOLS_PIN:
        raise HTTPException(403, "工具页 PIN 未配置，请先设置 KAOWU_TOOLS_PIN")
    if not secrets.compare_digest(pin.strip(), TOOLS_PIN):
        raise HTTPException(401, "PIN 码错误")

    token = serializer.dumps(f"tools:{ADMIN_USERNAME}")
    json_response = JSONResponse({"code": 0, "msg": "工具页已解锁"})
    json_response.set_cookie(
        key="kaowu_tools",
        value=token,
        httponly=True,
        max_age=TOOLS_UNLOCK_MAX_AGE,
        samesite="lax",
    )
    return json_response


def _download_response(output: BytesIO, filename_utf8: str, filename_ascii: str, media_type: str):
    headers = {
        "Content-Disposition": f'attachment; filename="{filename_ascii}"; filename*=UTF-8\'\'{quote(filename_utf8)}',
        "Content-Type": media_type,
    }
    output.seek(0)
    return StreamingResponse(output, headers=headers, media_type=media_type)


def _ensure_filename(filename: str | None, suffixes: tuple[str, ...], label: str):
    if not filename or not filename.lower().endswith(suffixes):
        raise HTTPException(400, f"{label} 文件格式不正确")


TERM_LABELS = {"spring": "上半年", "fall": "下半年", "other": "其他"}
LEVEL_LABELS = {"cet4": "四级", "cet6": "六级"}
VARIANT_LABELS = {"normal": "正常", "delayed": "延考", "extra": "加考", "other": "其他"}


def _term_from_code(code: str) -> str | None:
    return {"1": "spring", "2": "fall"}.get(code)


def _level_from_code(code: str) -> str | None:
    return {"1": "cet4", "2": "cet6"}.get(code)


def _batch_name(year: int, term: str, level: str, variant: str = "normal", custom_name: str | None = None) -> str:
    if custom_name and custom_name.strip():
        return custom_name.strip()
    base = f"{year}年{TERM_LABELS.get(term, term)}{LEVEL_LABELS.get(level, level)}"
    if variant and variant != "normal":
        base += f"（{VARIANT_LABELS.get(variant, variant)}）"
    return base


def parse_cet_ticket(ticket_no: str | None) -> dict:
    ticket = str(ticket_no or "").strip()
    if not (len(ticket) >= 10 and ticket[:10].isdigit()):
        return {"status": "failed", "reason": "准考证号为空或长度不足", "ticket_no": ticket}
    year = 2000 + int(ticket[6:8])
    term = _term_from_code(ticket[8])
    level = _level_from_code(ticket[9])
    if not term or not level:
        return {"status": "failed", "reason": f"无法识别批次编码：{ticket[6:10]}", "ticket_no": ticket}
    return {
        "status": "auto",
        "exam_year": year,
        "exam_term": term,
        "exam_level": level,
        "batch_variant": "normal",
        "batch_name": _batch_name(year, term, level),
        "ticket_no": ticket,
    }


def _score_float(value):
    try:
        if value is None or str(value).strip() in ("", "--"):
            return None
        return float(value)
    except Exception:
        return None


def _read_dbf_upload_bytes(file_bytes: bytes) -> list[dict]:
    try:
        from dbfread import DBF
    except ImportError as exc:
        raise HTTPException(500, "缺少 dbfread 依赖，请先安装 requirements.txt") from exc

    with tempfile.TemporaryDirectory(prefix="kaowu_dbf_") as tmpdir:
        path = Path(tmpdir) / "score.dbf"
        path.write_bytes(file_bytes)
        table = DBF(str(path), encoding="gbk", load=False)
        return [dict(record) for record in table]


def _score_payload(record: dict) -> dict:
    return {
        "ticket_no": str(record.get("ks_zkz") or "").strip(),
        "id_card": str(record.get("ks_sfz") or "").strip(),
        "student_no": str(record.get("Ks_xh") or record.get("ks_xh") or "").strip(),
        "name": str(record.get("ks_xm") or "").strip(),
        "college": str(record.get("ks_xy_dm") or "").strip(),
        "school_name": str(record.get("dm_mc") or "").strip(),
        "listening_score": _score_float(record.get("tl")),
        "reading_score": _score_float(record.get("yd")),
        "writing_score": _score_float(record.get("xz")),
        "total_score": _score_float(record.get("zf")),
        "raw_level": str(record.get("ks_yyjb") or "").strip(),
    }


def analyze_score_records(records: list[dict], filename: str, db: Session, variant: str = "normal") -> list[dict]:
    groups: dict[tuple, dict] = {}
    unknown_count = 0
    for record in records:
        parsed = parse_cet_ticket(record.get("ks_zkz"))
        if parsed["status"] == "auto":
            key = (parsed["exam_year"], parsed["exam_term"], parsed["exam_level"], variant)
            if key not in groups:
                existing = db.query(CetExamBatch).filter(
                    CetExamBatch.exam_year == parsed["exam_year"],
                    CetExamBatch.exam_term == parsed["exam_term"],
                    CetExamBatch.exam_level == parsed["exam_level"],
                    CetExamBatch.batch_variant == variant,
                ).first()
                groups[key] = {
                    "status": "auto",
                    "filename": filename,
                    "exam_year": parsed["exam_year"],
                    "exam_term": parsed["exam_term"],
                    "exam_level": parsed["exam_level"],
                    "batch_variant": variant,
                    "batch_name": _batch_name(parsed["exam_year"], parsed["exam_term"], parsed["exam_level"], variant),
                    "count": 0,
                    "existing": bool(existing),
                    "existing_count": existing.record_count if existing else 0,
                }
            groups[key]["count"] += 1
        else:
            unknown_count += 1
    result = list(groups.values())
    if unknown_count:
        result.append({
            "status": "failed",
            "filename": filename,
            "batch_name": "无法自动识别",
            "count": unknown_count,
            "existing": False,
            "message": "存在无法从 ks_zkz 识别批次的记录，可手动指定批次后导入",
        })
    return result


def get_or_create_cet_batch(db: Session, year: int, term: str, level: str, variant: str, status: str, source_filename: str, batch_name: str | None = None):
    batch = db.query(CetExamBatch).filter(
        CetExamBatch.exam_year == year,
        CetExamBatch.exam_term == term,
        CetExamBatch.exam_level == level,
        CetExamBatch.batch_variant == variant,
    ).first()
    if not batch:
        batch = CetExamBatch(
            exam_year=year,
            exam_term=term,
            exam_level=level,
            batch_variant=variant,
            batch_name=_batch_name(year, term, level, variant, batch_name),
            recognition_status=status,
            source_filenames=source_filename,
            record_count=0,
        )
        db.add(batch)
        db.flush()
    else:
        batch.batch_name = _batch_name(year, term, level, variant, batch_name) if batch_name else batch.batch_name
        batch.recognition_status = status
        names = set(filter(None, (batch.source_filenames or "").split(";")))
        names.add(source_filename)
        batch.source_filenames = ";".join(sorted(names))
        batch.upload_time = now_beijing()
    return batch


def _upsert_score(db: Session, batch_id: int, payload: dict):
    if not payload["id_card"]:
        return False
    score = db.query(CetScore).filter(
        CetScore.batch_id == batch_id,
        CetScore.id_card == payload["id_card"],
    ).first()
    if not score:
        score = CetScore(batch_id=batch_id, id_card=payload["id_card"], ticket_no=payload["ticket_no"])
        db.add(score)
    score.ticket_no = payload["ticket_no"]
    score.student_no = payload["student_no"]
    score.name = payload["name"]
    score.college = payload["college"]
    score.school_name = payload["school_name"]
    score.listening_score = payload["listening_score"]
    score.reading_score = payload["reading_score"]
    score.writing_score = payload["writing_score"]
    score.total_score = payload["total_score"]
    score.raw_level = payload["raw_level"]
    score.upload_time = now_beijing()
    return True


def _grade_from_upload_name(filename: str) -> str:
    grade_name, _ = extract_grade_from_filename(filename)
    return grade_name


def _graduate_payload(row) -> dict:
    def get_any(*names):
        for name in names:
            if name in row and pd.notna(row[name]):
                return str(row[name]).strip()
        return ""
    return {
        "id_card": get_any("身份证号码", "身份证号", "证件号码", "身份证"),
        "student_no": get_any("学号", "学生学号", "Ks_xh", "ks_xh"),
        "name": get_any("姓名", "学生姓名", "ks_xm"),
        "major": get_any("专业"),
        "education_level": get_any("培养层次", "层次", "学历层次"),
        "college": get_any("学院", "院系", "学院名称"),
    }


def _selected_exam_levels(value: str | None) -> list[str]:
    raw = [item.strip() for item in (value or "cet4,cet6").split(",") if item.strip()]
    levels = [item for item in raw if item in LEVEL_LABELS]
    if not levels:
        raise HTTPException(400, "请选择考试级别")
    return levels


def _graduate_query(db: Session, grade_name: str, education_level: str = "all"):
    batch = db.query(GraduateBatch).filter(GraduateBatch.grade_name == grade_name).first()
    if not batch:
        raise HTTPException(404, "毕业生届别不存在")
    query = db.query(GraduateRecord).filter(GraduateRecord.batch_id == batch.id)
    if education_level and education_level != "all":
        query = query.filter(GraduateRecord.education_level == education_level)
    return batch, query


def compute_cet_pass_rate_stats(
    db: Session,
    grade_name: str,
    education_level: str,
    exam_levels: list[str],
    excluded_majors: list[str],
):
    batch, query = _graduate_query(db, grade_name, education_level)
    graduates = query.all()
    if not graduates:
        raise HTTPException(400, "当前届别/层次下没有毕业生数据")

    graduate_ids = {g.id_card for g in graduates if g.id_card}
    excluded_set = set(excluded_majors or [])
    valid_graduates = [g for g in graduates if (g.major or "") not in excluded_set]
    valid_ids = {g.id_card for g in valid_graduates if g.id_card}

    pass_ids_by_level = {}
    for level in exam_levels:
        rows = db.query(CetScore.id_card).join(
            CetExamBatch,
            CetScore.batch_id == CetExamBatch.id,
        ).filter(
            CetExamBatch.exam_level == level,
            CetScore.total_score >= PASS_SCORE,
            CetScore.id_card.in_(graduate_ids),
        ).all()
        pass_ids_by_level[level] = {row[0] for row in rows}

    summary = []
    for level in exam_levels:
        pass_ids = pass_ids_by_level[level]
        pass_count = len(graduate_ids & pass_ids)
        valid_pass_count = len(valid_ids & pass_ids)
        total = len(graduates)
        valid_total = len(valid_graduates)
        summary.append({
            "grade_name": batch.grade_name,
            "education_level": education_level if education_level != "all" else "全部",
            "exam_level": level,
            "exam_level_label": LEVEL_LABELS[level],
            "total_count": total,
            "pass_count": pass_count,
            "pass_rate": round(pass_count / total * 100, 2) if total else 0,
            "valid_total_count": valid_total,
            "valid_pass_count": valid_pass_count,
            "valid_pass_rate": round(valid_pass_count / valid_total * 100, 2) if valid_total else 0,
        })

    majors = sorted({g.major or "未填写专业" for g in graduates})
    major_details = []
    for major in majors:
        major_graduates = [g for g in graduates if (g.major or "未填写专业") == major]
        major_ids = {g.id_card for g in major_graduates if g.id_card}
        row = {
            "grade_name": batch.grade_name,
            "education_level": education_level if education_level != "all" else "全部",
            "major": major,
            "is_excluded": major in excluded_set,
            "total_count": len(major_graduates),
        }
        for level in exam_levels:
            pass_count = len(major_ids & pass_ids_by_level[level])
            row[f"{level}_pass_count"] = pass_count
            row[f"{level}_pass_rate"] = round(pass_count / len(major_graduates) * 100, 2) if major_graduates else 0
        major_details.append(row)

    list_rows = {}
    for level in exam_levels:
        pass_ids = pass_ids_by_level[level]
        passed = []
        failed = []
        for g in graduates:
            target = passed if g.id_card in pass_ids else failed
            target.append({
                "届别": batch.grade_name,
                "培养层次": g.education_level,
                "专业": g.major,
                "学号": g.student_no,
                "姓名": g.name,
                "身份证号": g.id_card,
                "级别": LEVEL_LABELS[level],
            })
        list_rows[level] = {"passed": passed, "failed": failed}

    return {
        "summary": summary,
        "major_details": major_details,
        "lists": list_rows,
        "conditions": {
            "grade_name": batch.grade_name,
            "education_level": education_level if education_level != "all" else "全部",
            "exam_levels": [LEVEL_LABELS[level] for level in exam_levels],
            "excluded_majors": excluded_majors,
            "pass_score": PASS_SCORE,
            "mode": "累计通过率（全部已入库成绩中任意一次达到分数线即通过）",
        },
    }


def _stats_to_workbook(stats: dict) -> BytesIO:
    output = BytesIO()
    summary_rows = []
    for row in stats["summary"]:
        summary_rows.append({
            "届别": row["grade_name"],
            "培养层次": row["education_level"],
            "级别": row["exam_level_label"],
            "总人数": row["total_count"],
            "通过人数": row["pass_count"],
            "通过率(%)": row["pass_rate"],
            "有效总人数": row["valid_total_count"],
            "有效通过人数": row["valid_pass_count"],
            "有效通过率(%)": row["valid_pass_rate"],
        })

    major_rows = []
    for row in stats["major_details"]:
        item = {
            "届别": row["grade_name"],
            "培养层次": row["education_level"],
            "专业": row["major"],
            "是否排除": "是" if row["is_excluded"] else "否",
            "专业总人数": row["total_count"],
        }
        for summary in stats["summary"]:
            level = summary["exam_level"]
            label = summary["exam_level_label"]
            item[f"{label}通过人数"] = row.get(f"{level}_pass_count", 0)
            item[f"{label}通过率(%)"] = row.get(f"{level}_pass_rate", 0)
        major_rows.append(item)

    conditions = stats["conditions"]
    condition_rows = [
        {"项目": "届别", "值": conditions["grade_name"]},
        {"项目": "培养层次", "值": conditions["education_level"]},
        {"项目": "考试级别", "值": "、".join(conditions["exam_levels"])},
        {"项目": "分数线", "值": conditions["pass_score"]},
        {"项目": "统计口径", "值": conditions["mode"]},
        {"项目": "排除专业", "值": "、".join(conditions["excluded_majors"]) if conditions["excluded_majors"] else "无"},
    ]

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        pd.DataFrame(summary_rows).to_excel(writer, sheet_name="整体通过率汇总", index=False)
        pd.DataFrame(major_rows).to_excel(writer, sheet_name="专业通过率明细", index=False)
        for level, lists in stats["lists"].items():
            label = LEVEL_LABELS[level]
            pd.DataFrame(lists["passed"]).to_excel(writer, sheet_name=f"{label}已通过名单", index=False)
            pd.DataFrame(lists["failed"]).to_excel(writer, sheet_name=f"{label}未通过名单", index=False)
        pd.DataFrame(condition_rows).to_excel(writer, sheet_name="统计条件说明", index=False)
        for ws in writer.book.worksheets:
            for cell in ws[1]:
                cell.font = Font(bold=True)
                cell.alignment = Alignment(horizontal="center")
            for column in ws.columns:
                max_length = max(len(str(cell.value)) if cell.value is not None else 0 for cell in column)
                ws.column_dimensions[column[0].column_letter].width = min(max(max_length + 2, 12), 28)
    output.seek(0)
    return output


@app.get("/api/tools/cet-data/overview")
async def cet_data_overview(request: Request, db: Session = Depends(get_db)):
    check_admin_login(request)
    check_tools_unlocked(request)
    score_batches = db.query(CetExamBatch).order_by(
        CetExamBatch.exam_year.desc(),
        CetExamBatch.exam_term.desc(),
        CetExamBatch.exam_level,
        CetExamBatch.batch_variant,
    ).all()
    graduate_batches = db.query(GraduateBatch).order_by(GraduateBatch.grade_name.desc()).all()
    return {
        "score_batches": [{
            "id": b.id,
            "batch_name": b.batch_name,
            "exam_year": b.exam_year,
            "exam_term": b.exam_term,
            "exam_term_label": TERM_LABELS.get(b.exam_term, b.exam_term),
            "exam_level": b.exam_level,
            "exam_level_label": LEVEL_LABELS.get(b.exam_level, b.exam_level),
            "batch_variant": b.batch_variant,
            "batch_variant_label": VARIANT_LABELS.get(b.batch_variant, b.batch_variant),
            "recognition_status": b.recognition_status,
            "record_count": b.record_count,
            "source_filenames": b.source_filenames,
            "upload_time": b.upload_time.strftime("%Y-%m-%d %H:%M") if b.upload_time else None,
        } for b in score_batches],
        "graduate_batches": [{
            "id": b.id,
            "grade_name": b.grade_name,
            "source_filename": b.source_filename,
            "record_count": b.record_count,
            "upload_time": b.upload_time.strftime("%Y-%m-%d %H:%M") if b.upload_time else None,
        } for b in graduate_batches],
    }


@app.get("/api/tools/cet-pass-rate/options")
async def cet_pass_rate_options(request: Request, db: Session = Depends(get_db)):
    check_admin_login(request)
    check_tools_unlocked(request)
    batches = db.query(GraduateBatch).order_by(GraduateBatch.grade_name.desc()).all()
    return {
        "graduate_batches": [{
            "id": b.id,
            "grade_name": b.grade_name,
            "record_count": b.record_count,
        } for b in batches],
        "default_excluded_majors": EXCLUDE_MAJORS,
    }


@app.get("/api/tools/cet-pass-rate/majors")
async def cet_pass_rate_majors(
    request: Request,
    grade_name: str,
    education_level: str = "all",
    db: Session = Depends(get_db),
):
    check_admin_login(request)
    check_tools_unlocked(request)
    _, query = _graduate_query(db, grade_name, education_level)
    majors = [
        row[0]
        for row in query.with_entities(GraduateRecord.major)
        .filter(GraduateRecord.major != None)
        .distinct()
        .order_by(GraduateRecord.major)
        .all()
        if row[0]
    ]
    major_set = set(majors)
    return {
        "majors": majors,
        "default_excluded_majors": [major for major in EXCLUDE_MAJORS if major in major_set],
    }


@app.post("/api/tools/cet-pass-rate/analyze")
async def cet_pass_rate_analyze(
    request: Request,
    grade_name: str = Form(...),
    education_level: str = Form("all"),
    exam_levels: str = Form("cet4,cet6"),
    excluded_majors: str = Form("[]"),
    db: Session = Depends(get_db),
):
    check_admin_login(request)
    check_csrf(request)
    check_tools_unlocked(request)
    try:
        excluded = json.loads(excluded_majors)
        if not isinstance(excluded, list):
            excluded = []
    except Exception:
        excluded = []
    stats = compute_cet_pass_rate_stats(
        db,
        grade_name.strip(),
        education_level,
        _selected_exam_levels(exam_levels),
        [str(item) for item in excluded],
    )
    stats.pop("lists", None)
    return stats


@app.post("/api/tools/cet-pass-rate/export")
async def cet_pass_rate_export(
    request: Request,
    grade_name: str = Form(...),
    education_level: str = Form("all"),
    exam_levels: str = Form("cet4,cet6"),
    excluded_majors: str = Form("[]"),
    db: Session = Depends(get_db),
):
    check_admin_login(request)
    check_csrf(request)
    check_tools_unlocked(request)
    try:
        excluded = json.loads(excluded_majors)
        if not isinstance(excluded, list):
            excluded = []
    except Exception:
        excluded = []
    stats = compute_cet_pass_rate_stats(
        db,
        grade_name.strip(),
        education_level,
        _selected_exam_levels(exam_levels),
        [str(item) for item in excluded],
    )
    output = _stats_to_workbook(stats)
    return _download_response(
        output,
        f"{grade_name}_四六级累计通过率统计.xlsx",
        "cet_pass_rate.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.post("/api/tools/cet-scores/precheck")
async def cet_scores_precheck(
    request: Request,
    files: list[UploadFile] = File(...),
    batch_variant: str = Form("normal"),
    db: Session = Depends(get_db),
):
    check_admin_login(request)
    check_csrf(request)
    check_tools_unlocked(request)
    if batch_variant not in VARIANT_LABELS:
        raise HTTPException(400, "批次类型无效")

    results = []
    for file in files:
        _ensure_filename(file.filename, (".dbf",), "成绩 DBF")
        try:
            records = _read_dbf_upload_bytes(await file.read())
            groups = analyze_score_records(records, file.filename or "score.dbf", db, batch_variant)
            status = "auto"
            if any(g["status"] == "failed" for g in groups):
                status = "failed"
            elif len(groups) > 1:
                status = "multiple"
            results.append({
                "filename": file.filename,
                "record_count": len(records),
                "status": status,
                "groups": groups,
            })
        except Exception as e:
            results.append({
                "filename": file.filename,
                "record_count": 0,
                "status": "error",
                "groups": [],
                "message": str(e),
            })
    return {"files": results}


@app.post("/api/tools/cet-scores/import")
async def cet_scores_import(
    request: Request,
    files: list[UploadFile] = File(...),
    import_mode: str = Form("overwrite"),
    batch_variant: str = Form("normal"),
    manual_year: int | None = Form(None),
    manual_term: str | None = Form(None),
    manual_level: str | None = Form(None),
    manual_batch_name: str | None = Form(None),
    db: Session = Depends(get_db),
):
    check_admin_login(request)
    check_csrf(request)
    check_tools_unlocked(request)
    if import_mode not in ("overwrite", "merge"):
        raise HTTPException(400, "导入方式无效")
    if batch_variant not in VARIANT_LABELS:
        raise HTTPException(400, "批次类型无效")
    if manual_term and manual_term not in TERM_LABELS:
        raise HTTPException(400, "手动批次无效")
    if manual_level and manual_level not in LEVEL_LABELS:
        raise HTTPException(400, "手动级别无效")

    imported = 0
    skipped = 0
    affected_batches = {}
    overwritten_batch_ids = set()

    for file in files:
        _ensure_filename(file.filename, (".dbf",), "成绩 DBF")
        records = _read_dbf_upload_bytes(await file.read())
        for record in records:
            parsed = parse_cet_ticket(record.get("ks_zkz"))
            status = "auto"
            if parsed["status"] == "auto":
                year = parsed["exam_year"]
                term = parsed["exam_term"]
                level = parsed["exam_level"]
            else:
                if not (manual_year and manual_term and manual_level):
                    skipped += 1
                    continue
                year = manual_year
                term = manual_term
                level = manual_level
                status = "manual"

            batch = get_or_create_cet_batch(
                db,
                year,
                term,
                level,
                batch_variant,
                status,
                file.filename or "score.dbf",
                manual_batch_name,
            )
            if import_mode == "overwrite" and batch.id not in overwritten_batch_ids:
                db.query(CetScore).filter(CetScore.batch_id == batch.id).delete(synchronize_session=False)
                overwritten_batch_ids.add(batch.id)
            if _upsert_score(db, batch.id, _score_payload(record)):
                imported += 1
                affected_batches[batch.id] = batch
            else:
                skipped += 1

    db.flush()
    for batch_id, batch in affected_batches.items():
        batch.record_count = db.query(func.count(CetScore.id)).filter(CetScore.batch_id == batch_id).scalar() or 0
        batch.upload_time = now_beijing()
    db.commit()

    return {
        "code": 0,
        "msg": f"导入完成：成功 {imported} 条，跳过 {skipped} 条",
        "imported": imported,
        "skipped": skipped,
        "batches": [{
            "id": b.id,
            "batch_name": b.batch_name,
            "record_count": b.record_count,
        } for b in affected_batches.values()],
    }


@app.post("/api/tools/graduates/precheck")
async def graduates_precheck(
    request: Request,
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
):
    check_admin_login(request)
    check_csrf(request)
    check_tools_unlocked(request)
    results = []
    for file in files:
        _ensure_filename(file.filename, (".xlsx", ".xls"), "毕业生届别")
        try:
            content = await file.read()
            df = pd.read_excel(BytesIO(content))
            grade_name = _grade_from_upload_name(file.filename or "")
            existing = db.query(GraduateBatch).filter(GraduateBatch.grade_name == grade_name).first()
            results.append({
                "filename": file.filename,
                "grade_name": grade_name,
                "record_count": len(df),
                "existing": bool(existing),
                "existing_count": existing.record_count if existing else 0,
                "status": "auto",
            })
        except Exception as e:
            results.append({
                "filename": file.filename,
                "grade_name": "",
                "record_count": 0,
                "existing": False,
                "status": "error",
                "message": str(e),
            })
    return {"files": results}


@app.post("/api/tools/graduates/import")
async def graduates_import(
    request: Request,
    files: list[UploadFile] = File(...),
    import_mode: str = Form("overwrite"),
    db: Session = Depends(get_db),
):
    check_admin_login(request)
    check_csrf(request)
    check_tools_unlocked(request)
    if import_mode not in ("overwrite", "merge"):
        raise HTTPException(400, "导入方式无效")

    imported = 0
    skipped = 0
    batches = []
    for file in files:
        _ensure_filename(file.filename, (".xlsx", ".xls"), "毕业生届别")
        df = pd.read_excel(BytesIO(await file.read()))
        grade_name = _grade_from_upload_name(file.filename or "")
        if "身份证号码" not in df.columns and "身份证号" not in df.columns and "证件号码" not in df.columns:
            raise HTTPException(400, f"{file.filename} 缺少身份证号码列")

        batch = db.query(GraduateBatch).filter(GraduateBatch.grade_name == grade_name).first()
        if not batch:
            batch = GraduateBatch(grade_name=grade_name, source_filename=file.filename, record_count=0)
            db.add(batch)
            db.flush()
        else:
            batch.source_filename = file.filename
            batch.upload_time = now_beijing()
        if import_mode == "overwrite":
            db.query(GraduateRecord).filter(GraduateRecord.batch_id == batch.id).delete(synchronize_session=False)

        for _, row in df.iterrows():
            payload = _graduate_payload(row)
            if not payload["id_card"]:
                skipped += 1
                continue
            record = db.query(GraduateRecord).filter(
                GraduateRecord.batch_id == batch.id,
                GraduateRecord.id_card == payload["id_card"],
            ).first()
            if not record:
                record = GraduateRecord(batch_id=batch.id, id_card=payload["id_card"])
                db.add(record)
            record.student_no = payload["student_no"]
            record.name = payload["name"]
            record.major = payload["major"]
            record.education_level = payload["education_level"]
            record.college = payload["college"]
            imported += 1

        db.flush()
        batch.record_count = db.query(func.count(GraduateRecord.id)).filter(GraduateRecord.batch_id == batch.id).scalar() or 0
        batches.append(batch)
    db.commit()

    return {
        "code": 0,
        "msg": f"导入完成：成功 {imported} 条，跳过 {skipped} 条",
        "imported": imported,
        "skipped": skipped,
        "batches": [{"id": b.id, "grade_name": b.grade_name, "record_count": b.record_count} for b in batches],
    }


@app.get("/api/tools/cet-student-query")
async def cet_student_query(request: Request, id_card: str, db: Session = Depends(get_db)):
    check_admin_login(request)
    check_tools_unlocked(request)
    cleaned_id = id_card.strip()
    if not cleaned_id:
        raise HTTPException(400, "请输入身份证号")

    rows = db.query(CetScore, CetExamBatch).join(
        CetExamBatch,
        CetScore.batch_id == CetExamBatch.id,
    ).filter(CetScore.id_card == cleaned_id).order_by(
        CetExamBatch.exam_year,
        CetExamBatch.exam_term,
        CetExamBatch.exam_level,
        CetExamBatch.batch_variant,
    ).all()
    return [{
        "batch_name": batch.batch_name,
        "exam_year": batch.exam_year,
        "exam_term": batch.exam_term,
        "exam_term_label": TERM_LABELS.get(batch.exam_term, batch.exam_term),
        "exam_level": batch.exam_level,
        "exam_level_label": LEVEL_LABELS.get(batch.exam_level, batch.exam_level),
        "batch_variant": batch.batch_variant,
        "batch_variant_label": VARIANT_LABELS.get(batch.batch_variant, batch.batch_variant),
        "ticket_no": score.ticket_no,
        "id_card": score.id_card,
        "student_no": score.student_no,
        "name": score.name,
        "college": score.college,
        "listening_score": score.listening_score,
        "reading_score": score.reading_score,
        "writing_score": score.writing_score,
        "total_score": score.total_score,
        "passed": (score.total_score or 0) >= PASS_SCORE,
    } for score, batch in rows]


@app.post("/api/tools/invigilator-assign")
async def tool_invigilator_assign(
    request: Request,
    teachers_file: UploadFile = File(...),
    rooms_file: UploadFile = File(...),
    room_count: int = Form(...),
):
    check_admin_login(request)
    check_csrf(request)
    check_tools_unlocked(request)
    rate_limit(f"tool_invigilator_{get_client_ip(request)}", max_requests=5, window=60)
    _ensure_filename(teachers_file.filename, (".xlsx", ".xls"), "监考员表")
    _ensure_filename(rooms_file.filename, (".xlsx", ".xls"), "考场表")
    try:
        output = assign_invigilators(
            await teachers_file.read(),
            await rooms_file.read(),
            room_count,
        )
    except Exception as e:
        raise HTTPException(400, f"监考员分配失败：{str(e)}")
    return _download_response(
        output,
        "监考员分配结果.xlsx",
        "invigilator_assignment.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.get("/api/tools/invigilator/templates/{template_type}")
async def invigilator_template(request: Request, template_type: str):
    check_admin_login(request)
    check_tools_unlocked(request)
    if template_type == "teachers":
        df = pd.DataFrame({
            "id": [1001, 1002, 1003, 1004],
            "name": ["张三", "李四", "王五", "赵六"],
            "gender": ["男", "女", "男", "女"],
            "college": ["文学与历史学院", "教师教育学院", "数学学院", "经济与管理学院"],
        })
        filename_utf8 = "监考员表模板.xlsx"
        filename_ascii = "invigilator_teachers_template.xlsx"
        sheet_name = "监考员表"
    elif template_type == "rooms":
        df = pd.DataFrame({
            "room_no": [1, 2, 3],
            "room_name": ["B101-1", "B102-1", "综合楼101"],
        })
        filename_utf8 = "考场表模板.xlsx"
        filename_ascii = "invigilator_rooms_template.xlsx"
        sheet_name = "考场表"
    else:
        raise HTTPException(404, "模板不存在")

    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name=sheet_name)
        ws = writer.book[sheet_name]
        for cell in ws[1]:
            cell.font = Font(bold=True)
            cell.alignment = Alignment(horizontal="center")
        for column in ws.columns:
            max_length = max(len(str(cell.value)) if cell.value is not None else 0 for cell in column)
            ws.column_dimensions[column[0].column_letter].width = min(max(max_length + 2, 12), 24)
    output.seek(0)
    return _download_response(
        output,
        filename_utf8,
        filename_ascii,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.post("/api/tools/seat-labels")
async def tool_seat_labels(
    request: Request,
    num_rooms: int = Form(...),
    num_seats: int = Form(30),
    cols: int = Form(3),
    rows: int = Form(10),
    font_size: int = Form(40),
):
    check_admin_login(request)
    check_csrf(request)
    check_tools_unlocked(request)
    rate_limit(f"tool_seat_labels_{get_client_ip(request)}", max_requests=10, window=60)
    try:
        output = generate_seat_labels_pdf(num_rooms, num_seats, cols, rows, font_size)
    except Exception as e:
        raise HTTPException(400, f"考场桌贴生成失败：{str(e)}")
    return _download_response(output, "考场桌贴.pdf", "seat_labels.pdf", "application/pdf")


@app.post("/api/tools/merge-workbook")
async def tool_merge_workbook(request: Request, workbook_file: UploadFile = File(...)):
    check_admin_login(request)
    check_csrf(request)
    check_tools_unlocked(request)
    rate_limit(f"tool_merge_workbook_{get_client_ip(request)}", max_requests=10, window=60)
    _ensure_filename(workbook_file.filename, (".xlsx", ".xlsm"), "工作簿")
    try:
        output = merge_excel_sheets(await workbook_file.read())
    except Exception as e:
        raise HTTPException(400, f"工作簿合并失败：{str(e)}")
    return _download_response(
        output,
        "工作簿多表合并结果.xlsx",
        "merged_workbook.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.post("/api/tools/cet-pass-rate")
async def tool_cet_pass_rate(
    request: Request,
    graduate_files: list[UploadFile] = File(...),
    cet4_files: list[UploadFile] = File(...),
    cet6_files: list[UploadFile] = File(...),
):
    check_admin_login(request)
    check_csrf(request)
    check_tools_unlocked(request)
    rate_limit(f"tool_cet_pass_rate_{get_client_ip(request)}", max_requests=3, window=300)

    for file in graduate_files:
        _ensure_filename(file.filename, (".xlsx", ".xls"), "毕业生届别")
    for file in cet4_files:
        _ensure_filename(file.filename, (".dbf",), "四级成绩")
    for file in cet6_files:
        _ensure_filename(file.filename, (".dbf",), "六级成绩")

    try:
        graduate_payloads = [(file.filename or "毕业生届别.xlsx", await file.read()) for file in graduate_files]
        with tempfile.TemporaryDirectory(prefix="kaowu_tools_") as tmpdir:
            cet4_paths = []
            cet6_paths = []
            for index, file in enumerate(cet4_files, start=1):
                path = Path(tmpdir) / f"cet4_{index}.dbf"
                path.write_bytes(await file.read())
                cet4_paths.append(str(path))
            for index, file in enumerate(cet6_files, start=1):
                path = Path(tmpdir) / f"cet6_{index}.dbf"
                path.write_bytes(await file.read())
                cet6_paths.append(str(path))
            output = calculate_cet_pass_rates(graduate_payloads, cet4_paths, cet6_paths)
    except Exception as e:
        raise HTTPException(400, f"四六级通过率统计失败：{str(e)}")

    return _download_response(output, "四六级通过率统计结果.zip", "cet_pass_rate_results.zip", "application/zip")


# ==================== 考场基础数据 API ====================

@app.get("/api/classrooms/template")
async def download_classroom_template():
    """下载教室导入模板（.xlsx）"""
    df = pd.DataFrame({
        "教学楼": ["树人楼", "树人楼", "综合楼"],
        "教室名称": ["B101", "A201", "101"],
        "是否固定桌椅": ["否", "否", "是"],
        "是否具备双考场条件": ["是", "否", "是"],
    })
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name="教室导入模板")
    output.seek(0)

    headers = {
        "Content-Disposition": 'attachment; filename="classroom_import_template.xlsx"',
        "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    }
    return StreamingResponse(output, headers=headers, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.post("/api/classrooms/import")
async def import_classrooms(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db)):
    check_admin_login(request)
    check_csrf(request)
    rate_limit(f"classroom_import_{get_client_ip(request)}", max_requests=5, window=60)

    if not file.filename.endswith(('.xlsx', '.xls')):
        raise HTTPException(400, "请上传 .xlsx 或 .xls 文件")

    try:
        contents = await file.read()
        df = pd.read_excel(BytesIO(contents))

        # 校验必要列
        required_cols = {'教学楼', '教室名称', '是否固定桌椅', '是否具备双考场条件'}
        if not required_cols.issubset(df.columns):
            raise HTTPException(400, f"Excel 缺少必要列：{required_cols}")

        imported = 0
        errors = []

        for idx, row in df.iterrows():
            building_name = str(row['教学楼']).strip()
            classroom_name = str(row['教室名称']).strip()
            is_fixed = str(row['是否固定桌椅']).strip() in ('是', '1', 'true', 'True')
            can_double = str(row['是否具备双考场条件']).strip() in ('是', '1', 'true', 'True')

            if not classroom_name:
                errors.append(f"第{idx+2}行：教室名称为空")
                continue

            # 查找或创建教学楼
            building = db.query(Building).filter(Building.name == building_name).first()
            if not building:
                building = Building(name=building_name)
                db.add(building)
                db.flush()

            # 检查重复
            existing = db.query(Classroom).filter(
                Classroom.building_id == building.id,
                Classroom.name == classroom_name
            ).first()
            if existing:
                existing.is_fixed_seats = is_fixed
                existing.can_double_exam = can_double
            else:
                classroom = Classroom(
                    building_id=building.id,
                    name=classroom_name,
                    is_fixed_seats=is_fixed,
                    can_double_exam=can_double
                )
                db.add(classroom)
            imported += 1

        db.commit()
        return {"code": 0, "msg": f"成功导入/更新 {imported} 条记录", "errors": errors}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"导入失败：{str(e)}")


@app.get("/api/buildings")
async def get_buildings(db: Session = Depends(get_db)):
    """获取所有教学楼"""
    buildings = db.query(Building).order_by(Building.id).all()
    return [{"id": b.id, "name": b.name} for b in buildings]


@app.get("/api/classrooms")
async def get_classrooms(building_id: int = None, include_disabled: bool = False, db: Session = Depends(get_db)):
    """获取教室列表，可按教学楼筛选"""
    query = db.query(Classroom)
    if building_id:
        query = query.filter(Classroom.building_id == building_id)
    if not include_disabled:
        query = query.filter(Classroom.is_enabled == True)
    classrooms = query.order_by(Classroom.building_id, Classroom.name).all()

    # 批量获取 building 名称
    building_ids = {c.building_id for c in classrooms}
    building_map = {}
    if building_ids:
        for b in db.query(Building).filter(Building.id.in_(building_ids)).all():
            building_map[b.id] = b.name

    result = []
    for c in classrooms:
        zone = detect_zone(c.name)
        result.append({
            "id": c.id,
            "building_id": c.building_id,
            "building_name": building_map.get(c.building_id, ""),
            "name": c.name,
            "zone": zone,
            "is_fixed_seats": c.is_fixed_seats,
            "can_double_exam": c.can_double_exam,
            "is_enabled": c.is_enabled,
        })
    return result


@app.post("/api/classrooms")
async def create_classroom(
    request: Request,
    building_id: int = Form(...),
    name: str = Form(...),
    is_fixed_seats: bool = Form(False),
    can_double_exam: bool = Form(False),
    db: Session = Depends(get_db)
):
    check_admin_login(request)
    check_csrf(request)

    building = db.query(Building).filter(Building.id == building_id).first()
    if not building:
        raise HTTPException(400, "教学楼不存在")

    name = name.strip()
    if not name:
        raise HTTPException(400, "教室名称不能为空")

    exists = db.query(Classroom).filter(
        Classroom.building_id == building_id,
        Classroom.name == name
    ).first()
    if exists:
        raise HTTPException(400, f"教室 {name} 已存在")

    classroom = Classroom(
        building_id=building_id,
        name=name,
        is_fixed_seats=is_fixed_seats,
        can_double_exam=can_double_exam
    )
    db.add(classroom)
    db.commit()
    db.refresh(classroom)
    return {"code": 0, "msg": "添加成功", "id": classroom.id}


@app.put("/api/classrooms/{classroom_id}")
async def update_classroom(
    request: Request,
    classroom_id: int,
    name: str = Form(None),
    building_id: int = Form(None),
    is_fixed_seats: bool = Form(False),
    can_double_exam: bool = Form(False),
    is_enabled: bool = Form(True),
    db: Session = Depends(get_db)
):
    check_admin_login(request)
    check_csrf(request)

    classroom = db.query(Classroom).filter(Classroom.id == classroom_id).first()
    if not classroom:
        raise HTTPException(404, "教室不存在")

    if name is not None:
        name = name.strip()
        if not name:
            raise HTTPException(400, "教室名称不能为空")
        if building_id is None:
            building_id = classroom.building_id
        # Check duplicate
        dup = db.query(Classroom).filter(
            Classroom.building_id == building_id,
            Classroom.name == name,
            Classroom.id != classroom_id
        ).first()
        if dup:
            raise HTTPException(400, f"教室名称 {name} 已存在")
        classroom.name = name

    if building_id is not None:
        building = db.query(Building).filter(Building.id == building_id).first()
        if not building:
            raise HTTPException(400, "教学楼不存在")
        classroom.building_id = building_id

    classroom.is_fixed_seats = is_fixed_seats
    classroom.can_double_exam = can_double_exam
    classroom.is_enabled = is_enabled
    db.commit()
    return {"code": 0, "msg": "修改成功"}


@app.delete("/api/classrooms/{classroom_id}")
async def delete_classroom(
    request: Request,
    classroom_id: int,
    db: Session = Depends(get_db)
):
    check_admin_login(request)
    check_csrf(request)

    classroom = db.query(Classroom).filter(Classroom.id == classroom_id).first()
    if not classroom:
        raise HTTPException(404, "教室不存在")

    usage_count = db.query(func.count(RecruitmentClassroom.id)).filter(
        RecruitmentClassroom.classroom_id == classroom_id
    ).scalar()
    if usage_count:
        raise HTTPException(400, f"该教室已被 {usage_count} 个招募使用，不能删除；请改为禁用")

    db.delete(classroom)
    db.commit()
    return {"code": 0, "msg": "删除成功"}


@app.put("/api/classrooms/{classroom_id}/toggle-enabled")
async def toggle_classroom_enabled(
    request: Request,
    classroom_id: int,
    db: Session = Depends(get_db)
):
    """切换教室的启用/禁用状态"""
    check_admin_login(request)
    check_csrf(request)

    classroom = db.query(Classroom).filter(Classroom.id == classroom_id).first()
    if not classroom:
        raise HTTPException(404, "教室不存在")

    classroom.is_enabled = not classroom.is_enabled
    db.commit()
    status = "启用" if classroom.is_enabled else "禁用"
    return {"code": 0, "msg": f"教室已{status}", "is_enabled": classroom.is_enabled}


@app.get("/api/recruit/{recruit_id}/classrooms")
async def get_recruit_classrooms(recruit_id: int, db: Session = Depends(get_db)):
    """获取某个招募的考场配置"""
    recruit = db.query(Recruitment).filter(Recruitment.id == recruit_id).first()
    if not recruit:
        raise HTTPException(404, "招募不存在")
    return serialize_recruit_classrooms(recruit_id, db)


@app.get("/api/recruit/{recruit_id}/manual-grouping-data")
async def get_manual_grouping_data(recruit_id: int, db: Session = Depends(get_db)):
    """获取手动分组所需全部数据"""
    recruit = db.query(Recruitment).filter(Recruitment.id == recruit_id).first()
    if not recruit:
        raise HTTPException(404, "招募不存在")

    registrations = db.query(Registration).filter(
        Registration.recruitment_id == recruit_id
    ).order_by(Registration.id).all()

    reg_list = [{
        "id": r.id, "student_id": r.student_id, "name": r.name,
        "gender": r.gender, "has_experience": r.has_experience,
    } for r in registrations]

    general = recruit.general_supervisor_id

    bs_list = db.query(BuildingSupervisor).filter(
        BuildingSupervisor.recruitment_id == recruit_id
    ).all()
    supervisors = [{"id": bs.id, "zone_name": bs.zone_name, "registration_id": bs.registration_id} for bs in bs_list]

    groups = db.query(RecruitmentGroup).filter(
        RecruitmentGroup.recruitment_id == recruit_id
    ).all()

    groups_data = []
    for g in groups:
        members = db.query(RecruitmentGroupMember).filter(
            RecruitmentGroupMember.group_id == g.id
        ).all()
        member_ids = [m.registration_id for m in members]

        classrooms = db.query(RecruitmentGroupClassroom).filter(
            RecruitmentGroupClassroom.group_id == g.id
        ).all()
        rc_ids = [c.recruitment_classroom_id for c in classrooms]

        groups_data.append({
            "id": g.id,
            "zone_name": g.zone_name,
            "is_supervisor": g.is_supervisor,
            "member_ids": member_ids,
            "classroom_rc_ids": rc_ids,
        })

    rcs = db.query(RecruitmentClassroom).filter(
        RecruitmentClassroom.recruitment_id == recruit_id
    ).all()
    classrooms_info = []
    for rc in rcs:
        cr = db.query(Classroom).filter(Classroom.id == rc.classroom_id).first()
        if cr:
            classrooms_info.append({
                "rc_id": rc.id,
                "classroom_id": rc.classroom_id,
                "name": cr.name,
                "zone": detect_zone(cr.name) or "未分区",
                "exam_count": 2 if rc.exam_mode == "double" else 1,
            })

    is_finalized = db.query(TaskProgress).join(
        RecruitmentClassroom, TaskProgress.recruitment_classroom_id == RecruitmentClassroom.id
    ).filter(
        RecruitmentClassroom.recruitment_id == recruit_id
    ).count() > 0

    return {
        "recruit_id": recruit_id,
        "general_supervisor_id": general,
        "supervisors": supervisors,
        "registrations": reg_list,
        "groups": groups_data,
        "classrooms": classrooms_info,
        "is_finalized": is_finalized,
    }


@app.put("/api/recruit/{recruit_id}/general-supervisor")
async def set_general_supervisor(
    request: Request, recruit_id: int,
    registration_id: int | None = Form(None),
    db: Session = Depends(get_db)
):
    """设置或移除总负责人"""
    check_admin_login(request)
    check_csrf(request)
    _check_not_finalized(recruit_id, db)

    recruit = db.query(Recruitment).filter(Recruitment.id == recruit_id).first()
    if not recruit:
        raise HTTPException(404, "招募不存在")

    if registration_id is not None and registration_id > 0:
        reg = db.query(Registration).filter(
            Registration.id == registration_id,
            Registration.recruitment_id == recruit_id
        ).first()
        if not reg:
            raise HTTPException(400, "该报名记录不存在或不在此招募中")

    recruit.general_supervisor_id = registration_id if registration_id and registration_id > 0 else None
    db.commit()
    is_set = recruit.general_supervisor_id is not None
    return {"code": 0, "msg": "总负责人已设置" if is_set else "总负责人已移除",
            "general_supervisor_id": recruit.general_supervisor_id}


@app.put("/api/recruit/{recruit_id}/building-supervisors")
async def set_building_supervisors(
    request: Request, recruit_id: int,
    db: Session = Depends(get_db)
):
    """批量设置楼栋负责人"""
    check_admin_login(request)
    check_csrf(request)
    _check_not_finalized(recruit_id, db)

    body = await request.json()
    supervisors = body.get("supervisors", [])

    recruit = db.query(Recruitment).filter(Recruitment.id == recruit_id).first()
    if not recruit:
        raise HTTPException(404, "招募不存在")

    db.query(BuildingSupervisor).filter(
        BuildingSupervisor.recruitment_id == recruit_id
    ).delete()

    for sv in supervisors:
        zone = sv.get("zone_name", "").strip()
        reg_id = sv.get("registration_id")
        if zone and reg_id:
            db.add(BuildingSupervisor(
                recruitment_id=recruit_id,
                zone_name=zone,
                registration_id=reg_id,
            ))

    db.commit()
    return {"code": 0, "msg": "楼栋负责人已保存"}


def _secure_shuffle(lst):
    """Fisher-Yates shuffle using secrets module for true randomness."""
    for i in range(len(lst) - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        lst[i], lst[j] = lst[j], lst[i]


def auto_group_members(registrations, excluded_ids):
    """四人桶配对算法：尽量 2 人一组，经验混搭、性别混搭，允许落单。"""
    buckets = {"exp_male": [], "exp_female": [], "new_male": [], "new_female": []}
    for r in registrations:
        if r.id in excluded_ids:
            continue
        exp = "exp" if r.has_experience else "new"
        gender = "male" if r.gender == "男" else "female"
        buckets[f"{exp}_{gender}"].append(r.id)

    for key in buckets:
        _secure_shuffle(buckets[key])

    pairs = []

    # Phase 1: 跨经验（最高优先级，不限性别）
    all_exp = buckets["exp_male"] + buckets["exp_female"]
    all_new = buckets["new_male"] + buckets["new_female"]
    _secure_shuffle(all_exp)
    _secure_shuffle(all_new)
    used = set()
    while all_exp and all_new:
        pairs.append([all_exp.pop(), all_new.pop()])
        used.add(pairs[-1][0])
        used.add(pairs[-1][1])
    # 从原始桶中移除已配对的
    for key in buckets:
        buckets[key] = [x for x in buckets[key] if x not in used]

    # Phase 2: 同经验跨性别（次优先级）
    for e in ("exp", "new"):
        male_list = buckets[f"{e}_male"]
        female_list = buckets[f"{e}_female"]
        while male_list and female_list:
            pairs.append([male_list.pop(), female_list.pop()])

    # Phase 3: 剩余混合
    remaining = []
    for key in buckets:
        remaining.extend(buckets[key])
    _secure_shuffle(remaining)
    while len(remaining) >= 2:
        pairs.append([remaining.pop(), remaining.pop()])

    # Phase 4: 落单
    if remaining:
        pairs.append([remaining.pop()])

    # Post-process: random swaps ensure variety even with balanced buckets
    if len(pairs) >= 2:
        swaps = max(1, len(pairs) // 2)
        for _ in range(swaps):
            a = secrets.randbelow(len(pairs))
            b = secrets.randbelow(len(pairs))
            if a != b and pairs[a] and pairs[b]:
                ai = secrets.randbelow(len(pairs[a]))
                bi = secrets.randbelow(len(pairs[b]))
                pairs[a][ai], pairs[b][bi] = pairs[b][bi], pairs[a][ai]

    return pairs


@app.post("/api/recruit/{recruit_id}/auto-group")
async def auto_group(request: Request, recruit_id: int, db: Session = Depends(get_db)):
    """自动分组：排除总负责人和楼栋负责人后，运行配对算法"""
    check_admin_login(request)
    check_csrf(request)
    _check_not_finalized(recruit_id, db)

    recruit = db.query(Recruitment).filter(Recruitment.id == recruit_id).first()
    if not recruit:
        raise HTTPException(404, "招募不存在")

    excluded_ids = set()
    if recruit.general_supervisor_id:
        excluded_ids.add(recruit.general_supervisor_id)
    for bs in db.query(BuildingSupervisor).filter(
        BuildingSupervisor.recruitment_id == recruit_id
    ).all():
        excluded_ids.add(bs.registration_id)

    regs = db.query(Registration).filter(
        Registration.recruitment_id == recruit_id
    ).all()
    if excluded_ids:
        regs = [r for r in regs if r.id not in excluded_ids]
    if not regs:
        raise HTTPException(400, "没有可分组的人员（所有人都已被指定为负责人）")

    pairs = auto_group_members(regs, excluded_ids)

    existing_ids = [
        g.id for g in db.query(RecruitmentGroup).filter(
            RecruitmentGroup.recruitment_id == recruit_id
        ).all()
    ]
    if existing_ids:
        db.query(RecruitmentGroupMember).filter(
            RecruitmentGroupMember.group_id.in_(existing_ids)
        ).delete(synchronize_session=False)
        db.query(RecruitmentGroupClassroom).filter(
            RecruitmentGroupClassroom.group_id.in_(existing_ids)
        ).delete(synchronize_session=False)
        db.query(RecruitmentGroup).filter(
            RecruitmentGroup.id.in_(existing_ids)
        ).delete(synchronize_session=False)
    db.flush()

    for pair in pairs:
        group = RecruitmentGroup(recruitment_id=recruit_id)
        db.add(group)
        db.flush()
        for rid in pair:
            db.add(RecruitmentGroupMember(group_id=group.id, registration_id=rid))

    db.commit()
    return {"code": 0, "msg": f"已自动分成 {len(pairs)} 组", "group_count": len(pairs)}


@app.post("/api/recruit/{recruit_id}/groups")
async def create_group(request: Request, recruit_id: int, db: Session = Depends(get_db)):
    """创建新组"""
    check_admin_login(request)
    check_csrf(request)
    _check_not_finalized(recruit_id, db)

    group = RecruitmentGroup(recruitment_id=recruit_id)
    db.add(group)
    db.commit()
    db.refresh(group)
    return {"code": 0, "msg": "组已创建", "group_id": group.id}


@app.delete("/api/recruit/{recruit_id}/groups/{group_id}")
async def delete_group(request: Request, recruit_id: int, group_id: int, db: Session = Depends(get_db)):
    """删除空组"""
    check_admin_login(request)
    check_csrf(request)
    _check_not_finalized(recruit_id, db)

    group = db.query(RecruitmentGroup).filter(
        RecruitmentGroup.id == group_id,
        RecruitmentGroup.recruitment_id == recruit_id
    ).first()
    if not group:
        raise HTTPException(404, "组不存在")

    members = db.query(RecruitmentGroupMember).filter(
        RecruitmentGroupMember.group_id == group_id
    ).count()
    if members > 0:
        raise HTTPException(400, "该组还有成员，请先移除所有成员再删除")

    db.query(RecruitmentGroupClassroom).filter(
        RecruitmentGroupClassroom.group_id == group_id
    ).delete()
    db.delete(group)
    db.commit()
    return {"code": 0, "msg": "组已删除"}


@app.put("/api/recruit/{recruit_id}/groups/{group_id}/members")
async def set_group_members(
    request: Request, recruit_id: int, group_id: int,
    db: Session = Depends(get_db)
):
    """设置组成员（全量替换）"""
    check_admin_login(request)
    check_csrf(request)
    _check_not_finalized(recruit_id, db)

    body = await request.json()
    member_ids = body.get("member_ids", [])

    group = db.query(RecruitmentGroup).filter(
        RecruitmentGroup.id == group_id,
        RecruitmentGroup.recruitment_id == recruit_id
    ).first()
    if not group:
        raise HTTPException(404, "组不存在")

    db.query(RecruitmentGroupMember).filter(
        RecruitmentGroupMember.group_id == group_id
    ).delete()
    db.flush()

    seen = set()
    for rid in member_ids:
        if rid in seen:
            continue
        seen.add(rid)
        reg = db.query(Registration).filter(
            Registration.id == rid,
            Registration.recruitment_id == recruit_id
        ).first()
        if reg:
            db.add(RecruitmentGroupMember(group_id=group_id, registration_id=rid))

    db.commit()
    return {"code": 0, "msg": "组成员已更新"}


@app.put("/api/recruit/{recruit_id}/groups/{group_id}/classrooms")
async def set_group_classrooms(
    request: Request, recruit_id: int, group_id: int,
    db: Session = Depends(get_db)
):
    """设置组负责的教室（全量替换，互斥检查）"""
    check_admin_login(request)
    check_csrf(request)
    _check_not_finalized(recruit_id, db)

    body = await request.json()
    rc_ids = body.get("rc_ids", [])

    group = db.query(RecruitmentGroup).filter(
        RecruitmentGroup.id == group_id,
        RecruitmentGroup.recruitment_id == recruit_id
    ).first()
    if not group:
        raise HTTPException(404, "组不存在")

    other_assigns = db.query(RecruitmentGroupClassroom).join(
        RecruitmentGroup,
        RecruitmentGroupClassroom.group_id == RecruitmentGroup.id
    ).filter(
        RecruitmentGroup.recruitment_id == recruit_id,
        RecruitmentGroupClassroom.group_id != group_id,
        RecruitmentGroupClassroom.recruitment_classroom_id.in_(rc_ids)
    ).all()
    if other_assigns:
        taken_ids = [a.recruitment_classroom_id for a in other_assigns]
        raise HTTPException(400, f"以下教室已被其他组占用：{taken_ids}")

    db.query(RecruitmentGroupClassroom).filter(
        RecruitmentGroupClassroom.group_id == group_id
    ).delete()
    db.flush()

    for rcid in rc_ids:
        rc = db.query(RecruitmentClassroom).filter(
            RecruitmentClassroom.id == rcid,
            RecruitmentClassroom.recruitment_id == recruit_id
        ).first()
        if rc:
            db.add(RecruitmentGroupClassroom(group_id=group_id, recruitment_classroom_id=rcid))

    db.commit()
    return {"code": 0, "msg": "教室已分配"}


def _check_not_finalized(recruit_id: int, db: Session):
    """如果已经 finalize 过，抛出 400 错误"""
    count = db.query(TaskProgress).join(
        RecruitmentClassroom, TaskProgress.recruitment_classroom_id == RecruitmentClassroom.id
    ).filter(
        RecruitmentClassroom.recruitment_id == recruit_id
    ).count()
    if count > 0:
        raise HTTPException(400, "分组已确认并生成了任务清单，不可再修改分组")


@app.post("/api/recruit/{recruit_id}/finalize-grouping")
async def finalize_grouping(request: Request, recruit_id: int, db: Session = Depends(get_db)):
    """最终确认分组：初始化任务进度和验收记录"""
    check_admin_login(request)
    check_csrf(request)
    _check_not_finalized(recruit_id, db)

    groups = db.query(RecruitmentGroup).filter(
        RecruitmentGroup.recruitment_id == recruit_id
    ).count()
    if groups == 0:
        raise HTTPException(400, "还没有任何分组，请先创建分组")

    all_groups = db.query(RecruitmentGroup).filter(
        RecruitmentGroup.recruitment_id == recruit_id
    ).order_by(RecruitmentGroup.id).all()
    for idx, g in enumerate(all_groups, 1):
        cnt = db.query(RecruitmentGroupMember).filter(
            RecruitmentGroupMember.group_id == g.id
        ).count()
        if cnt == 0:
            raise HTTPException(400, f"第{idx}组没有成员，请先分配人员")

        room_cnt = db.query(RecruitmentGroupClassroom).filter(
            RecruitmentGroupClassroom.group_id == g.id
        ).count()
        if room_cnt == 0:
            raise HTTPException(400, f"第{idx}组没有分配教室，请先在「分配教室」中分配")

    total_classrooms = db.query(func.count(RecruitmentClassroom.id)).filter(
        RecruitmentClassroom.recruitment_id == recruit_id
    ).scalar()
    assigned_classrooms = db.query(func.count(RecruitmentGroupClassroom.id)).join(
        RecruitmentGroup, RecruitmentGroupClassroom.group_id == RecruitmentGroup.id
    ).filter(
        RecruitmentGroup.recruitment_id == recruit_id
    ).scalar()
    if assigned_classrooms < total_classrooms:
        raise HTTPException(400, f"还有 {total_classrooms - assigned_classrooms} 间教室未分配，请将所有教室分配到对应组后再确认")

    init_task_progress(recruit_id, db)
    init_acceptance_records(recruit_id, db)
    return {"code": 0, "msg": "分组已确认，任务清单和验收记录已创建"}


@app.get("/api/recruit/{recruit_id}/groups")
async def get_groups(request: Request, recruit_id: int, db: Session = Depends(get_db)):
    """获取某个招募的所有分组"""
    check_admin_login(request)
    groups = db.query(RecruitmentGroup).filter(
        RecruitmentGroup.recruitment_id == recruit_id
    ).order_by(RecruitmentGroup.id).all()

    result = []
    for g in groups:
        members = db.query(Registration).join(
            RecruitmentGroupMember,
            RecruitmentGroupMember.registration_id == Registration.id
        ).filter(RecruitmentGroupMember.group_id == g.id).all()

        assigns = db.query(RecruitmentGroupClassroom).filter(
            RecruitmentGroupClassroom.group_id == g.id
        ).all()

        classrooms_info = []
        for ca in assigns:
            rc = db.query(RecruitmentClassroom).filter(RecruitmentClassroom.id == ca.recruitment_classroom_id).first()
            if rc:
                cr = db.query(Classroom).filter(Classroom.id == rc.classroom_id).first()
                classrooms_info.append({
                    "rc_id": rc.id,
                    "classroom_name": cr.name if cr else "未知",
                    "exam_mode": rc.exam_mode,
                    "exam_number_start": rc.exam_number_start,
                })

        result.append({
            "id": g.id,
            "zone_name": g.zone_name or "无分区",
            "is_supervisor": g.is_supervisor,
            "members": [{
                "id": m.id,
                "name": m.name,
                "student_id": m.student_id,
                "gender": m.gender,
                "has_experience": m.has_experience,
            } for m in members],
            "classrooms": classrooms_info,
        })

    return {"code": 0, "data": result}


@app.post("/api/recruit/{recruit_id}/init-tasks")
async def init_tasks(request: Request, recruit_id: int, db: Session = Depends(get_db)):
    """初始化布置清单任务（管理员手动触发）"""
    check_admin_login(request)
    check_csrf(request)
    init_task_progress(recruit_id, db)
    return {"code": 0, "msg": "任务初始化成功"}


@app.post("/api/my-tasks")
async def get_my_tasks(
    student_id: str = Form(...),
    phone: str = Form(...),
    db: Session = Depends(get_db)
):
    """学生查看自己的布置任务"""
    if not (student_id.isdigit() and len(student_id) == 8):
        raise HTTPException(400, "学号格式错误")
    if not (phone.isdigit() and len(phone) == 11):
        raise HTTPException(400, "手机号格式错误")

    reg = db.query(Registration).filter(
        Registration.student_id == student_id,
        Registration.phone == phone
    ).first()
    if not reg:
        raise HTTPException(404, "未找到报名记录")

    member = db.query(RecruitmentGroupMember).join(
        RecruitmentGroup,
        RecruitmentGroupMember.group_id == RecruitmentGroup.id
    ).filter(
        RecruitmentGroupMember.registration_id == reg.id,
        RecruitmentGroup.recruitment_id == reg.recruitment_id
    ).first()

    if not member:
        return {"code": 0, "data": [], "msg": "暂无分组任务"}

    assignments = db.query(RecruitmentGroupClassroom).filter(
        RecruitmentGroupClassroom.group_id == member.group_id
    ).all()

    group = db.query(RecruitmentGroup).filter(RecruitmentGroup.id == member.group_id).first()

    result = []
    for assign in assignments:
        rc = db.query(RecruitmentClassroom).filter(RecruitmentClassroom.id == assign.recruitment_classroom_id).first()
        if not rc:
            continue
        cr = db.query(Classroom).filter(Classroom.id == rc.classroom_id).first()
        if not cr:
            continue

        tasks = db.query(TaskProgress).filter(
            TaskProgress.recruitment_classroom_id == rc.id,
            TaskProgress.task_type == "setup"
        ).order_by(TaskProgress.id).all()

        task_list = [{
            "id": t.id,
            "item_key": t.item_key,
            "item_name": t.item_name,
            "is_completed": t.is_completed,
            "is_auto_skip": t.is_auto_skip,
        } for t in tasks]

        completed = sum(1 for t in tasks if t.is_completed)
        total = sum(1 for t in tasks if not t.is_auto_skip)

        result.append({
            "rc_id": rc.id,
            "classroom_name": cr.name,
            "exam_numbers": [rc.exam_number_start, rc.exam_number_start + 1] if rc.exam_mode == "double" else [rc.exam_number_start],
            "tasks": task_list,
            "progress": f"{completed}/{total}",
            "all_done": completed >= total,
        })

    return {"code": 0, "data": result, "zone_name": group.zone_name if group else ""}


@app.post("/api/tasks/{task_id}/toggle")
async def toggle_task(task_id: int, db: Session = Depends(get_db)):
    """切换任务项的完成状态"""
    task = db.query(TaskProgress).filter(TaskProgress.id == task_id).first()
    if not task:
        raise HTTPException(404, "任务不存在")
    if task.is_auto_skip:
        raise HTTPException(400, "自动跳过的任务不可操作")

    task.is_completed = not task.is_completed
    task.completed_at = now_beijing() if task.is_completed else None
    db.commit()

    return {"code": 0, "msg": "更新成功", "is_completed": task.is_completed}


@app.get("/api/recruit/{recruit_id}/task-progress")
async def get_task_progress(request: Request, recruit_id: int, db: Session = Depends(get_db)):
    """管理员查看所有教室的进度"""
    check_admin_login(request)

    recruit = db.query(Recruitment).filter(Recruitment.id == recruit_id).first()
    if not recruit:
        raise HTTPException(404, "招募不存在")

    groups = db.query(RecruitmentGroup).filter(
        RecruitmentGroup.recruitment_id == recruit_id
    ).all()

    result = []
    for g in groups:
        assigns = db.query(RecruitmentGroupClassroom).filter(
            RecruitmentGroupClassroom.group_id == g.id
        ).all()

        for assign in assigns:
            rc = db.query(RecruitmentClassroom).filter(RecruitmentClassroom.id == assign.recruitment_classroom_id).first()
            if not rc:
                continue
            cr = db.query(Classroom).filter(Classroom.id == rc.classroom_id).first()
            if not cr:
                continue

            tasks = db.query(TaskProgress).filter(
                TaskProgress.recruitment_classroom_id == rc.id,
                TaskProgress.task_type == "setup"
            ).all()

            completed = sum(1 for t in tasks if t.is_completed)
            total = sum(1 for t in tasks if not t.is_auto_skip)

            result.append({
                "classroom_name": cr.name,
                "zone_name": g.zone_name or "无分区",
                "progress": f"{completed}/{total}",
                "percent": int(completed / total * 100) if total > 0 else 0,
                "all_done": completed >= total,
            })

    return {"code": 0, "data": result}


@app.post("/api/recruit/{recruit_id}/classrooms/{rc_id}/submit-review")
async def submit_for_review(
    request: Request,
    recruit_id: int,
    rc_id: int,
    db: Session = Depends(get_db)
):
    """小组提交验收（需学号+手机验证）"""
    body = await request.json()
    student_id = body.get("student_id", "")
    phone = body.get("phone", "")

    if not (student_id.isdigit() and len(student_id) == 8):
        raise HTTPException(400, "学号格式错误")
    if not (phone.isdigit() and len(phone) == 11):
        raise HTTPException(400, "手机号格式错误")

    reg = db.query(Registration).filter(
        Registration.student_id == student_id,
        Registration.phone == phone,
        Registration.recruitment_id == recruit_id
    ).first()
    if not reg:
        raise HTTPException(404, "未找到报名记录")

    rc = db.query(RecruitmentClassroom).filter(RecruitmentClassroom.id == rc_id).first()
    if not rc:
        raise HTTPException(404, "考场不存在")

    assign = db.query(RecruitmentGroupClassroom).join(
        RecruitmentGroup, RecruitmentGroupClassroom.group_id == RecruitmentGroup.id
    ).filter(
        RecruitmentGroupClassroom.recruitment_classroom_id == rc_id,
        RecruitmentGroup.recruitment_id == recruit_id
    ).first()
    if not assign:
        raise HTTPException(403, "你没有此考场的权限")

    member = db.query(RecruitmentGroupMember).filter(
        RecruitmentGroupMember.group_id == assign.group_id,
        RecruitmentGroupMember.registration_id == reg.id
    ).first()
    if not member:
        raise HTTPException(403, "你不是该考场所在组的成员")

    tasks = db.query(TaskProgress).filter(
        TaskProgress.recruitment_classroom_id == rc_id,
        TaskProgress.task_type == "setup"
    ).all()
    completed = sum(1 for t in tasks if t.is_completed)
    total = sum(1 for t in tasks if not t.is_auto_skip)
    if completed < total:
        raise HTTPException(400, f"还有 {total - completed} 项任务未完成")

    record = db.query(AcceptanceRecord).filter(
        AcceptanceRecord.recruitment_classroom_id == rc_id
    ).first()
    if not record:
        record = AcceptanceRecord(recruitment_classroom_id=rc_id)
        db.add(record)

    if record.status == "passed":
        raise HTTPException(400, "该考场已验收通过")
    if record.status == "sealed":
        raise HTTPException(400, "该考场已封门，不可操作")

    record.status = "submitted"
    record.note = None
    db.commit()
    return {"code": 0, "msg": "已提交验收"}


@app.get("/api/recruit/{recruit_id}/acceptance/supervisor")
async def supervisor_acceptance_panel(
    request: Request,
    recruit_id: int,
    student_id: str = "",
    phone: str = "",
    db: Session = Depends(get_db)
):
    """楼栋负责人查看自己楼栋的验收面板"""
    if not (student_id.isdigit() and len(student_id) == 8):
        raise HTTPException(400, "学号格式错误")
    if not (phone.isdigit() and len(phone) == 11):
        raise HTTPException(400, "手机号格式错误")

    reg = db.query(Registration).filter(
        Registration.student_id == student_id,
        Registration.phone == phone,
        Registration.recruitment_id == recruit_id
    ).first()
    if not reg:
        raise HTTPException(404, "未找到报名记录")

    bs = db.query(BuildingSupervisor).filter(
        BuildingSupervisor.recruitment_id == recruit_id,
        BuildingSupervisor.registration_id == reg.id
    ).first()
    if not bs:
        raise HTTPException(403, "你不是楼栋负责人")

    zone_name = bs.zone_name
    zone_groups = db.query(RecruitmentGroup).filter(
        RecruitmentGroup.recruitment_id == recruit_id,
        RecruitmentGroup.zone_name == zone_name
    ).all()

    zone_group_ids = [g.id for g in zone_groups]
    assigns = db.query(RecruitmentGroupClassroom).filter(
        RecruitmentGroupClassroom.group_id.in_(zone_group_ids)
    ).all()

    result = []
    for assign in assigns:
        rc = db.query(RecruitmentClassroom).filter(RecruitmentClassroom.id == assign.recruitment_classroom_id).first()
        if not rc:
            continue
        cr = db.query(Classroom).filter(Classroom.id == rc.classroom_id).first()
        if not cr:
            continue

        record = db.query(AcceptanceRecord).filter(
            AcceptanceRecord.recruitment_classroom_id == rc.id
        ).first()

        members = db.query(Registration).join(
            RecruitmentGroupMember,
            RecruitmentGroupMember.registration_id == Registration.id
        ).filter(RecruitmentGroupMember.group_id == assign.group_id).all()

        result.append({
            "rc_id": rc.id,
            "classroom_name": cr.name,
            "group_members": [{"name": m.name, "gender": m.gender} for m in members],
            "status": record.status if record else "pending",
            "note": record.note if record else "",
        })

    return {"code": 0, "data": result, "zone_name": zone_name}


@app.post("/api/acceptance/{rc_id}/review")
async def review_classroom(
    request: Request,
    rc_id: int,
    db: Session = Depends(get_db)
):
    """楼栋负责人/管理员验收或驳回"""
    body = await request.json()
    action = body.get("action", "")
    note = body.get("note", "")

    if action not in ("pass", "reject"):
        raise HTTPException(400, "操作无效")
    if action == "reject" and not note.strip():
        raise HTTPException(400, "驳回必须填写原因")

    record = db.query(AcceptanceRecord).filter(
        AcceptanceRecord.recruitment_classroom_id == rc_id
    ).first()
    if not record:
        raise HTTPException(404, "验收记录不存在")
    if record.status == "sealed":
        raise HTTPException(400, "已封门，不可操作")

    record.status = "rejected" if action == "reject" else "passed"
    record.note = note.strip() if note.strip() else None
    db.commit()
    return {"code": 0, "msg": "操作成功"}


@app.post("/api/recruit/{recruit_id}/acceptance/seal")
async def seal_all(request: Request, recruit_id: int, db: Session = Depends(get_db)):
    """总负责人确认封门"""
    check_admin_login(request)
    check_csrf(request)

    records = db.query(AcceptanceRecord).join(
        RecruitmentClassroom,
        AcceptanceRecord.recruitment_classroom_id == RecruitmentClassroom.id
    ).filter(
        RecruitmentClassroom.recruitment_id == recruit_id
    ).all()

    not_passed = [r for r in records if r.status != "passed" and r.status != "sealed"]
    if not_passed:
        raise HTTPException(400, f"还有 {len(not_passed)} 间教室未通过验收")

    for r in records:
        r.status = "sealed"

    db.commit()
    return {"code": 0, "msg": "全部封门完成"}


@app.get("/api/recruit/{recruit_id}/acceptance/overview")
async def acceptance_overview(request: Request, recruit_id: int, db: Session = Depends(get_db)):
    """管理员查看验收总览"""
    check_admin_login(request)

    groups = db.query(RecruitmentGroup).filter(
        RecruitmentGroup.recruitment_id == recruit_id
    ).all()

    result = []
    for g in groups:
        assigns = db.query(RecruitmentGroupClassroom).filter(
            RecruitmentGroupClassroom.group_id == g.id
        ).all()

        for assign in assigns:
            rc = db.query(RecruitmentClassroom).filter(RecruitmentClassroom.id == assign.recruitment_classroom_id).first()
            if not rc:
                continue
            cr = db.query(Classroom).filter(Classroom.id == rc.classroom_id).first()
            if not cr:
                continue

            record = db.query(AcceptanceRecord).filter(
                AcceptanceRecord.recruitment_classroom_id == rc.id
            ).first()

            members = db.query(Registration).join(
                RecruitmentGroupMember,
                RecruitmentGroupMember.registration_id == Registration.id
            ).filter(RecruitmentGroupMember.group_id == g.id).all()

            result.append({
                "classroom_name": cr.name,
                "zone_name": g.zone_name or "无分区",
                "members": [m.name for m in members],
                "status": record.status if record else "pending",
                "note": record.note if record else "",
            })

    return {"code": 0, "data": result}


@app.post("/api/recruit/{recruit_id}/init-recovery")
async def init_recovery(request: Request, recruit_id: int, db: Session = Depends(get_db)):
    """开启恢复阶段"""
    check_admin_login(request)
    check_csrf(request)

    recruit = db.query(Recruitment).filter(Recruitment.id == recruit_id).first()
    if not recruit:
        raise HTTPException(404, "招募不存在")

    records = db.query(AcceptanceRecord).join(
        RecruitmentClassroom,
        AcceptanceRecord.recruitment_classroom_id == RecruitmentClassroom.id
    ).filter(RecruitmentClassroom.recruitment_id == recruit_id).all()

    not_sealed = [r for r in records if r.status != "sealed"]
    if not_sealed:
        raise HTTPException(400, f"还有 {len(not_sealed)} 间教室未封门，不能开启恢复")

    init_recovery_tasks(recruit_id, db)
    return {"code": 0, "msg": "恢复任务已创建"}


@app.post("/api/my-recovery-tasks")
async def get_my_recovery_tasks(
    student_id: str = Form(...),
    phone: str = Form(...),
    db: Session = Depends(get_db)
):
    """学生查看自己的恢复任务"""
    if not (student_id.isdigit() and len(student_id) == 8):
        raise HTTPException(400, "学号格式错误")
    if not (phone.isdigit() and len(phone) == 11):
        raise HTTPException(400, "手机号格式错误")

    reg = db.query(Registration).filter(
        Registration.student_id == student_id,
        Registration.phone == phone
    ).first()
    if not reg:
        raise HTTPException(404, "未找到报名记录")

    member = db.query(RecruitmentGroupMember).join(
        RecruitmentGroup,
        RecruitmentGroupMember.group_id == RecruitmentGroup.id
    ).filter(
        RecruitmentGroupMember.registration_id == reg.id,
        RecruitmentGroup.recruitment_id == reg.recruitment_id
    ).first()

    if not member:
        return {"code": 0, "data": []}

    assignments = db.query(RecruitmentGroupClassroom).filter(
        RecruitmentGroupClassroom.group_id == member.group_id
    ).all()

    result = []
    for assign in assignments:
        rc = db.query(RecruitmentClassroom).filter(RecruitmentClassroom.id == assign.recruitment_classroom_id).first()
        if not rc:
            continue
        cr = db.query(Classroom).filter(Classroom.id == rc.classroom_id).first()
        if not cr:
            continue

        tasks = db.query(TaskProgress).filter(
            TaskProgress.recruitment_classroom_id == rc.id,
            TaskProgress.task_type == "recovery"
        ).order_by(TaskProgress.id).all()

        if not tasks:
            continue

        task_list = [{
            "id": t.id,
            "item_key": t.item_key,
            "item_name": t.item_name,
            "is_completed": t.is_completed,
        } for t in tasks]

        completed = sum(1 for t in tasks if t.is_completed)
        total = len(tasks)

        result.append({
            "rc_id": rc.id,
            "classroom_name": cr.name,
            "tasks": task_list,
            "progress": f"{completed}/{total}",
            "all_done": completed >= total,
        })

    return {"code": 0, "data": result}


@app.get("/api/recruit/{recruit_id}/recovery-progress")
async def get_recovery_progress(request: Request, recruit_id: int, db: Session = Depends(get_db)):
    """管理员查看恢复进度"""
    check_admin_login(request)

    groups = db.query(RecruitmentGroup).filter(
        RecruitmentGroup.recruitment_id == recruit_id
    ).all()

    result = []
    for g in groups:
        assigns = db.query(RecruitmentGroupClassroom).filter(
            RecruitmentGroupClassroom.group_id == g.id
        ).all()

        for assign in assigns:
            rc = db.query(RecruitmentClassroom).filter(RecruitmentClassroom.id == assign.recruitment_classroom_id).first()
            if not rc:
                continue
            cr = db.query(Classroom).filter(Classroom.id == rc.classroom_id).first()
            if not cr:
                continue

            tasks = db.query(TaskProgress).filter(
                TaskProgress.recruitment_classroom_id == rc.id,
                TaskProgress.task_type == "recovery"
            ).all()

            completed = sum(1 for t in tasks if t.is_completed)
            total = len(tasks)

            result.append({
                "classroom_name": cr.name,
                "zone_name": g.zone_name or "无分区",
                "progress": f"{completed}/{total}",
                "percent": int(completed / total * 100) if total > 0 else 0,
                "all_done": completed >= total,
            })

    return {"code": 0, "data": result}
