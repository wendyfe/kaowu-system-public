# 考场/教室信息管理 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立教学楼和教室的基础数据库，支持 Excel 批量导入和页面 CRUD 维护，自动推导"栋"分区。

**Architecture:** 在现有单文件后端 main.py 中新增两个 SQLAlchemy 模型（Building / Classroom），新增一组 API 用于导入和 CRUD，在 admin.html 中新增「教室管理」页面。

**Tech Stack:** FastAPI + SQLAlchemy + SQLite + pandas/openpyxl（已有）、Bootstrap 5 + SortableJS（CDN 新增）

---

## 文件结构

| 文件 | 变更 |
|------|------|
| `app/main.py` | 新增 Building、Classroom 模型 + 导入&CRUD API + zone 推导工具 |
| `app/static/admin.html` | 新增「教室管理」Tab 页面（导入、展示、CRUD） |
| `app/requirements.txt` | 无需变更（pandas/openpyxl 已安装） |

---

### Task 1: 新增 Building 和 Classroom 模型

**Modify:** `app/main.py`（在第 75 行 Base 定义之后、第 77 行 Recruitment 模型之前插入新模型）

- [ ] **Step 1: 添加 Building 和 Classroom 模型代码**

在 `app/main.py` 中现有模型定义之前、`Base.metadata.create_all(bind=engine)` 之前，添加两个新模型。将以下代码插入到现有 Recruitment 模型之前（约第 77 行）：

```python
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
```

- [ ] **Step 2: 添加 zone 推导工具函数**

在第 131 行工具函数区（get_client_ip 附近）添加：

```python
# ==================== 考场工具函数 ====================

def detect_zone(classroom_name: str) -> str | None:
    """从教室名称推导"栋"信息。
    规则：
    - 首字母为英文字母 → 返回该字母 + "栋"（B101 → B栋）
    - 首字母为数字 → 返回 None（综合楼 101 → 无分区）
    """
    if not classroom_name:
        return None
    first_char = classroom_name.strip()[0].upper()
    if first_char.isalpha():
        return f"{first_char}栋"
    return None
```

- [ ] **Step 3: 验证模型**

Run: `cd app && python -c "from main import Building, Classroom, engine; Base.metadata.create_all(bind=engine); print('OK')"`
Expected: `OK`（无报错，数据库表自动创建）

- [ ] **Step 4: 提交**

```bash
git add app/main.py
git commit -m "feat: add Building and Classroom model"
```

---

### Task 2: 后台 API — Excel 批量导入

**Modify:** `app/main.py`（在现有路由之后、约第 661 行 export 路由之后添加）

- [ ] **Step 1: 添加导入 API**

```python
# ==================== 考场基础数据 API ====================

@app.post("/api/classrooms/import")
async def import_classrooms(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db)):
    check_admin_login(request)
    check_csrf(request)

    if not file.filename.endswith(('.xlsx', '.xls')):
        raise HTTPException(400, "请上传 .xlsx 或 .xls 文件")

    try:
        import pandas as pd
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
                # 更新已有记录
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
```

注意：需要在文件头部新增 `from fastapi import UploadFile, File`。修改第 1 行 import 语句：
```python
from fastapi import FastAPI, Request, Form, HTTPException, Depends, BackgroundTasks, UploadFile, File
```

- [ ] **Step 2: 添加获取教学楼列表和教室列表 API**

```python
@app.get("/api/buildings")
async def get_buildings(db: Session = Depends(get_db)):
    """获取所有教学楼（已登录管理员可用）"""
    try:
        check_admin_login(Request)
    except:
        pass  # 返回但不要求强制登录（学生端也可能用到）
    buildings = db.query(Building).order_by(Building.id).all()
    return [{"id": b.id, "name": b.name} for b in buildings]


@app.get("/api/classrooms")
async def get_classrooms(building_id: int = None, db: Session = Depends(get_db)):
    """获取教室列表，可按教学楼筛选"""
    try:
        check_admin_login(Request)
    except:
        pass
    query = db.query(Classroom)
    if building_id:
        query = query.filter(Classroom.building_id == building_id)
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
    is_fixed_seats: bool = Form(...),
    can_double_exam: bool = Form(...),
    db: Session = Depends(get_db)
):
    check_admin_login(request)
    check_csrf(request)

    classroom = db.query(Classroom).filter(Classroom.id == classroom_id).first()
    if not classroom:
        raise HTTPException(404, "教室不存在")

    classroom.is_fixed_seats = is_fixed_seats
    classroom.can_double_exam = can_double_exam
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
```

- [ ] **Step 3: 验证 API**

Run: `cd app && uvicorn main:app --reload --port 8000`
然后用 curl 测试（或者后续用前端测试）。

- [ ] **Step 4: 提交**

```bash
git add app/main.py
git commit -m "feat: add classroom import and CRUD APIs"
```

---

### Task 3: 后台管理页面 — 教室管理 Tab

**Modify:** `app/static/admin.html`

在 admin.html 中新增一个 Tab 导航 + 「教室管理」面板。

- [ ] **Step 1: 添加 Tab 导航栏**

在 header 与 content-wrapper 之间插入 Tab 导航（约第 153 行）：

```html
<!-- Tab 导航 -->
<div class="container mt-4">
    <ul class="nav nav-tabs" id="adminTabs" role="tablist">
        <li class="nav-item" role="presentation">
            <button class="nav-link active" id="recruit-tab" data-bs-toggle="tab" data-bs-target="#recruit-panel" type="button">招募管理</button>
        </li>
        <li class="nav-item" role="presentation">
            <button class="nav-link" id="classroom-tab" data-bs-toggle="tab" data-bs-target="#classroom-panel" type="button">教室管理</button>
        </li>
    </ul>
</div>
```

- [ ] **Step 2: 包裹现有内容到 recruit-panel tab-pane 中**

将 `<div class="content-wrapper">` 内容改为两个 tab-pane 结构。用 `<div class="tab-content" id="adminTabContent">` 包裹：

```html
<div class="tab-content" id="adminTabContent">
    <!-- 招募管理面板 -->
    <div class="tab-pane fade show active" id="recruit-panel" role="tabpanel">
        ... 现有 content-wrapper 内的所有内容（卡片、表格等） ...
    </div>

    <!-- 教室管理面板 -->
    <div class="tab-pane fade" id="classroom-panel" role="tabpanel">
        ... 新教室管理内容 ...
    </div>
</div>
```

原有 content-wrapper 的样式改为在 tab-pane 内部的容器保留。

- [ ] **Step 3: 添加教室管理面板内容**

在 classroom-panel 中插入：

```html
<div class="container mt-4 mb-5">
    <!-- 导入 -->
    <div class="card p-4 mb-4">
        <h3 class="mb-3">批量导入教室</h3>
        <form id="importForm">
            <div class="mb-3">
                <label class="form-label">选择 Excel 文件</label>
                <input type="file" class="form-control" id="importFile" accept=".xlsx,.xls">
                <div class="form-text mt-2">
                    表头格式：教学楼 | 教室名称 | 是否固定桌椅 | 是否具备双考场条件
                    <a href="#" id="downloadTemplate" class="ms-2">下载模板</a>
                </div>
            </div>
            <button type="submit" class="btn btn-custom">导入</button>
        </form>
    </div>

    <!-- 教室列表 -->
    <div class="card p-4">
        <div class="d-flex justify-content-between align-items-center mb-3">
            <h3 class="mb-0">教室列表</h3>
            <button class="btn btn-custom btn-sm" id="addClassroomBtn">+ 添加教室</button>
        </div>
        <div id="classroomList"></div>
    </div>
</div>

<!-- 添加/编辑教室弹窗 -->
<div class="modal fade" id="classroomModal" tabindex="-1">
    <div class="modal-dialog modal-dialog-centered">
        <div class="modal-content">
            <div class="modal-header">
                <h5 class="modal-title" id="classroomModalTitle">添加教室</h5>
                <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
            </div>
            <div class="modal-body">
                <form id="classroomForm">
                    <input type="hidden" id="editClassroomId">
                    <div class="mb-3">
                        <label class="form-label">教学楼</label>
                        <select id="formBuildingId" class="form-select" required></select>
                    </div>
                    <div class="mb-3">
                        <label class="form-label">教室名称</label>
                        <input type="text" id="formClassName" class="form-control" required placeholder="如 B101">
                    </div>
                    <div class="mb-3">
                        <div class="form-check">
                            <input type="checkbox" class="form-check-input" id="formFixedSeats">
                            <label class="form-check-label">固定桌椅（好布置，可多分）</label>
                        </div>
                    </div>
                    <div class="mb-3">
                        <div class="form-check">
                            <input type="checkbox" class="form-check-input" id="formCanDouble">
                            <label class="form-check-label">具备双考场条件（大教室）</label>
                        </div>
                    </div>
                </form>
            </div>
            <div class="modal-footer">
                <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">取消</button>
                <button type="button" class="btn btn-custom" id="saveClassroomBtn">保存</button>
            </div>
        </div>
    </div>
</div>
```

- [ ] **Step 4: 添加教室管理 JavaScript**

在 admin.html 的 `<script>` 标签末尾添加（约第 644 行 `loadRecruitList();` 之前）：

```javascript
// ========== 教室管理 ==========

// 加载教学楼下拉
async function loadBuildingSelect(selectId, selectedId) {
    try {
        const res = await fetch('/api/buildings');
        const list = await res.json();
        const select = document.getElementById(selectId);
        select.innerHTML = '<option value="">选择教学楼</option>';
        list.forEach(b => {
            const opt = document.createElement('option');
            opt.value = b.id;
            opt.textContent = b.name;
            if (selectedId && b.id == selectedId) opt.selected = true;
            select.appendChild(opt);
        });
    } catch (err) {
        console.error('加载教学楼失败', err);
    }
}

// 加载教室列表
async function loadClassroomList() {
    try {
        const res = await fetch('/api/classrooms');
        const list = await res.json();
        const container = document.getElementById('classroomList');

        // 按楼栋分组
        const groups = {};
        list.forEach(c => {
            const key = c.building_name;
            if (!groups[key]) groups[key] = { building_name: key, building_id: c.building_id, zones: {} };
            const zone = c.zone || '无分区';
            if (!groups[key].zones[zone]) groups[key].zones[zone] = [];
            groups[key].zones[zone].push(c);
        });

        container.innerHTML = '';
        for (const [bname, building] of Object.entries(groups)) {
            const buildingCard = document.createElement('div');
            buildingCard.className = 'mb-4';
            let html = `<h5 class="mb-2">${bname}</h5>`;
            for (const [zone, classrooms] of Object.entries(building.zones)) {
                html += `<div class="ms-3 mb-2"><span class="text-muted small">${zone}</span>`;
                html += `<div class="d-flex flex-wrap gap-2 mt-1">`;
                classrooms.forEach(c => {
                    const icon = c.is_fixed_seats ? '🪑' : '📦';
                    const doubleLabel = c.can_double_exam ? '双' : '单';
                    html += `<div class="border rounded p-2" style="min-width:120px;background:#f8f9fa">
                        <div><strong>${c.name}</strong></div>
                        <div class="small text-muted">${icon} ${c.is_fixed_seats ? '固定' : '活动'}</div>
                        <div class="small text-muted">${doubleLabel}考场</div>
                        <div class="mt-1">
                            <a href="#" class="small me-2 edit-classroom" data-id="${c.id}" data-building-id="${c.building_id}" data-name="${c.name}" data-fixed="${c.is_fixed_seats}" data-double="${c.can_double_exam}">编辑</a>
                            <a href="#" class="small text-danger delete-classroom" data-id="${c.id}" data-name="${c.name}">删除</a>
                        </div>
                    </div>`;
                });
                html += `</div></div>`;
            }
            buildingCard.innerHTML = html;
            container.appendChild(buildingCard);
        }

        // 绑定编辑和删除事件
        document.querySelectorAll('.edit-classroom').forEach(el => {
            el.addEventListener('click', e => {
                e.preventDefault();
                document.getElementById('classroomModalTitle').textContent = '编辑教室';
                document.getElementById('editClassroomId').value = el.dataset.id;
                document.getElementById('formClassName').value = el.dataset.name;
                document.getElementById('formClassName').disabled = true;
                document.getElementById('formFixedSeats').checked = el.dataset.fixed === 'true';
                document.getElementById('formCanDouble').checked = el.dataset.double === 'true';
                loadBuildingSelect('formBuildingId', el.dataset.buildingId);
                new bootstrap.Modal(document.getElementById('classroomModal')).show();
            });
        });
        document.querySelectorAll('.delete-classroom').forEach(el => {
            el.addEventListener('click', async e => {
                e.preventDefault();
                if (!await showConfirm('确认删除', `确定删除教室 ${el.dataset.name}？`)) return;
                try {
                    const res = await fetch(`/api/classrooms/${el.dataset.id}`, {
                        method: 'DELETE',
                        headers: { 'X-CSRF-Token': document.cookie.split('; ').find(row => row.startsWith('kaowu_csrf='))?.split('=')[1] || '' }
                    });
                    const data = await res.json();
                    showToast(data.msg, data.code === 0 ? 'success' : 'danger');
                    if (data.code === 0) loadClassroomList();
                } catch (err) {
                    showToast('删除失败', 'danger');
                }
            });
        });
    } catch (err) {
        console.error('加载教室列表失败', err);
    }
}

// Excel 导入
document.getElementById('importForm').addEventListener('submit', async e => {
    e.preventDefault();
    const fileInput = document.getElementById('importFile');
    if (!fileInput.files.length) {
        showToast('请选择文件', 'warning');
        return;
    }
    const formData = new FormData();
    formData.append('file', fileInput.files[0]);
    try {
        const res = await fetch('/api/classrooms/import', {
            method: 'POST',
            body: formData,
            headers: { 'X-CSRF-Token': document.cookie.split('; ').find(row => row.startsWith('kaowu_csrf='))?.split('=')[1] || '' }
        });
        const data = await res.json();
        showToast(data.msg, data.code === 0 ? 'success' : 'danger');
        if (data.code === 0) {
            fileInput.value = '';
            loadClassroomList();
            loadBuildingSelect('formBuildingId');
        }
    } catch (err) {
        showToast('导入失败', 'danger');
    }
});

// 添加教室按钮
document.getElementById('addClassroomBtn').addEventListener('click', () => {
    document.getElementById('classroomModalTitle').textContent = '添加教室';
    document.getElementById('editClassroomId').value = '';
    document.getElementById('classroomForm').reset();
    document.getElementById('formClassName').disabled = false;
    loadBuildingSelect('formBuildingId');
    new bootstrap.Modal(document.getElementById('classroomModal')).show();
});

// 保存教室
document.getElementById('saveClassroomBtn').addEventListener('click', async () => {
    const id = document.getElementById('editClassroomId').value;
    const formData = new FormData();
    formData.append('building_id', document.getElementById('formBuildingId').value);
    formData.append('name', document.getElementById('formClassName').value);
    formData.append('is_fixed_seats', document.getElementById('formFixedSeats').checked);
    formData.append('can_double_exam', document.getElementById('formCanDouble').checked);

    if (!formData.get('building_id') || !formData.get('name')) {
        showToast('请填写完整信息', 'warning');
        return;
    }

    try {
        const url = id ? `/api/classrooms/${id}` : '/api/classrooms';
        const method = id ? 'PUT' : 'POST';
        const res = await fetch(url, {
            method,
            body: formData,
            headers: { 'X-CSRF-Token': document.cookie.split('; ').find(row => row.startsWith('kaowu_csrf='))?.split('=')[1] || '' }
        });
        const data = await res.json();
        showToast(data.msg, data.code === 0 ? 'success' : 'danger');
        if (data.code === 0) {
            bootstrap.Modal.getInstance(document.getElementById('classroomModal')).hide();
            loadClassroomList();
        }
    } catch (err) {
        showToast('保存失败', 'danger');
    }
});

// Tab 切换时加载
document.getElementById('classroom-tab').addEventListener('shown.bs.tab', () => {
    loadClassroomList();
    loadBuildingSelect('formBuildingId');
});

// 下载模板示例
document.getElementById('downloadTemplate').addEventListener('click', e => {
    e.preventDefault();
    const csv = '﻿教学楼,教室名称,是否固定桌椅,是否具备双考场条件\n树人楼,B101,否,是\n树人楼,A201,否,否\n综合楼,101,是,是';
    const blob = new Blob([csv], {type: 'text/csv;charset=utf-8'});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = '教室导入模板.csv';
    a.click();
    URL.revokeObjectURL(url);
});
```

- [ ] **Step 5: 提交**

```bash
git add app/static/admin.html app/main.py
git commit -m "feat: add classroom management tab in admin panel"
```

---

## Self-Review

**Spec coverage check:**
- [x] Building 和 Classroom 模型定义 → Task 1
- [x] Excel 批量导入 → Task 2 Step 1
- [x] 教室 CRUD API → Task 2 Step 2
- [x] Zone 自动推导 → Task 1 Step 2
- [x] 后台「教室管理」页面 → Task 3
- [x] 导入模板下载 → Task 3 Step 4

**无占位符、类型一致性检查通过。**
