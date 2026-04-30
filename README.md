关于这个项目的介绍如下：
1、谁写的？一个高校搞考试管理的牛马，整个项目早期由豆包、grok、GPT三位大师指导，后期学会了Claude code后，调用DSv4，已经实现了半自动化编程。
2、能干嘛？解决日常工作的一个痛点，考试考场布置、手机集中存放这些工作前都要向全校招募学生助理，通过这个系统能够快速发布招募信息，学生在获知招募信息后能够快速完成报名，名额已满，自动结束招募。截止时间前，可取消报名，为防止恶意被人取消，设置邮箱验证功能。管理端可修改招募信息、以Excel表格导出招募结果，方便下一步管理。

---

## 项目结构

```
app/
  main.py              # 后端：FastAPI 应用、数据库模型、路由、全部逻辑
  static/
    admin.html          # 管理后台：发布/编辑/删除/开关招募、导出 Excel
    admin_login.html    # 管理员登录页
    student.html        # 学生端：报名、查询、取消报名
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
| 部署 | Docker + docker-compose |

## 功能清单

- **管理员**：发布招募（考试名称、人数、截止时间、QQ加群链接）、编辑、开启/关闭、删除、查看报名名单、导出 Excel
- **学生**：查看可选招募项目、报名、查询我的报名、取消报名（QQ邮箱验证码）
- **安全**：管理员 Cookie 会话、CSRF 防护、接口限流

## 启动方式

```bash
# 本地开发
cd app && uvicorn main:app --reload --port 8000

# Docker 部署
docker compose up -d --build
```

## 环境变量

| 变量 | 说明 | 默认值 |
|---|---|---|
| `KAOWU_ADMIN_USERNAME` | 管理员账号 | `admin` |
| `KAOWU_ADMIN_PASSWORD` | 管理员密码 | `change_this_immediately` |
| `KAOWU_SECRET_KEY` | 会话密钥 | `kaowu_2026_secret` |
| `SMTP_USER` | 邮箱账号（发验证码） | — |
| `SMTP_PASS` | 邮箱密码/授权码 | — |
| `SMTP_HOST` | SMTP 服务器 | `smtp.gmail.com` |
| `SMTP_PORT` | SMTP 端口 | `465` |

> `.env` 文件不会被自动加载，需通过 docker-compose 或系统环境变量传入。

## 注意事项

- 数据库文件自动创建于 `app/db/kaowu.db`
- 注册时填写的 QQ 号需对应 QQ 邮箱，用于接收取消报名的验证码
- 管理员发布的 QQ 加群链接需从 QQ 群管理 → 群设置 → 加群设置中生成（`qm.qq.com` 格式）
- 所有时间默认北京时间（`Asia/Shanghai`）
