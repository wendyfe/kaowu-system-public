from fastapi import FastAPI, Request, Form, HTTPException, Depends, BackgroundTasks, UploadFile, File
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean, func, UniqueConstraint, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from datetime import datetime, timedelta
import os
import pandas as pd
from io import BytesIO
from urllib.parse import quote
from zoneinfo import ZoneInfo
from contextlib import contextmanager
from uuid import uuid4
from itsdangerous import URLSafeTimedSerializer
import smtplib
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr
import random
import string
import time
import json


# ==================== 北京时间配置 ====================
BEIJING_TZ = ZoneInfo("Asia/Shanghai")

def now_beijing():
    return datetime.now(BEIJING_TZ).replace(tzinfo=None)

# ==================== 核心配置 ====================
ADMIN_USERNAME = os.getenv("KAOWU_ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("KAOWU_ADMIN_PASSWORD", "change_this_immediately")
SECRET_KEY = os.getenv("KAOWU_SECRET_KEY", "kaowu_2026_secret")
if SECRET_KEY == "kaowu_2026_secret":
    print("WARNING: Using default SECRET_KEY. Set KAOWU_SECRET_KEY env var for production.")
serializer = URLSafeTimedSerializer(SECRET_KEY)

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


class BuildingSupervisor(Base):
    """楼栋负责人"""
    __tablename__ = "building_supervisors"
    id = Column(Integer, primary_key=True)
    recruitment_id = Column(Integer, nullable=False)
    zone_name = Column(String(20), nullable=False)    # 如 "B栋"
    registration_id = Column(Integer, nullable=False)  # 关联 registration.id


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

def rate_limit(key: str, max_requests: int = 10, window: int = 60):
    now = time.time()
    if key not in _RATE_LIMITS:
        _RATE_LIMITS[key] = []
    _RATE_LIMITS[key] = [t for t in _RATE_LIMITS[key] if now - t < window]
    if len(_RATE_LIMITS[key]) >= max_requests:
        raise HTTPException(429, f"请求过于频繁，请{window}秒后再试")
    _RATE_LIMITS[key].append(now)

# 定期清理过期限流记录
def _cleanup_rate_limits():
    now = time.time()
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

    recruit = Recruitment(exam_name=exam_name.strip(), need_num=need_num, end_time=end_time, qq_group=qq_group.strip() if qq_group and qq_group.strip() else None, has_floor_supervisors=has_floor_supervisors)
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
    recruit.qq_group = qq_group.strip() if qq_group and qq_group.strip() else None
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
    with db_lock(db):
        # 锁内重新查询人数
        current_count = db.query(func.count(Registration.id)).filter(Registration.recruitment_id == recruitment_id).scalar()
        if current_count >= recruit.need_num:
            recruit.is_active = False
            is_full = True
        else:
            # 防重复报名（同一考试 + 同一学号）
            exists = db.query(Registration).filter(
                Registration.recruitment_id == recruitment_id,
                Registration.student_id == student_id
            ).first()
            if exists:
                raise HTTPException(400, "此学号已报名过该考试")

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
            # 添加后检查是否已满，满则关闭招募
            if current_count + 1 >= recruit.need_num:
                recruit.is_active = False

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

    return {
        "recruit_id": recruit_id,
        "general_supervisor_id": general,
        "supervisors": supervisors,
        "registrations": reg_list,
        "groups": groups_data,
        "classrooms": classrooms_info,
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
        random.shuffle(buckets[key])

    pairs = []

    # Phase 1: 同性别跨经验
    for g in ("male", "female"):
        exp_list = buckets[f"exp_{g}"]
        new_list = buckets[f"new_{g}"]
        while exp_list and new_list:
            pairs.append([exp_list.pop(), new_list.pop()])

    # Phase 2: 同经验跨性别
    for e in ("exp", "new"):
        male_list = buckets[f"{e}_male"]
        female_list = buckets[f"{e}_female"]
        while male_list and female_list:
            pairs.append([male_list.pop(), female_list.pop()])

    # Phase 3: 剩余混合
    remaining = []
    for key in buckets:
        remaining.extend(buckets[key])
    random.shuffle(remaining)
    while len(remaining) >= 2:
        pairs.append([remaining.pop(), remaining.pop()])

    # Phase 4: 落单
    if remaining:
        pairs.append([remaining.pop()])

    return pairs


@app.post("/api/recruit/{recruit_id}/auto-group")
async def auto_group(request: Request, recruit_id: int, db: Session = Depends(get_db)):
    """自动分组：排除总负责人和楼栋负责人后，运行配对算法"""
    check_admin_login(request)
    check_csrf(request)

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


@app.post("/api/recruit/{recruit_id}/finalize-grouping")
async def finalize_grouping(request: Request, recruit_id: int, db: Session = Depends(get_db)):
    """最终确认分组：初始化任务进度和验收记录"""
    check_admin_login(request)
    check_csrf(request)

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

    record.status = "pending"
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