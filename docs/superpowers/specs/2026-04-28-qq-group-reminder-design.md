---
title: QQ群号提醒功能设计
date: 2026-04-28
status: draft
---

# QQ 群号提醒功能设计

## 概述

管理员发布招募时可填写 QQ 群号（可选），学生报名成功后看到入群提醒，报名记录中也显示群号，方便学生加群接收后续通知。

## 数据模型

### Recruitment 表新增字段

```python
qq_group = Column(String(20), nullable=True)  # 考务QQ群号，纯数字，可空
```

已有招募的 `qq_group` 为 NULL，学生端不会展示。不提供编辑功能，发布时一旦确定不可修改。

## 后端 API 改动

### 1. 发布招募 `POST /api/recruit/add`

- 新增可选参数 `qq_group: str = Form(None)`
- 存入 `recruit.qq_group`（如果传了值则 strip 后存储，空字符串视为 None）

### 2. 学生报名 `POST /api/reg`

- 成功返回中增加 `qq_group` 字段（从对应 Recruitment 取出）
- 前端据此在报名成功后直接弹窗显示群号，无需二次查询

### 3. 查询报名记录 `POST /api/my-registrations`

- 返回数据中每条记录新增 `qq_group` 字段（从关联的 Recruitment 取出）
- 供前端报名记录列表展示群号

### 不变更的 API

- 编辑招募 `PUT /api/recruit/{id}` — 不增加群号修改能力
- 管理员列表 `GET /api/recruit/admin-list` — 不需要显示群号
- 导出 Excel `GET /api/export/{id}` — 先不加入群号导出

## 前端改动

### 管理员发布表单（admin.html）

- 在发布表单最后新增 QQ 群号输入框（Bootstrap 栅格布局，col-md-4）
- 输入框提示：选填，报名成功后展示给学生
- 提交前校验：如果 `qq_group` 为空，弹确认框「你没有填写QQ群号，已报名的学生将无法加入考务群接收通知，确定继续发布？」
- 用户确认后才提交

### 学生报名成功弹窗（student.html）

- `POST /api/reg` 返回成功时，判断 `qq_group` 是否有值
- 有值：弹出自定义模态框，显示：
  - 报名成功状态图标
  - 考试名称
  - 「📢 请加入考务工作QQ群」
  - 大号加粗的群号
  - 「后续通知将通过群消息发布」
  - 关闭按钮

### 学生报名记录显示群号（student.html）

- 查询报名记录后，每条记录的渲染中，判断 `item.qq_group` 是否有值
- 有值则追加一行「📢 请加考务群：{群号}」（橙色/暖色文字）
- 无值则不显示

## 未涉及的范围

- 不增加群号修改功能（管理员无法事后编辑）
- 不增加群号导出到 Excel
- 不增加管理员列表显示群号
- 不增加二维码或群链接支持
