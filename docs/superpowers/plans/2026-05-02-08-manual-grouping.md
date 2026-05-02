# 手动分组排班 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace auto-grouping with a 4-step manual workflow: general supervisor → building supervisors → drag grouping → classroom assignment.

**Architecture:** Backend adds BuildingSupervisor model and general_supervisor_id field, new manual grouping APIs. Frontend rewrites the grouping modal into a 4-tab wizard with SortableJS drag-and-drop.

**Tech Stack:** FastAPI + SQLAlchemy + SQLite (backend), Bootstrap 5 + SortableJS + vanilla JS (frontend).

**Files modified:**
- `app/main.py` — new model + migration + APIs + remove auto-group functions
- `app/static/admin.html` — rewrite grouping panel HTML + JS

---

### Task 1: Add BuildingSupervisor model and migration

**Files:**
- Modify: `app/main.py`

- [ ] **Step 1: Add `general_supervisor_id` to Recruitment model**

  Find the Recruitment model (around line 78). Add one new field after `qq_group`:

  ```python
  class Recruitment(Base):
      __tablename__ = "recruitment"
      id = Column(Integer, primary_key=True)
      exam_name = Column(String(100), nullable=False)
      need_num = Column(Integer, nullable=False)
      create_time = Column(DateTime, default=now_beijing)
      is_active = Column(Boolean, default=True)
      qq_group = Column(String(300), nullable=True)
      end_time = Column(DateTime, nullable=True)
      general_supervisor_id = Column(Integer, nullable=True)  # 总负责人，关联 registration.id
  ```

- [ ] **Step 2: Add BuildingSupervisor model**

  After the AcceptanceRecord model (after line ~191), add:

  ```python
  class BuildingSupervisor(Base):
      """楼栋负责人"""
      __tablename__ = "building_supervisors"
      id = Column(Integer, primary_key=True)
      recruitment_id = Column(Integer, nullable=False)
      zone_name = Column(String(20), nullable=False)    # 如 "B栋"
      registration_id = Column(Integer, nullable=False)  # 关联 registration.id
  ```

- [ ] **Step 3: Add migration for existing databases**

  After `Base.metadata.create_all(bind=engine)` (line 195), add migration SQL:

  ```python
  # 数据库迁移：手动分组相关字段
  try:
      with engine.connect() as conn:
          conn.execute(text("ALTER TABLE recruitment ADD COLUMN general_supervisor_id INTEGER DEFAULT NULL"))
          conn.commit()
  except Exception:
      pass
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
      pass
  ```

- [ ] **Step 4: Commit**

  ```bash
  git add app/main.py
  git commit -m "feat: add BuildingSupervisor model and general_supervisor_id field"
  ```

---

### Task 2: Add manual grouping backend APIs

**Files:**
- Modify: `app/main.py`

- [ ] **Step 1: Add `GET /api/recruit/{id}/manual-grouping-data` endpoint**

  This returns all data needed for the manual grouping panel. Add it after the `get_recruit_classrooms` endpoint (around line 1380):

  ```python
  @app.get("/api/recruit/{recruit_id}/manual-grouping-data")
  async def get_manual_grouping_data(recruit_id: int, db: Session = Depends(get_db)):
      """获取手动分组所需全部数据"""
      recruit = db.query(Recruitment).filter(Recruitment.id == recruit_id).first()
      if not recruit:
          raise HTTPException(404, "招募不存在")

      # 报名人员列表
      registrations = db.query(Registration).filter(
          Registration.recruitment_id == recruit_id
      ).order_by(Registration.id).all()

      reg_list = [{
          "id": r.id, "student_id": r.student_id, "name": r.name,
          "gender": r.gender, "has_experience": r.has_experience,
      } for r in registrations]

      # 总负责人
      general = recruit.general_supervisor_id

      # 楼栋负责人
      bs_list = db.query(BuildingSupervisor).filter(
          BuildingSupervisor.recruitment_id == recruit_id
      ).all()
      supervisors = [{"id": bs.id, "zone_name": bs.zone_name, "registration_id": bs.registration_id} for bs in bs_list]

      # 已有的组
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

      # 所有教室信息（含考场数）
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
  ```

- [ ] **Step 2: Add general supervisor API**

  ```python
  @app.put("/api/recruit/{recruit_id}/general-supervisor")
  async def set_general_supervisor(
      request: Request, recruit_id: int,
      registration_id: int = Form(None),
      db: Session = Depends(get_db)
  ):
      """设置或移除总负责人"""
      check_admin_login(request)
      check_csrf(request)

      recruit = db.query(Recruitment).filter(Recruitment.id == recruit_id).first()
      if not recruit:
          raise HTTPException(404, "招募不存在")

      if registration_id:
          reg = db.query(Registration).filter(
              Registration.id == registration_id,
              Registration.recruitment_id == recruit_id
          ).first()
          if not reg:
              raise HTTPException(400, "该报名记录不存在或不在此招募中")

      recruit.general_supervisor_id = registration_id
      db.commit()
      return {"code": 0, "msg": "总负责人已设置" if registration_id else "总负责人已移除",
              "general_supervisor_id": registration_id}
  ```

- [ ] **Step 3: Add building supervisors API**

  ```python
  @app.put("/api/recruit/{recruit_id}/building-supervisors")
  async def set_building_supervisors(
      request: Request, recruit_id: int,
      db: Session = Depends(get_db)
  ):
      """批量设置楼栋负责人"""
      check_admin_login(request)
      check_csrf(request)

      import json
      body = await request.json()
      supervisors = body.get("supervisors", [])  # [{zone_name, registration_id}]

      recruit = db.query(Recruitment).filter(Recruitment.id == recruit_id).first()
      if not recruit:
          raise HTTPException(404, "招募不存在")

      # 删除旧记录
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
  ```

- [ ] **Step 4: Add group CRUD APIs**

  ```python
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
      """删除空组（有成员的组不可删除）"""
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
  ```

- [ ] **Step 5: Add group member assignment API**

  ```python
  @app.put("/api/recruit/{recruit_id}/groups/{group_id}/members")
  async def set_group_members(
      request: Request, recruit_id: int, group_id: int,
      db: Session = Depends(get_db)
  ):
      """设置组成员（全量替换）"""
      check_admin_login(request)
      check_csrf(request)

      import json
      body = await request.json()
      member_ids = body.get("member_ids", [])

      group = db.query(RecruitmentGroup).filter(
          RecruitmentGroup.id == group_id,
          RecruitmentGroup.recruitment_id == recruit_id
      ).first()
      if not group:
          raise HTTPException(404, "组不存在")

      # 全量替换
      db.query(RecruitmentGroupMember).filter(
          RecruitmentGroupMember.group_id == group_id
      ).delete()
      db.flush()

      for rid in member_ids:
          # 验证成员属于此招募
          reg = db.query(Registration).filter(
              Registration.id == rid,
              Registration.recruitment_id == recruit_id
          ).first()
          if reg:
              db.add(RecruitmentGroupMember(group_id=group_id, registration_id=rid))

      db.commit()
      return {"code": 0, "msg": "组成员已更新"}
  ```

- [ ] **Step 6: Add classroom assignment API**

  ```python
  @app.put("/api/recruit/{recruit_id}/groups/{group_id}/classrooms")
  async def set_group_classrooms(
      request: Request, recruit_id: int, group_id: int,
      db: Session = Depends(get_db)
  ):
      """设置组负责的教室（全量替换，自动互斥检查）"""
      check_admin_login(request)
      check_csrf(request)

      import json
      body = await request.json()
      rc_ids = body.get("rc_ids", [])

      group = db.query(RecruitmentGroup).filter(
          RecruitmentGroup.id == group_id,
          RecruitmentGroup.recruitment_id == recruit_id
      ).first()
      if not group:
          raise HTTPException(404, "组不存在")

      # 检查互斥：其他组是否已占用
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

      # 全量替换
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
  ```

- [ ] **Step 7: Add finalize grouping API**

  ```python
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

      # 检查每组成员数
      all_groups = db.query(RecruitmentGroup).filter(
          RecruitmentGroup.recruitment_id == recruit_id
      ).all()
      for g in all_groups:
          cnt = db.query(RecruitmentGroupMember).filter(
              RecruitmentGroupMember.group_id == g.id
          ).count()
          if cnt == 0:
              raise HTTPException(400, f"第{g.id}组没有成员，请先分配人员")

      init_task_progress(recruit_id, db)
      init_acceptance_records(recruit_id, db)
      return {"code": 0, "msg": "分组已确认，任务清单和验收记录已创建"}
  ```

- [ ] **Step 8: Commit**

  ```bash
  git add app/main.py
  git commit -m "feat: add manual grouping APIs"
  ```

---

### Task 3: Remove auto-grouping code

**Files:**
- Modify: `app/main.py`

- [ ] **Step 1: Remove `auto_assign_groups()` function**

  Delete the entire function (around lines 411-502 in the current file, starts with `def auto_assign_groups` and ends at the line before `def save_groups_to_db`).

- [ ] **Step 2: Remove `save_groups_to_db()` function**

  Delete the entire function (starts with `def save_groups_to_db` and ends at the line before `def generate_verify_code`).

- [ ] **Step 3: Remove the old auto-group endpoint and save-groups endpoint**

  Remove these two endpoints:
  - `@app.post("/api/recruit/{recruit_id}/auto-group")` — calls `save_groups_to_db()`
  - `@app.post("/api/recruit/{recruit_id}/save-groups")` — manual save that's now replaced

  Keep:
  - `@app.get("/api/recruit/{recruit_id}/groups")` — may still be useful for the acceptance panel
  - `@app.get("/api/recruit/{recruit_id}/classrooms")` — still used elsewhere

- [ ] **Step 4: Commit**

  ```bash
  git add app/main.py
  git commit -m "refactor: remove auto-grouping code"
  ```

---

### Task 4: Rewrite grouping panel frontend (4-tab manual wizard)

**Files:**
- Modify: `app/static/admin.html`

This is the largest task. The current grouping modal HTML (lines 340-370) and JS (lines 1517-1684) need to be rewritten.

- [ ] **Step 1: Replace grouping modal HTML**

  Replace the existing grouping modal structure (around lines 340-370):

  ```html
  <!-- 分组排班弹窗 -->
  <div class="modal fade" id="groupingModal" tabindex="-1">
      <div class="modal-dialog modal-xl modal-dialog-centered modal-dialog-scrollable">
          <div class="modal-content">
              <div class="modal-header">
                  <h5 class="modal-title" id="groupingModalTitle">分组排班</h5>
                  <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
              </div>
              <div class="modal-body" id="groupingModalBody">
                  <!-- Tab 导航 -->
                  <ul class="nav nav-tabs mb-3" id="groupingTabs" role="tablist">
                      <li class="nav-item"><button class="nav-link active" id="tab-general-tab" data-bs-toggle="tab" data-bs-target="#tab-general" type="button">①总负责人</button></li>
                      <li class="nav-item"><button class="nav-link" id="tab-supervisor-tab" data-bs-toggle="tab" data-bs-target="#tab-supervisor" type="button">②楼栋负责人</button></li>
                      <li class="nav-item"><button class="nav-link" id="tab-groups-tab" data-bs-toggle="tab" data-bs-target="#tab-groups" type="button">③拖拽分组</button></li>
                      <li class="nav-item"><button class="nav-link" id="tab-classrooms-tab" data-bs-toggle="tab" data-bs-target="#tab-classrooms" type="button">④分配教室</button></li>
                  </ul>
                  <div class="tab-content">
                      <!-- Tab 1: 总负责人 -->
                      <div class="tab-pane fade show active" id="tab-general">
                          <div id="tabGeneralContent"><div class="text-center py-4 text-muted">加载中...</div></div>
                      </div>
                      <!-- Tab 2: 楼栋负责人 -->
                      <div class="tab-pane fade" id="tab-supervisor">
                          <div id="tabSupervisorContent"><div class="text-center py-4 text-muted">加载中...</div></div>
                      </div>
                      <!-- Tab 3: 拖拽分组 -->
                      <div class="tab-pane fade" id="tab-groups">
                          <div id="tabGroupsContent"><div class="text-center py-4 text-muted">加载中...</div></div>
                      </div>
                      <!-- Tab 4: 分配教室 -->
                      <div class="tab-pane fade" id="tab-classrooms">
                          <div id="tabClassroomsContent"><div class="text-center py-4 text-muted">加载中...</div></div>
                      </div>
                  </div>
              </div>
              <div class="modal-footer">
                  <button type="button" class="btn btn-custom" id="finalizeGroupBtn">✅ 确认分组并创建任务</button>
                  <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">关闭</button>
              </div>
          </div>
      </div>
  </div>
  ```

- [ ] **Step 2: Replace grouping JS — data loading and tab rendering**

  Replace ALL the grouping JS (from `let currentGroupingRecruitId = null;` through the event listeners, roughly lines 1517-1684) with new code:

  ```javascript
  // ========== 手动分组排班（4 标签页） ==========

  let currentGroupingRecruitId = null;
  let groupingData = null;

  async function openGroupingPanel(recruitId, examName) {
      currentGroupingRecruitId = recruitId;
      document.getElementById('groupingModalTitle').textContent = `分组排班 — ${examName}`;
      // 重置所有 tab 内容为加载中
      document.getElementById('tabGeneralContent').innerHTML = '<div class="text-center py-4 text-muted">加载中...</div>';
      document.getElementById('tabSupervisorContent').innerHTML = '<div class="text-center py-4 text-muted">加载中...</div>';
      document.getElementById('tabGroupsContent').innerHTML = '<div class="text-center py-4 text-muted">加载中...</div>';
      document.getElementById('tabClassroomsContent').innerHTML = '<div class="text-center py-4 text-muted">加载中...</div>';

      const modal = bootstrap.Modal.getOrCreateInstance(document.getElementById('groupingModal'));
      modal.show();

      await loadGroupingData(recruitId);
  }

  async function loadGroupingData(recruitId) {
      try {
          const res = await fetch(`/api/recruit/${recruitId}/manual-grouping-data`);
          if (!res.ok) throw new Error('加载失败');
          groupingData = await res.json();
          renderGeneralTab();
          renderSupervisorTab();
          renderGroupsTab();
          renderClassroomsTab();
      } catch (err) {
          document.getElementById('tabGeneralContent').innerHTML = '<div class="text-danger text-center py-4">加载分组数据失败</div>';
      }
  }
  ```

- [ ] **Step 3: Add Tab 1 rendering (general supervisor)**

  ```javascript
  function renderGeneralTab() {
      const container = document.getElementById('tabGeneralContent');
      const data = groupingData;
      const registrations = data.registrations.filter(r => r.id !== data.general_supervisor_id);
      const current = data.registrations.find(r => r.id === data.general_supervisor_id);

      let html = '';
      if (current) {
          html += `<div class="alert alert-success py-2">当前总负责人：<strong>${current.name}</strong>（${current.gender}）<button class="btn btn-sm btn-outline-danger ms-2" onclick="removeGeneralSupervisor()">移除</button></div>`;
      } else {
          html += `<div class="alert alert-warning py-2">⚠️ 尚未设置总负责人，建议指定一位</div>`;
      }

      html += `<div class="mb-2"><strong>选择总负责人（从报名人员中选1人）：</strong></div>`;
      html += `<div class="d-flex flex-wrap gap-2">`;
      registrations.forEach(r => {
          html += `<div class="border rounded p-2 text-center" style="width:140px;background:#f8f9fa">`;
          html += `<div><strong>${r.name}</strong></div>`;
          html += `<div class="small text-muted">${r.gender}${r.has_experience ? ' ⭐有经验' : ''}</div>`;
          html += `<button class="btn btn-sm btn-outline-primary mt-1" onclick="setGeneralSupervisor(${r.id})">设为总负责人</button>`;
          html += `</div>`;
      });
      html += `</div>`;
      container.innerHTML = html;
  }

  async function setGeneralSupervisor(regId) {
      const formData = new FormData();
      formData.append('registration_id', regId);
      const csrf = getCsrf();
      try {
          const res = await fetch(`/api/recruit/${currentGroupingRecruitId}/general-supervisor`, {
              method: 'PUT', body: formData, headers: { 'X-CSRF-Token': csrf }
          });
          const data = await res.json();
          showToast(data.msg, 'success');
          if (data.code === 0) await loadGroupingData(currentGroupingRecruitId);
      } catch (err) { showToast('操作失败', 'danger'); }
  }

  async function removeGeneralSupervisor() {
      await setGeneralSupervisor(null);
  }
  ```

- [ ] **Step 4: Add Tab 2 rendering (building supervisors)**

  This needs to load zones from the classrooms data. Zones are derived from the classrooms in the recruit's classroom selection.

  ```javascript
  function renderSupervisorTab() {
      const container = document.getElementById('tabSupervisorContent');
      const data = groupingData;

      // 从教室数据中提取所有 unique zone
      const zones = [...new Set(data.classrooms.map(c => c.zone).filter(z => z))];

      if (zones.length === 0) {
          container.innerHTML = '<div class="text-muted py-4">暂无楼栋分区数据</div>';
          return;
      }

      // 已有人选供快速选择（排除总负责人）
      const candidates = data.registrations.filter(r => r.id !== data.general_supervisor_id);
      const supervisorMap = {};
      data.supervisors.forEach(s => { supervisorMap[s.zone_name] = s.registration_id; });

      let html = `<div class="alert alert-info py-2">同一人可以负责多栋。跳过此步骤可能会影响验收流程。</div>`;
      html += `<div class="row g-3">`;

      zones.forEach(zone => {
          const currentId = supervisorMap[zone] || '';
          const currentPerson = candidates.find(r => r.id === currentId);
          html += `<div class="col-md-4"><div class="border rounded p-3">`;
          html += `<div class="fw-bold mb-2">${zone}</div>`;
          html += `<select class="form-select form-select-sm zone-supervisor-select" data-zone="${zone}">`;
          html += `<option value="">（不指定）</option>`;
          candidates.forEach(r => {
              const selected = r.id === currentId ? 'selected' : '';
              const label = `${r.name}（${r.gender}${r.has_experience ? '⭐' : ''}）`;
              html += `<option value="${r.id}" ${selected}>${label}</option>`;
          });
          html += `</select>`;
          if (currentPerson) {
              html += `<div class="small text-muted mt-1">当前：${currentPerson.name}</div>`;
          }
          html += `</div></div>`;
      });

      html += `</div>`;
      html += `<button class="btn btn-custom mt-3" onclick="saveBuildingSupervisors()">保存楼栋负责人</button>`;
      container.innerHTML = html;
  }

  async function saveBuildingSupervisors() {
      const selects = document.querySelectorAll('.zone-supervisor-select');
      const supervisors = [];
      selects.forEach(sel => {
          const val = sel.value;
          if (val) {
              supervisors.push({ zone_name: sel.dataset.zone, registration_id: parseInt(val) });
          }
      });

      const csrf = getCsrf();
      try {
          const res = await fetch(`/api/recruit/${currentGroupingRecruitId}/building-supervisors`, {
              method: 'PUT',
              headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
              body: JSON.stringify({ supervisors }),
          });
          const data = await res.json();
          showToast(data.msg, 'success');
          if (data.code === 0) await loadGroupingData(currentGroupingRecruitId);
      } catch (err) { showToast('保存失败', 'danger'); }
  }
  ```

- [ ] **Step 5: Add Tab 3 rendering (drag grouping with SortableJS)**

  ```javascript
  let groupsSortable = null;
  let poolSortable = null;

  function renderGroupsTab() {
      const container = document.getElementById('tabGroupsContent');
      const data = groupingData;

      // 排除总负责人和楼栋负责人
      const excludedIds = new Set();
      if (data.general_supervisor_id) excludedIds.add(data.general_supervisor_id);
      data.supervisors.forEach(s => excludedIds.add(s.registration_id));

      const pool = data.registrations.filter(r => !excludedIds.has(r.id));

      // 已分配到组的人员
      const groupedIds = new Set();
      data.groups.forEach(g => g.member_ids.forEach(mid => groupedIds.add(mid)));

      const ungrouped = pool.filter(r => !groupedIds.has(r.id));

      let html = `<div class="row">`;
      // 待分区
      html += `<div class="col-md-4">`;
      html += `<div class="fw-bold mb-2">待分组人员（${ungrouped.length}人）</div>`;
      html += `<div id="groupPool" class="border rounded p-2" style="min-height:200px">`;
      ungrouped.forEach(r => {
          html += `<div class="border rounded p-2 mb-1 bg-light" data-reg-id="${r.id}" style="cursor:grab">${r.name}（${r.gender}${r.has_experience ? ' ⭐' : ''}）</div>`;
      });
      html += `</div></div>`;

      // 组卡片区
      html += `<div class="col-md-8">`;
      html += `<div class="d-flex justify-content-between align-items-center mb-2">`;
      html += `<span class="fw-bold">已创建的分组</span>`;
      html += `<button class="btn btn-sm btn-outline-primary" onclick="createNewGroup()">+ 新增组</button>`;
      html += `</div>`;
      html += `<div id="groupsArea" class="d-flex flex-wrap gap-2">`;

      data.groups.forEach((g, idx) => {
          const members = g.member_ids.map(mid => {
              const r = data.registrations.find(reg => reg.id === mid);
              return r ? `${r.name}（${r.gender}）` : '未知';
          }).join('、');

          html += `<div class="border rounded p-2" style="width:200px;background:#f8f9fa" data-group-id="${g.id}">`;
          html += `<div class="d-flex justify-content-between"><strong>第${idx+1}组</strong><button class="btn btn-sm btn-outline-danger py-0" onclick="deleteGroup(${g.id}, this)">×</button></div>`;
          html += `<div class="group-members-area mt-1" data-group-id="${g.id}" style="min-height:60px">`;
          g.member_ids.forEach(mid => {
              const r = data.registrations.find(reg => reg.id === mid);
              if (r) {
                  html += `<div class="border rounded p-1 mb-1 bg-white small" data-reg-id="${r.id}" style="cursor:grab">${r.name}（${r.gender}${r.has_experience ? ' ⭐' : ''}）</div>`;
              }
          });
          html += `</div></div>`;
      });

      html += `</div></div></div>`;

      container.innerHTML = html;

      // 初始化 SortableJS
      if (poolSortable) poolSortable.destroy();
      if (groupsSortable) groupsSortable.destroy();

      poolSortable = new Sortable(document.getElementById('groupPool'), {
          group: 'grouping',
          animation: 150,
          sort: false,
      });

      const groupAreas = document.querySelectorAll('.group-members-area');
      groupsSortable = [];
      groupAreas.forEach(area => {
          const s = new Sortable(area, {
              group: 'grouping',
              animation: 150,
              onEnd: function(evt) {
                  // 自动保存
                  saveGroupMembers();
              }
          });
          groupsSortable.push(s);
      });
  }

  async function saveGroupMembers() {
      // 收集所有组的人员分配
      const groupAreas = document.querySelectorAll('.group-members-area');
      const updates = [];
      groupAreas.forEach(area => {
          const groupId = parseInt(area.dataset.groupId);
          const memberIds = [];
          area.querySelectorAll('[data-reg-id]').forEach(el => {
              memberIds.push(parseInt(el.dataset.regId));
          });
          updates.push({ group_id: groupId, member_ids: memberIds });
      });

      const csrf = getCsrf();
      for (const u of updates) {
          try {
              await fetch(`/api/recruit/${currentGroupingRecruitId}/groups/${u.group_id}/members`, {
                  method: 'PUT',
                  headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
                  body: JSON.stringify({ member_ids: u.member_ids }),
              });
          } catch (err) { /* ignore per-group errors */ }
      }
      showToast('分组已保存', 'success');
  }

  async function createNewGroup() {
      const csrf = getCsrf();
      try {
          const res = await fetch(`/api/recruit/${currentGroupingRecruitId}/groups`, {
              method: 'POST', headers: { 'X-CSRF-Token': csrf }
          });
          const data = await res.json();
          if (data.code === 0) await loadGroupingData(currentGroupingRecruitId);
      } catch (err) { showToast('创建失败', 'danger'); }
  }

  async function deleteGroup(groupId, btn) {
      if (!await showConfirm('确认删除', '确定删除此组？')) return;
      const csrf = getCsrf();
      try {
          const res = await fetch(`/api/recruit/${currentGroupingRecruitId}/groups/${groupId}`, {
              method: 'DELETE', headers: { 'X-CSRF-Token': csrf }
          });
          const data = await res.json();
          showToast(data.msg, data.code === 0 ? 'success' : 'danger');
          if (data.code === 0) await loadGroupingData(currentGroupingRecruitId);
      } catch (err) { showToast('删除失败', 'danger'); }
  }
  ```

- [ ] **Step 6: Add Tab 4 rendering (classroom assignment)**

  ```javascript
  function renderClassroomsTab() {
      const container = document.getElementById('tabClassroomsContent');
      const data = groupingData;

      if (!data.groups || data.groups.length === 0) {
          container.innerHTML = '<div class="text-muted py-4">暂无分组，请先在「拖拽分组」中创建组</div>';
          return;
      }
      if (data.classrooms.length === 0) {
          container.innerHTML = '<div class="text-muted py-4">暂无考场数据</div>';
          return;
      }

      // 按 zone 分组教室
      const zoneClassrooms = {};
      data.classrooms.forEach(c => {
          if (!zoneClassrooms[c.zone]) zoneClassrooms[c.zone] = [];
          zoneClassrooms[c.zone].push(c);
      });

      let html = '';
      data.groups.forEach((g, idx) => {
          const members = g.member_ids.map(mid => {
              const r = data.registrations.find(reg => reg.id === mid);
              return r ? r.name : '未知';
          }).join('、');

          const assignedIds = new Set(g.classroom_rc_ids);
          let groupExamCount = 0;
          data.classrooms.forEach(c => { if (assignedIds.has(c.rc_id)) groupExamCount += c.exam_count; });

          // 收集所有已分配的教室ID（互斥用）
          const takenIds = new Set();
          data.groups.forEach(og => {
              if (og.id !== g.id) og.classroom_rc_ids.forEach(rid => takenIds.add(rid));
          });

          html += `<div class="border rounded p-3 mb-3">`;
          html += `<div class="fw-bold mb-2">第${idx+1}组：${members}</div>`;

          // 按 zone 展示教室
          for (const [zone, classrooms] of Object.entries(zoneClassrooms)) {
              html += `<div class="ms-2 mb-1"><span class="text-muted small">${zone}</span>`;
              html += `<div class="d-flex flex-wrap gap-1 mt-1">`;
              classrooms.forEach(c => {
                  const checked = assignedIds.has(c.rc_id) ? 'checked' : '';
                  const disabled = takenIds.has(c.rc_id) ? 'disabled' : '';
                  html += `<div class="border rounded p-1 text-center ${disabled ? 'bg-light text-muted' : 'bg-white'}" style="width:100px">`;
                  html += `<div class="form-check mb-0 justify-content-center">`;
                  html += `<input type="checkbox" class="form-check-input group-classroom-cb" data-group-id="${g.id}" data-rc-id="${c.rc_id}" ${checked} ${disabled}>`;
                  html += `<label class="form-check-label small">${c.name}</label></div>`;
                  html += `<div class="small text-muted">${c.exam_count}考场</div></div>`;
              });
              html += `</div></div>`;
          }

          const totalExam = data.classrooms.reduce((sum, c) => sum + (assignedIds.has(c.rc_id) ? c.exam_count : 0), 0);
          html += `<div class="small text-muted mt-1">已分配 ${assignedIds.size} 间教室 → ${totalExam} 个考场</div>`;
          html += `</div>`;
      });

      html += `<button class="btn btn-custom" onclick="saveAllClassroomAssignments()">保存教室分配</button>`;
      container.innerHTML = html;
  }

  async function saveAllClassroomAssignments() {
      const csrf = getCsrf();
      const groupMap = {};
      document.querySelectorAll('.group-classroom-cb:checked').forEach(cb => {
          const gid = parseInt(cb.dataset.groupId);
          if (!groupMap[gid]) groupMap[gid] = [];
          groupMap[gid].push(parseInt(cb.dataset.rcId));
      });

      for (const [gid, rcIds] of Object.entries(groupMap)) {
          try {
              await fetch(`/api/recruit/${currentGroupingRecruitId}/groups/${gid}/classrooms`, {
                  method: 'PUT',
                  headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
                  body: JSON.stringify({ rc_ids: rcIds }),
              });
          } catch (err) { /* handle per-group */ }
      }
      showToast('教室分配已保存', 'success');
  }
  ```

- [ ] **Step 7: Add finalize handler and utility**

  ```javascript
  document.getElementById('finalizeGroupBtn')?.addEventListener('click', async () => {
      if (!currentGroupingRecruitId) return;
      if (!await showConfirm('确认分组', '确定完成分组？这将创建布置清单和验收记录。')) return;
      const csrf = getCsrf();
      try {
          const res = await fetch(`/api/recruit/${currentGroupingRecruitId}/finalize-grouping`, {
              method: 'POST', headers: { 'X-CSRF-Token': csrf }
          });
          const data = await res.json();
          showToast(data.msg || data.detail, data.code === 0 ? 'success' : 'danger');
          if (data.code === 0) bootstrap.Modal.getInstance(document.getElementById('groupingModal')).hide();
      } catch (err) { showToast('操作失败', 'danger'); }
  });

  // Utility
  function getCsrf() {
      return document.cookie.split('; ').find(row => row.startsWith('kaowu_csrf='))?.split('=')[1] || '';
  }
  ```

- [ ] **Step 8: Clean up — remove old grouping event listeners**

  Remove these lines that no longer apply:
  ```javascript
  document.getElementById('autoGroupBtn')?.addEventListener('click', triggerAutoGroup);
  document.getElementById('saveGroupBtn')?.addEventListener('click', saveGroupManually);
  ```

  Also remove the old grouping functions: `triggerAutoGroup()`, `saveGroupManually()`, `renderGrouping()`.

- [ ] **Step 9: Verify JS syntax**

  ```bash
  node -e "
  const fs = require('fs');
  const html = fs.readFileSync('app/static/admin.html', 'utf8');
  const match = html.match(/<script>([\s\S]*?)<\/script>/);
  if (match) { try { new Function(match[1]); console.log('JS OK'); } catch(e) { console.log('JS Error:', e); } }
  "
  ```

- [ ] **Step 10: Commit**

  ```bash
  git add app/static/admin.html
  git commit -m "feat: rewrite grouping panel with 4-tab manual wizard"
  ```
