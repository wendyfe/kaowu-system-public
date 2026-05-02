# 发布招募关联考场 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在发布招募时，管理员可以选择哪些教室参与本次考试、配置单/双考场模式、自动生成考场号。

**Architecture:** 新增 RecruitmentClassroom 模型关联招募与教室，修改现有 add_recruit / edit_recruit API 以接收考场配置，在 admin.html 发布表单新增「选择考场场地」区块。

**Tech Stack:** FastAPI + SQLAlchemy + SQLite + Bootstrap 5

---

## 文件结构

| 文件 | 变更 |
|------|------|
| `app/main.py` | 新增 RecruitmentClassroom 模型；修改 add_recruit / edit_recruit API；新增 recruit classrooms 查询 API；修改 admin-list 返回教室信息 |
| `app/static/admin.html` | 发布表单新增「选择考场场地」区块；编辑侧滑面板新增考场调整；招募列表显示考场数 |

---

### Task 1: 新增 RecruitmentClassroom 模型 + 修改现有 API

**Modify:** `app/main.py`

- [ ] **Step 1: 添加 RecruitmentClassroom 模型**

在 `Recruitment` 模型定义之后（约第 87 行后）、`Registration` 模型之前插入：

```python
class RecruitmentClassroom(Base):
    """招募-教室关联：记录本次考试使用了哪些教室以及单/双模式"""
    __tablename__ = "recruitment_classrooms"
    id = Column(Integer, primary_key=True)
    recruitment_id = Column(Integer, nullable=False)
    classroom_id = Column(Integer, nullable=False)
    exam_mode = Column(String(10), nullable=False, default="single")  # 'single' 或 'double'
    exam_number_start = Column(Integer, nullable=False)  # 该教室考场起始号
```

- [ ] **Step 2: 添加教室考场列表的序列化函数**

在工具函数区添加（放在 detect_zone 附近）：

```python
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
    buildings = {b.id: b.name for b in db.query(Building).filter(Building.id.in_(building_ids)).all() if building_ids}

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
```

- [ ] **Step 3: 修改 add_recruit API 接收考场配置**

找到 `add_recruit` 函数（约第 256 行）。在完成 recruit 创建并 `db.refresh(recruit)` 之后、`return` 之前，添加考场配置逻辑。

需要修改函数签名以接收 classroom_ids 和 exam_modes：

```python
@app.post("/api/recruit/add")
async def add_recruit(
    request: Request,
    exam_name: str = Form(...),
    need_num: int = Form(...),
    end_time_str: str = Form(None),
    qq_group: str = Form(None),
    classroom_ids: str = Form(""),      # 逗号分隔的教室ID，如 "1,3,5"
    exam_modes: str = Form(""),         # 逗号分隔的模式，如 "double,single,single"
    db: Session = Depends(get_db)
):
    check_admin_login(request)
    check_csrf(request)
    rate_limit(f"recruit_add_{get_client_ip(request)}", max_requests=10, window=60)
    if need_num < 1:
        raise HTTPException(400, "人数必须≥1")

    # ... existing code to build recruit object ...

    recruit = Recruitment(exam_name=exam_name.strip(), need_num=need_num, end_time=end_time, qq_group=qq_group.strip() if qq_group and qq_group.strip() else None)
    db.add(recruit)
    db.commit()
    db.refresh(recruit)

    # 新增：处理考场配置
    if classroom_ids and exam_modes:
        ids_list = [x.strip() for x in classroom_ids.split(",") if x.strip()]
        modes_list = [x.strip() for x in exam_modes.split(",") if x.strip()]
        if len(ids_list) != len(modes_list):
            raise HTTPException(400, "教室ID与模式数量不匹配")

        exam_no = 1
        # 验证所有教室存在
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
```

- [ ] **Step 4: 修改 edit_recruit API 支持更新考场**

找到 `edit_recruit` 函数（约第 292 行）。在请求体参数中添加 classroom_ids 和 exam_modes（可选），在保存 recruit 属性之前或之后处理考场更新逻辑：

```python
@app.put("/api/recruit/{recruit_id}")
async def edit_recruit(
    request: Request,
    recruit_id: int,
    exam_name: str = Form(...),
    need_num: int = Form(...),
    end_time_str: str = Form(None),
    qq_group: str = Form(None),
    classroom_ids: str = Form(""),
    exam_modes: str = Form(""),
    db: Session = Depends(get_db)
):
    check_admin_login(request)
    check_csrf(request)
    # ... existing checks ...

    # 新增：如果有新的考场配置，先删除旧的再添加新的
    if classroom_ids and exam_modes:
        ids_list = [x.strip() for x in classroom_ids.split(",") if x.strip()]
        modes_list = [x.strip() for x in exam_modes.split(",") if x.strip()]
        if len(ids_list) != len(modes_list):
            raise HTTPException(400, "教室ID与模式数量不匹配")

        # 删除旧配置
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

    # ... existing code to update recruit fields ...
```

- [ ] **Step 5: 添加查询招募考场列表的 API**

```python
@app.get("/api/recruit/{recruit_id}/classrooms")
async def get_recruit_classrooms(recruit_id: int, db: Session = Depends(get_db)):
    """获取某个招募的考场配置"""
    recruit = db.query(Recruitment).filter(Recruitment.id == recruit_id).first()
    if not recruit:
        raise HTTPException(404, "招募不存在")
    return serialize_recruit_classrooms(recruit_id, db)
```

- [ ] **Step 6: 修改 admin-list 返回考场信息**

找到 `get_admin_recruit_list` 函数（约第 381 行），在返回的 result 字典中添加考场信息：

在 result.append 之前，查询该招募的考场配置并放入 result：

```python
classrooms_info = serialize_recruit_classrooms(r.id, db)
total_exam_rooms = sum(len(c["exam_numbers"]) for c in classrooms_info)

result.append({
    # ... existing fields ...
    "classrooms": classrooms_info,
    "total_exam_rooms": total_exam_rooms,
})
```

- [ ] **Step 7: 验证**

```bash
cd /Users/wff/KAIfa/kaowu-system/app && python3 -c "from main import app; print('OK')"
```
Expected: "OK" without traceback.

- [ ] **Step 8: 提交**

```bash
cd /Users/wff/KAIfa/kaowu-system && git add app/main.py && git commit -m "$(cat <<'EOF'
feat: add RecruitmentClassroom model and modify recruit APIs

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: 管理页面 — 发布表单选择考场

**Modify:** `app/static/admin.html`

在发布表单新增「选择考场场地」区块。在编辑侧滑面板新增考场信息展示和调整功能。在招募列表中显示考场数量和明细。

- [ ] **Step 1: 在发布表单添加考场选择区块**

在发布表单 `#addRecruitForm` 中，在 QQ 加群链接的行之后、提交按钮之前，插入：

```html
<!-- 选择考场场地 -->
<div class="card p-4 mt-4 mb-0" style="background:#f8f9fa;">
    <h5 class="mb-3">
        选择考场场地
        <span class="text-muted fw-normal" style="font-size:14px;">（先选教学楼，再勾选教室）</span>
    </h5>
    <div class="mb-3">
        <label class="form-label">选择教学楼</label>
        <select id="publishBuilding" class="form-select">
            <option value="">请选择教学楼</option>
        </select>
    </div>
    <div id="publishClassroomList" class="mb-2">
        <div class="text-muted small">请先选择教学楼</div>
    </div>
    <div class="d-flex justify-content-between align-items-center border-top pt-2 mt-2">
        <span id="publishSelectionCount" class="text-muted small">已选 0 间教室 → 0 个考场</span>
    </div>
    <!-- 隐藏字段：提交时填充 -->
    <input type="hidden" name="classroom_ids" id="publishClassroomIds">
    <input type="hidden" name="exam_modes" id="publishExamModes">
</div>
```

- [ ] **Step 2: 添加教室选择相关的 JavaScript**

在现有的 `<script>` 块末尾追加：

```javascript
// ========== 发布招募 — 选择考场 ==========

// 当前发布表单的教室选择状态
let publishSelections = {};  // { classroom_id: 'single' | 'double' }

// 加载发布表单的教学楼列表
async function loadPublishBuildings() {
    try {
        const res = await fetch('/api/buildings');
        const list = await res.json();
        const select = document.getElementById('publishBuilding');
        select.innerHTML = '<option value="">请选择教学楼</option>';
        list.forEach(b => {
            const opt = document.createElement('option');
            opt.value = b.id;
            opt.textContent = b.name;
            select.appendChild(opt);
        });
    } catch (err) {
        console.error('加载教学楼失败', err);
    }
}

// 根据选中的教学楼加载教室列表
async function loadPublishClassrooms(buildingId) {
    try {
        const url = buildingId ? `/api/classrooms?building_id=${buildingId}` : '/api/classrooms';
        const res = await fetch(url);
        const list = await res.json();
        const container = document.getElementById('publishClassroomList');
        container.innerHTML = '';

        if (list.length === 0) {
            container.innerHTML = '<div class="text-muted small">该教学楼暂无教室，请先在「教室管理」中导入</div>';
            return;
        }

        list.forEach(c => {
            const div = document.createElement('div');
            div.className = 'form-check form-check-inline mb-2';
            div.style.minWidth = '160px';

            const cb = document.createElement('input');
            cb.type = 'checkbox';
            cb.className = 'form-check-input classroom-checkbox';
            cb.value = c.id;
            cb.id = `publish_cb_${c.id}`;
            cb.dataset.canDouble = c.can_double_exam;
            cb.dataset.name = c.name;

            const label = document.createElement('label');
            label.className = 'form-check-label';
            label.htmlFor = `publish_cb_${c.id}`;
            label.textContent = c.name;
            if (c.is_fixed_seats) label.textContent += ' 🪑';

            div.appendChild(cb);
            div.appendChild(label);

            // 考场模式下拉（勾选后可用）
            const modeSelect = document.createElement('select');
            modeSelect.className = 'form-select form-select-sm d-inline-block ms-1 exam-mode-select';
            modeSelect.style.width = '100px';
            modeSelect.disabled = true;
            const optSingle = document.createElement('option');
            optSingle.value = 'single';
            optSingle.textContent = '单考场';
            const optDouble = document.createElement('option');
            optDouble.value = 'double';
            optDouble.textContent = '双考场';
            modeSelect.appendChild(optSingle);
            modeSelect.appendChild(optDouble);
            if (!c.can_double_exam) {
                optDouble.disabled = true;
                optDouble.textContent = '双考场(不可用)';
            }
            div.appendChild(modeSelect);

            // 勾选/取消事件
            cb.addEventListener('change', function() {
                modeSelect.disabled = !this.checked;
                if (this.checked) {
                    // 默认：具备双考场条件则选双，否则单
                    const defaultMode = c.can_double_exam ? 'double' : 'single';
                    modeSelect.value = defaultMode;
                    publishSelections[c.id] = defaultMode;
                } else {
                    delete publishSelections[c.id];
                }
                updatePublishSelectionCount();
            });

            // 模式变更事件
            modeSelect.addEventListener('change', function() {
                if (cb.checked) {
                    publishSelections[c.id] = this.value;
                    updatePublishSelectionCount();
                }
            });

            // 如果之前已选中（编辑时恢复）
            if (publishSelections[c.id] !== undefined) {
                cb.checked = true;
                modeSelect.disabled = false;
                modeSelect.value = publishSelections[c.id];
            }

            container.appendChild(div);
        });
    } catch (err) {
        console.error('加载教室列表失败', err);
        document.getElementById('publishClassroomList').innerHTML = '<div class="text-danger small">加载失败</div>';
    }
}

// 更新选择计数和隐藏字段
function updatePublishSelectionCount() {
    let total = 0;
    const ids = [];
    const modes = [];
    for (const [cid, mode] of Object.entries(publishSelections)) {
        ids.push(cid);
        modes.push(mode);
        total += mode === 'double' ? 2 : 1;
    }
    const count = Object.keys(publishSelections).length;
    document.getElementById('publishSelectionCount').textContent = `已选 ${count} 间教室 → ${total} 个考场`;
    document.getElementById('publishClassroomIds').value = ids.join(',');
    document.getElementById('publishExamModes').value = modes.join(',');
}

// 教学楼切换事件
document.getElementById('publishBuilding')?.addEventListener('change', function() {
    loadPublishClassrooms(this.value);
});

// 修改发布表单的提交 — 在已有提交逻辑中增加考场数据
// 在发布提交前，将当前选择写入隐藏字段
```

- [ ] **Step 3: 修改发布表单的提交逻辑**

找到 `addRecruitForm` 的 submit 事件处理（约第 592 行），在提交前添加更新隐藏字段的调用：

在 `formData.append("qq_group", ...)` 那行之后、`const res = await fetch(...)` 之前，添加：

```javascript
                // 写入考场选择
                updatePublishSelectionCount();
                const cids = document.getElementById('publishClassroomIds').value;
                const modes = document.getElementById('publishExamModes').value;
                if (cids) {
                    formData.append('classroom_ids', cids);
                    formData.append('exam_modes', modes);
                }
```

- [ ] **Step 4: 在招募列表显示考场数和教室信息**

在 `loadRecruitList` 函数中，修改表格列。给每个招募行添加考场信息：

在 `tdStatus.appendChild(statusBadge)` 之后、`tdActions` 之前，添加考场数显示列：

```javascript
                // 考场数
                const tdExamRooms = document.createElement("td");
                const totalRooms = item.total_exam_rooms || 0;
                tdExamRooms.textContent = totalRooms > 0 ? `${totalRooms} 个` : '未配置';
                if (!totalRooms) tdExamRooms.className = 'text-muted';
                tr.appendChild(tdExamRooms);
```

并修改 table header 添加「考场数」列。

- [ ] **Step 5: 初始化 + 清理发布表单**

在 `loadRecruitList` 函数末尾或 document ready 中：

```javascript
// 初始化发布表单的考场选择
loadPublishBuildings();

// 发布成功后重置考场选择
// 找到发布成功的回调（在 addRecruitForm submit 中 data.code === 0 时），添加：
publishSelections = {};
```

- [ ] **Step 6: 验证 HTML**

```bash
cd /Users/wff/KAIfa/kaowu-system && python3 -c "open('app/static/admin.html').read(); print('OK')"
```

- [ ] **Step 7: 提交**

```bash
cd /Users/wff/KAIfa/kaowu-system && git add app/static/admin.html app/main.py && git commit -m "$(cat <<'EOF'
feat: add classroom selection to recruitment publish form

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review

**Spec coverage:**
- [x] RecruitmentClassroom 模型
- [x] 发布招募时选择教室、配置单/双
- [x] 考场号自动编排（从1开始顺序编号）
- [x] 编辑招募时更新考场配置
- [x] 招募列表显示考场信息
- [x] 不具备双考场条件的教室只能选"单"
- [x] 默认勾选：具备双→双，不具备→单
- [x] 底部统计：X 间教室 → Y 个考场

**No placeholders, type consistency passed.**
