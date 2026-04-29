from fastapi import FastAPI, Request, Form, HTTPException, Depends, BackgroundTasks
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean, func
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

# 考场布置视频 URL（B站嵌入链接，如 //player.bilibili.com/player.html?bvid=xxx）
VIDEO_URL = os.getenv("KAOWU_VIDEO_URL")

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
    qq_group = Column(String(20), nullable=True)  # 考务QQ群号，纯数字，可空
    end_time = Column(DateTime, nullable=True)   # 北京时间

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

# 新增验证码记录表（可选，替代内存存储）
class VerifyCode(Base):
    __tablename__ = "verify_code"
    id = Column(Integer, primary_key=True)
    reg_id = Column(Integer, nullable=False)
    code = Column(String(6), nullable=False)
    email = Column(String(100), nullable=False)
    create_time = Column(DateTime, default=now_beijing)
    is_used = Column(Boolean, default=False)

# 考场布置视频观看记录（按学生追踪）
class VideoWatch(Base):
    __tablename__ = "video_watch"
    id = Column(Integer, primary_key=True)
    student_id = Column(String(20), unique=True, nullable=False)
    name = Column(String(50), nullable=False)
    watched_at = Column(DateTime, default=now_beijing)

Base.metadata.create_all(bind=engine)

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

    recruit = Recruitment(exam_name=exam_name.strip(), need_num=need_num, end_time=end_time, qq_group=qq_group.strip() if qq_group and qq_group.strip() else None)
    db.add(recruit)
    db.commit()
    db.refresh(recruit)
    return {"code": 0, "msg": "发布成功"}

# 编辑招募（不变）
@app.put("/api/recruit/{recruit_id}")
async def edit_recruit(
    request: Request,
    recruit_id: int,
    exam_name: str = Form(...),
    need_num: int = Form(...),
    end_time_str: str = Form(None),
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
    # 先删关联的报名记录
    db.query(Registration).filter(Registration.recruitment_id == recruit_id).delete()
    # 再删招募
    db.query(Recruitment).filter(Recruitment.id == recruit_id).delete()
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
        result.append({
            "id": r.id,
            "exam_name": r.exam_name,
            "need_num": r.need_num,
            "registered": count_val,
            "end_time": r.end_time.strftime("%Y-%m-%d %H:%M") if r.end_time else None,
            "status": status
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
    # 批量查询观看状态
    student_ids = list({r.student_id for r in regs})
    watched_students = set()
    if student_ids:
        watched = db.query(VideoWatch.student_id).filter(VideoWatch.student_id.in_(student_ids)).all()
        watched_students = {w[0] for w in watched}
    return [{
        "id": r.id,
        "student_id": r.student_id,
        "name": r.name,
        "phone": r.phone,
        "qq": r.qq,
        "has_experience": r.has_experience,
        "ip_address": r.ip_address,
        "video_watched": r.student_id in watched_students,
        "create_time": r.create_time.strftime("%Y-%m-%d %H:%M") if r.create_time else None
    } for r in regs]

# 获取视频 URL
@app.get("/api/video-url")
async def get_video_url():
    return {"video_url": VIDEO_URL}

# 确认已完成观看视频
@app.post("/api/mark-video-watched")
async def mark_video_watched(
    student_id: str = Form(...),
    name: str = Form(...),
    phone: str = Form(...),
    db: Session = Depends(get_db)
):
    reg = db.query(Registration).filter(
        Registration.student_id == student_id,
        Registration.phone == phone
    ).first()
    if not reg:
        raise HTTPException(400, "学号或手机号不匹配，请确认报名信息")

    existing = db.query(VideoWatch).filter(VideoWatch.student_id == student_id).first()
    if existing:
        return {"code": 0, "msg": "已完成观看", "watched_at": existing.watched_at.strftime("%Y-%m-%d %H:%M")}

    watch = VideoWatch(student_id=student_id, name=name)
    db.add(watch)
    db.commit()
    return {"code": 0, "msg": "确认成功"}

# 查询视频观看状态
@app.post("/api/video-watch-status")
async def video_watch_status(
    student_id: str = Form(...),
    phone: str = Form(...),
    db: Session = Depends(get_db)
):
    reg = db.query(Registration).filter(
        Registration.student_id == student_id,
        Registration.phone == phone
    ).first()
    if not reg:
        raise HTTPException(400, "学号或手机号不匹配")

    watch = db.query(VideoWatch).filter(VideoWatch.student_id == student_id).first()
    return {
        "code": 0,
        "watched": watch is not None,
        "watched_at": watch.watched_at.strftime("%Y-%m-%d %H:%M") if watch else None
    }

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
                ip_address=ip
            )
            db.add(reg)

    if is_full:
        raise HTTPException(400, "报名人数已满")

    return {"code": 0, "msg": "报名成功", "qq_group": recruit.qq_group, "video_url": VIDEO_URL}

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