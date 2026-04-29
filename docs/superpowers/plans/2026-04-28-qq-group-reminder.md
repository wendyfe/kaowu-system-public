# QQ 群号提醒功能 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 管理员发布招募时可选填 QQ 群号，学生报名成功后看到弹窗提示加群，报名记录中也能看到群号。

**Architecture:** 在 Recruitment 模型新增 `qq_group` 字段，后端 3 个 API 接口对应调整，前端 admin.html 发布表单加群号输入和空值确认，student.html 加成功弹窗和记录列表群号展示。

**Tech Stack:** FastAPI + SQLAlchemy (SQLite), Bootstrap 5, vanilla JS

---

### Task 1: 数据模型加字段

**Files:**
- Modify: `app/main.py:78-85`

- [ ] **Step 1: 在 Recruitment 模型添加 qq_group 字段**

在 `app/main.py` 第 78-85 行的 Recruitment 类中，在 `is_active` 字段后、`end_time` 字段前，添加 `qq_group` 字段：

```python
class Recruitment(Base):
    __tablename__ = "recruitment"
    id = Column(Integer, primary_key=True)
    exam_name = Column(String(100), nullable=False)
    need_num = Column(Integer, nullable=False)
    create_time = Column(DateTime, default=now_beijing)
    is_active = Column(Boolean, default=True)
    qq_group = Column(String(20), nullable=True)  # 考务QQ群号，纯数字，可空
    end_time = Column(DateTime, nullable=True)   # 北京时间
```

- [ ] **Step 2: 验证数据库自动迁移**

SQLAlchemy 的 `Base.metadata.create_all(bind=engine)` 会自动为已有表添加新列（SQLite 支持 ADD COLUMN）。重启服务即可。

```bash
cd /Users/wff/KAIfa/kaowu-system/app && uvicorn main:app --reload --port 8000
```

确认服务启动无报错即可（可用 Ctrl+C 停止，后续任务不需要一直开着服务）。

---

### Task 2: 后端 API 调整

**Files:**
- Modify: `app/main.py:255-287` (add_recruit), `app/main.py:351-374` (get_recruit_list), `app/main.py:406-475` (student_register), `app/main.py:478-522` (my_registrations)

- [ ] **Step 1: 发布招募接口增加 qq_group 参数**

修改 `POST /api/recruit/add`（第 255 行附近），在函数签名增加 `qq_group` 参数，保存到模型：

```python
@app.post("/api/recruit/add")
async def add_recruit(
    request: Request,
    exam_name: str = Form(...),
    need_num: int = Form(...),
    qq_group: str = Form(None),
    end_time_str: str = Form(None),
    db: Session = Depends(get_db)
):
    check_admin_login(request)
    check_csrf(request)
    rate_limit(f"recruit_add_{get_client_ip(request)}", max_requests=10, window=60)
    if need_num < 1:
        raise HTTPException(400, "人数必须≥1")

    end_time = None
    # ... end_time 解析代码不变 ...

    recruit = Recruitment(
        exam_name=exam_name.strip(),
        need_num=need_num,
        end_time=end_time,
        qq_group=qq_group.strip() if qq_group and qq_group.strip() else None
    )
    db.add(recruit)
    db.commit()
    db.refresh(recruit)
    return {"code": 0, "msg": "发布成功"}
```

- [ ] **Step 2: 学生报名成功接口返回 qq_group**

修改 `POST /api/reg`（第 406 行附近），在成功返回中加入 `qq_group`（从 recruit 对象取）：

找到第 473-475 行（目前返回部分），确保返回时包含群号：

```python
    if is_full:
        raise HTTPException(400, "报名人数已满")

    return {"code": 0, "msg": "报名成功", "qq_group": recruit.qq_group}
```

- [ ] **Step 3: 查询报名记录接口返回 qq_group**

修改 `POST /api/my-registrations`（第 478 行附近），在每条记录的 dict 中加入 `qq_group`：

找到 result.append 处（约第 513-521 行），在 dict 中新增 `qq_group` 字段：

```python
            result.append({
                "reg_id": reg.id,
                "recruit_id": recruit.id,
                "exam_name": recruit.exam_name,
                "create_time": reg.create_time.strftime("%Y-%m-%d %H:%M"),
                "status": "已报名",
                "can_cancel": can_cancel,
                "qq": reg.qq,
                "qq_group": recruit.qq_group  # 新增：考务QQ群号
            })
```

---

### Task 3: 管理端发布表单加 QQ 群号输入

**Files:**
- Modify: `app/static/admin.html:180-200`

- [ ] **Step 1: 发布表单增加 QQ 群号输入框**

在 `admin.html` 中 "发布新招募" 表单的现有 row 内，在截止时间旁边或后面增加一列。建议放在截止时间后面，保持布局整齐：

```html
<div class="row g-4">
    <div class="col-md-5">
        <label class="form-label">考试名称</label>
        <input type="text" name="exam_name" class="form-control form-control-lg" required placeholder="如：2026年上半年四六级考试">
    </div>
    <div class="col-md-3">
        <label class="form-label">招募人数</label>
        <input type="number" name="need_num" class="form-control form-control-lg" min="1" required placeholder="请输入人数">
    </div>
    <div class="col-md-4">
        <label class="form-label">截止时间 <span class="text-muted fw-normal">（可选）</span></label>
        <input type="text" class="form-control form-control-lg" id="publishEndTime" placeholder="点击选择截止时间">
    </div>
</div>
<div class="row g-4 mt-2">
    <div class="col-md-6">
        <label class="form-label">QQ群号 <span class="text-muted fw-normal">（可选）</span></label>
        <input type="text" name="qq_group" class="form-control form-control-lg" placeholder="选填，报名成功后展示给学生">
    </div>
</div>
```

- [ ] **Step 2: 添加空值确认逻辑**

在 `addRecruitForm` 的 submit 事件监听中（约第 512 行），在构造 formData 之后、发送请求之前，加入空值检查：

```javascript
document.getElementById("addRecruitForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const submitBtn = e.target.querySelector('button');
    
    // 新增：QQ群号为空时确认
    const qqGroupVal = e.target.querySelector('input[name="qq_group"]').value.trim();
    if (!qqGroupVal) {
        const confirmed = await showConfirm(
            "确认发布",
            "你没有填写QQ群号，已报名的学生将无法加入考务群接收通知，确定继续发布？"
        );
        if (!confirmed) return;
    }
    
    submitBtn.disabled = true;
    submitBtn.textContent = "发布中...";
    // ... 后续代码不变 ...
```

---

### Task 4: 学生端报名成功弹窗

**Files:**
- Modify: `app/static/student.html:214-260`

- [ ] **Step 1: 报名成功后显示入群弹窗**

修改 `POST /api/reg` 提交成功后的处理（约第 232-248 行），在成功时判断 `qq_group` 并弹窗：

找到 `.then` 或 `if (res.ok)` 块：

```javascript
if (res.ok) {
    data = await res.json();
    showToast(data.msg || "报名成功", 'success');
    
    // 新增：如果有QQ群号，弹窗提醒
    if (data.qq_group) {
        showQQGroupModal(data.qq_group);
    }
    
    // 报名成功后清空保存的状态
    // ... 后续清空逻辑不变 ...
```

- [ ] **Step 2: 创建 QQ 群弹窗 HTML 和函数**

在 `student.html` 中，在取消报名 modal 后面（第 110 行附近）新增入群提醒模态框：

```html
<!-- QQ群入群提醒弹窗 -->
<div class="modal fade" id="qqGroupModal" tabindex="-1">
    <div class="modal-dialog modal-dialog-centered">
        <div class="modal-content" style="border-radius:16px;">
            <div class="modal-body text-center py-5 px-4">
                <div style="font-size:48px;margin-bottom:12px;">✅</div>
                <h4 class="mb-2">报名成功！</h4>
                <p class="text-muted mb-4" id="qqGroupExamName" style="font-size:15px;"></p>
                <div style="background:#e8f5e9;border-radius:12px;padding:16px;margin-bottom:16px;">
                    <div style="font-size:14px;color:#2e7d32;margin-bottom:6px;">📢 请加入考务工作QQ群</div>
                    <div id="qqGroupNumber" style="font-size:28px;font-weight:bold;color:#1565c0;letter-spacing:2px;"></div>
                    <div style="font-size:13px;color:#666;margin-top:6px;">后续通知将通过群消息发布</div>
                </div>
                <button type="button" class="btn btn-primary btn-lg px-5" data-bs-dismiss="modal">知道了</button>
            </div>
        </div>
    </div>
</div>
```

在 JavaScript 中新增显示函数（放在合适位置，比如工具函数区附近）：

```javascript
// QQ群入群提醒
let qqGroupModalInstance = null;

function showQQGroupModal(qqGroup) {
    if (!qqGroupModalInstance) {
        qqGroupModalInstance = new bootstrap.Modal(document.getElementById('qqGroupModal'));
    }
    // 从下拉列表找到当前选中的考试名称
    const examSelect = document.getElementById('examSelect');
    const selectedOpt = examSelect.options[examSelect.selectedIndex];
    const examName = selectedOpt ? selectedOpt.textContent.split('（')[0] : '';
    
    document.getElementById('qqGroupExamName').textContent = `「${examName}」`;
    document.getElementById('qqGroupNumber').textContent = qqGroup;
    qqGroupModalInstance.show();
}
```

---

### Task 5: 学生端报名记录显示群号

**Files:**
- Modify: `app/static/student.html:295-353`

- [ ] **Step 1: 报名记录列表显示群号**

在 "查询我的报名记录" 的列表渲染中（约第 295-353 行），在每个列表项中添加群号显示：

找到 `item.qq` 显示行（约第 326-334 行，显示个人 QQ 号那一段），在后面新增 QQ 群号显示：

```javascript
// 个人QQ号（已有代码）
const row3 = document.createElement("div");
row3.className = "row mb-2";
// ... 个人QQ号代码不变 ...
li.appendChild(row3);

// 新增：考务QQ群号
if (item.qq_group) {
    const rowGroup = document.createElement("div");
    rowGroup.className = "row mb-2";
    const rgc1 = document.createElement("div");
    rgc1.className = "col-3 text-muted small";
    rgc1.textContent = "考务群";
    const rgc2 = document.createElement("div");
    rgc2.className = "col-9";
    rgc2.innerHTML = `<span style="color:#e67e22;">📢 请加群：${item.qq_group}</span>`;
    rowGroup.appendChild(rgc1);
    rowGroup.appendChild(rgc2);
    li.appendChild(rowGroup);
}
```

---

### Task 6: 验证与提交

- [ ] **Step 1: 启动服务并手动验证**

```bash
cd /Users/wff/KAIfa/kaowu-system/app && uvicorn main:app --reload --port 8000
```

验证要点：
1. 管理员发布招募：填群号和不填群号两种情况都能发布，不填时弹确认框
2. 学生报名有群号的招募：报名成功后弹窗显示群号
3. 学生报名无群号的招募：报名成功无弹窗（不报错）
4. 学生查询报名记录：有群号的显示群号行，无群号的不显示
5. 编辑招募：确认编辑面板不涉及群号修改

- [ ] **Step 2: 提交代码**

```bash
git add app/main.py app/static/admin.html app/static/student.html
git commit -m "feat: 招募添加QQ群号提醒功能

管理员发布招募时可选填QQ群号，学生报名成功后弹窗提示加群，
报名记录中也会显示群号。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```
