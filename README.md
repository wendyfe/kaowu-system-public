关于这个项目的介绍如下：
1、谁写的？一个高校搞考试管理的牛马，整个项目早期由豆包、grok、GPT三位大师指导，后期学会了Claude code后，调用DSv4，已经实现了半自动化编程。
2、能干嘛？解决日常工作的一个痛点，考试考场布置、手机集中存放这些工作前都要向全校招募学生助理，通过这个系统能够快速发布招募信息，学生在获知招募信息后能够快速完成报名，名额已满，自动结束招募。截止时间前，可取消报名，为防止恶意被人取消，设置邮箱验证功能。管理端可修改招募信息、以Excel表格导出招募结果，方便下一步管理。

---

## 项目结构

```
app/
  main.py              # 后端：FastAPI 应用、数据库模型、路由、全部逻辑
  tool_processors.py   # 管理端工具处理逻辑：监考分配、桌贴、Excel 合并等
  static/
    admin.html          # 管理后台：招募管理、教室管理、工具页
    admin_login.html    # 管理员登录页
    student.html        # 学生端：报名、查询、取消报名
    training.html       # 学生培训视频页：进度记录、节点题目、完成状态
    style.css           # 全局共享样式
  templates/
    index.html          # 首页（两个入口：管理后台、学生报名）
docker-compose.yml      # 生产部署
Dockerfile              # 容器构建
```

## 技术栈

| 层 | 技术 |
|---|---|
| 后端框架 | FastAPI |
| 数据库 | SQLite + SQLAlchemy |
| 前端 | Bootstrap 5.3 + 静态 HTML |
| 日期选择器 | Flatpickr |
| 文件处理 | pandas、openpyxl、xlrd、reportlab、dbfread |
| 部署 | Docker + docker-compose |

## 功能清单

- **招募管理**：发布招募、设置人数和截止时间、配置 QQ 加群链接、编辑/关闭/删除招募、查看报名名单、导出报名 Excel。
- **学生报名**：学生查看当前可报名项目，填写基础信息、性别、经验、联系方式后报名；支持查询个人报名记录和邮箱验证码取消报名。
- **教室与考场管理**：维护教学楼和教室基础数据，批量导入教室，设置固定桌椅、双考场能力和启用状态；发布招募时选择考场并自动生成考场号。
- **人员分组与场地分配**：支持总负责人、楼栋负责人、自动分组、手动拖拽分组、按组分配教室等流程。
- **布置、验收与恢复**：按考场生成布置任务，支持学生提交布置进度、管理员验收、封门、开启考后恢复和查看恢复进度。
- **培训视频与题目**：学生通过报名信息进入培训页，系统记录观看进度；管理员可维护培训题目并查看完成情况。
- **管理端工具页**：通过 PIN 验证后使用日常考务工具，如监考员分配、桌贴生成、数据统计、工作簿处理等。
- **数据统计辅助**：提供部分考试数据入库、查询和汇总分析能力，用于内部工作辅助。
- **安全控制**：管理员 Cookie 会话、CSRF 防护、接口限流、工具页二次 PIN 验证。

## 启动方式

```bash
# 本地开发
cd app && uvicorn main:app --reload --port 8000

# Docker 部署
docker compose up -d --build
```

> 当前 Dockerfile 会把 `app/` 复制进镜像；即使只改 `admin.html` 这类前端静态文件，VPS 上也需要重新构建镜像或额外挂载静态目录。

## 环境变量

| 变量 | 说明 | 默认值 |
|---|---|---|
| `KAOWU_ADMIN_USERNAME` | 管理员账号 | `admin` |
| `KAOWU_ADMIN_PASSWORD` | 管理员密码 | `change_this_immediately` |
| `KAOWU_SECRET_KEY` | 会话密钥 | `kaowu_2026_secret` |
| `DB_DIR` | SQLite 数据库目录 | `app/db` |
| `KAOWU_TOOLS_PIN` | 管理员工具页 PIN 码 | — |
| `KAOWU_TOOLS_UNLOCK_MAX_AGE` | 工具页解锁有效期（秒） | `3600` |
| `SMTP_USER` | 邮箱账号（发验证码） | — |
| `SMTP_PASS` | 邮箱密码/授权码 | — |
| `SMTP_HOST` | SMTP 服务器 | `smtp.gmail.com` |
| `SMTP_PORT` | SMTP 端口 | `465` |
| `TRAINING_VIDEO_PATH` | 培训视频在容器内的文件路径 | `app/protected_videos/training.mp4` |
| `TRAINING_PUBLIC_VIDEO_URL` | 培训页优先使用的公开视频地址，适合交给 Nginx/Cloudflare 缓存 | — |
| `TRAINING_VIDEO_DURATION_SECONDS` | 培训视频总时长（秒），前端读取到视频时也会上报更新 | `0` |
| `TRAINING_COOKIE_MAX_AGE` | 学生培训登录 Cookie 有效期（秒） | `86400` |

> 本地开发会自动读取项目根目录或 `app/` 下的 `.env`，但不会覆盖系统环境变量；Docker 部署通过 `docker-compose.yml` 的 `env_file: .env` 传入。

## 培训视频部署

生产环境建议把培训视频放在宿主机 `/data/training-videos/training.mp4`，不要放进 Git 仓库或 `static/` 目录。`docker-compose.yml` 已将该目录只读挂载到容器内。

`.env` 示例：

```env
TRAINING_VIDEO_PATH=/data/training-videos/training.mp4
TRAINING_PUBLIC_VIDEO_URL=/training-public/training.mp4
```

Nginx 示例：

```nginx
location /training-public/ {
    alias /data/training-videos/;
    add_header Accept-Ranges bytes;
    add_header Cache-Control "public, max-age=86400";
    autoindex off;
}
```

Cloudflare 橙云模式下，可为 `/training-public/*` 单独设置 Cache Rule（Cache Everything、Edge TTL 7 天或 30 天）。培训页和进度接口仍走系统身份验证，公开视频 URL 只用于提升视频加载速度。

## 工具页说明

工具页面向管理端内部使用，进入前需要输入独立 PIN。当前包含：

- **监考员分配**：按模板导入监考员和考场数据，生成分配结果。
- **考场桌贴生成**：根据考场数、座位数等参数生成桌贴 PDF，支持名单预检和模板下载。
- **考试数据辅助工具**：用于部分考试数据的导入、查询、通过率分析和汇总统计，具体口径以系统页面为准。
- **毕业生数据管理**：导入届别数据，为统计分析提供基础数据。
- **工作簿中多表合并**：将一个工作簿内多个工作表合并到一个汇总工作表。

涉及个人信息和成绩数据的工具仅供内部授权人员使用，不建议在公开文档中记录具体数据解析规则或业务口径细节。

## 注意事项

- 数据库文件自动创建于 `app/db/kaowu.db`
- 本地 `.env` 可放在项目根目录或 `app/` 下；代码会读取它，但不会覆盖系统环境变量
- 注册时填写的 QQ 号需对应 QQ 邮箱，用于接收取消报名的验证码
- 管理员发布的 QQ 加群链接需从 QQ 群管理 → 群设置 → 加群设置中生成（`qm.qq.com` 格式）
- 所有时间默认北京时间（`Asia/Shanghai`）
- `.env`、`app/db/`、`.claude/`、`.superpowers/`、`tmp_seat_label_tests/`、私有 VPS 运维手册不要提交到仓库
