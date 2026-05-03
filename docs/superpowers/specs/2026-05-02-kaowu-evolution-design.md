# 考务布置全流程管理系统 — 设计文档

## 概述

将现有考务报名系统从"招募工作人员报名"扩展为覆盖"招募→分组→布置→验收→恢复"全流程的管理平台。核心业务流：

```
发布招募 → 报名 → 分组排班 → 布置清单核验 → 检查验收 → 考后恢复
```

## 1. 数据模型

### 1.1 永久基础数据

**buildings（教学楼）**

| 字段 | 类型 | 说明 |
|------|------|------|
| id | int PK | |
| name | string | 如"树人楼""综合楼" |

**classrooms（教室）**

| 字段 | 类型 | 说明 |
|------|------|------|
| id | int PK | |
| building_id | int FK | 所属教学楼 |
| name | string | 如 B101、102 |
| is_fixed_seats | bool | 是否固定桌椅（Fixed: 好布置，可多分） |
| can_double_exam | bool | 是否具备双考场条件（物理空间够用） |
| is_enabled | bool | 是否启用为可用考场（禁用后不在考场选择器中显示） |

**栋（zone）— 从教室名称自动推导，不存表**

- 教室名包含英文字母 → 取**第一个英文字母**作为所属栋（B101 → B栋，A201 → A栋）
- 教室名无英文字母（纯数字或中文开头"综101"）→ 无栋分区，直接挂教学楼下面

### 1.2 考试动态数据

**recruitments（招募）— 已有的 Recruitment 表，新增关联字段**

| 新增字段 | 说明 |
|---------|------|
| general_supervisor_id | int \| null，关联 registration.id，总负责人（可选，传 null 移除）|
| has_floor_supervisors | bool，是否需要楼栋负责人（大考/小考自适应，发布时配置） |

**recruitment_classrooms（招募-教室关联）**

| 字段 | 类型 | 说明 |
|------|------|------|
| id | int PK | |
| recruitment_id | int FK | 关联招募 |
| classroom_id | int FK | 关联教室 |
| exam_mode | enum | single / double（本次考试实际用单还是双考场） |
| exam_number_start | int | 该教室考场起始号（发布时自动编排） |

考场号生成规则：按选择顺序从1开始连续编号，双考场自动占两个号。

**recruitment_groups（分组）**

| 字段 | 类型 | 说明 |
|------|------|------|
| id | int PK | |
| recruitment_id | int FK | |
| zone_name | string | 所属栋（如"B栋"） |
| is_supervisor | bool | 是否为楼栋负责人（每组一人，有经验者优先推荐） |

**recruitment_group_members（组成员）**

| 字段 | 类型 | 说明 |
|------|------|------|
| id | int PK | |
| group_id | int FK | |
| registration_id | int FK | 关联报名记录 |

> 约束：UNIQUE (group_id, registration_id)，防止同一成员重复入组。

**recruitment_group_classrooms（组-教室分配）**

| 字段 | 类型 | 说明 |
|------|------|------|
| id | int PK | |
| group_id | int FK | |
| recruitment_classroom_id | int FK | 关联的招募教室 |

**building_supervisors（楼栋负责人）**

| 字段 | 类型 | 说明 |
|------|------|------|
| id | int PK | |
| recruitment_id | int FK | |
| zone_name | string | 负责人负责的栋（如"B栋"）|
| registration_id | int FK | 关联报名记录，一人可对应多条（负责多栋） |

**task_checklist_items（布置清单模板项）**

| 字段 | 类型 | 说明 |
|------|------|------|
| id | int PK | |
| name | string | 项名称 |
| sort_order | int | 排序 |
| is_auto_skip_for_fixed | bool | 固定桌椅教室自动跳过（如清点桌椅） |

**classroom_task_progress（教室布置/恢复进度）**

| 字段 | 类型 | 说明 |
|------|------|------|
| id | int PK | |
| recruitment_classroom_id | int FK | |
| task_type | enum | setup / recovery |
| item_id | int FK | 关联清单项 |
| is_completed | bool | |
| completed_by | int | 谁完成的（关联报名记录） |
| completed_at | datetime | |

**acceptance_records（验收记录）**

| 字段 | 类型 | 说明 |
|------|------|------|
| id | int PK | |
| recruitment_classroom_id | int FK | |
| acceptor_type | enum | floor_supervisor / admin |
| acceptor_id | int | 验收人（管理员ID或报名记录ID） |
| status | enum | pending / inspecting / rejected / passed / sealed |
| note | text | 验收意见/返工原因 |
| created_at | datetime | |
| updated_at | datetime | |

## 2. 模块设计

### 2.1 考场/教室信息管理

**导入模板（Excel）：**

| 教学楼 | 教室名称 | 是否固定桌椅 | 是否具备双考场条件 |
|--------|---------|-------------|-----------------|
| 树人楼 | B101 | 否 | 是 |
| 树人楼 | A201 | 否 | 否 |
| 综合楼 | 101 | 是 | 是 |

**维护：**
- 页面 CRUD 增删改
- 按教学楼筛选查看
- 编辑时可修改所有字段

### 2.2 发布招募 — 选择考场

发布表单新增"选择考场场地"区块，采用**树形选择器**交互：

**树形结构（三级）：教学楼 → 栋 → 教室**

```
树人楼                     ← 教学楼（展开/收起按钮）
  ├─ ☑ B栋                ← 栋 checkbox = 全选/全取消该栋所有教室
  │   ├─ ☑ B101 │ 单考场 ▾
  │   ├─ ☑ B102 │ 双考场 ▾
  │   └─ ☑ B103 │ 单考场 ▾
  └─ ☐ A栋
      └─ ☐ A201 │ 单考场 ▾

综合楼                     ← 教学楼（无栋分区，整楼展示）
  └─ ☐ 综101 │ 双考场 ▾
      ☐ 综102 │ 单考场 ▾
```

**栋的推导规则：**
- 教室名含英文字母 → 取**第一个英文字母**作为栋（B101 → B栋，A201 → A栋）
- 教室名无英文字母 → 无栋分区，直接挂在教学楼下面

**勾选行为：**

| 层级 | 行为 | 说明 |
|------|------|------|
| 教学楼 | 展开/收起子节点 | 纯折叠，不做全选 |
| 栋 | checkbox 全选/全取消该栋所有教室 | 快速操作入口 |
| 教室 | checkbox 单独选中/取消 | 允许微调 |

**考场模式下拉：**
- `can_double_exam = false` 的教室 → 下拉固定"单考场"，不可更改
- `can_double_exam = true` 的教室 → 默认选"双考场"，可改为"单考场"

**底部统计：** 实时更新"已选 X 间教室 → 共 Y 个考场"（双考场计2）

**其他：**
- 发布时自动编排考场号（按选择顺序从1开始）
- 新增配置：是否需要楼栋负责人（控制验收层级）

### 2.3 分组排班（手动流程）

招募关闭后，管理员打开分组面板，按 4 步操作：

**Step 1 — 选总负责人（1人）**
- 从报名列表中单选 1 人设为总负责人
- 可选，跳过有提示"建议指定总负责人"

**Step 2 — 选楼栋负责人（每栋1人，一人可兼多栋）**
- 按栋展示（B栋、A栋、D栋...），每栋从报名列表中选 1 人
- 同一人可以负责多栋（选择时展示已选过的候选人）
- 可选，跳过有提示

**Step 3 — 拖拽分组**
- 总负责人和楼栋负责人自动排除，不参与分组
- 左侧待分区（剩余报名人员）→ 右侧组卡片区，SortableJS 拖拽
- 支持：从待分区拖入组、组间拖拽交换、组内拖出回待分区
- 管理员可"新增空组"

**Step 4 — 分配教室**
- 分组完成后，管理员为每组勾选负责的教室
- 每间教室只能分配给一个组（已分配的教室对其他组置灰不可选）
- 每行显示：教室名 + 考场数（单=1 双=2）
- 底部显示每组统计：已分配 X 间教室 → 共 Y 个考场

**分组面板 UI（4 标签页）：**

```
┌─ 分组排班 ───────────────────────────────────┐
│ [①总负责人] [②楼栋负责人] [③拖拽分组] [④分配教室] │
│                                               │
│ （当前 tab 内容）                              │
│                                               │
│            [上一步]               [下一步]      │
└───────────────────────────────────────────────┘
```

**保存时：** 创建分组记录、任务清单、验收记录（与原来自动分组后相同）。

### 2.4 布置清单

**标准清单项（一次配置可复用）：**

1. 张贴门帖（核对门牌号）
2. 设置禁带物品放置处（搬椅子+贴标识）
3. 清点桌椅/补齐缺额（固定桌椅自动跳过）
4. 环境清理（黑板、窗帘、课桌内部）
5. 核对时钟
6. 检查广播声音（听够3分钟）
7. 张贴座位号
8. 自查

**学生端：** 在手机上看到被分配的教室，逐项打勾。全部完成后点击"提交验收"。

**进度实时同步：** 楼栋负责人/总负责人可查看各教室完成进度。

### 2.5 检查验收

**自适应层级（发布时配置）：**

| 配置 | 验收流 |
|------|--------|
| 需要楼栋负责人 | 自检→交叉检→楼栋验收→总负责人确认→封门 |
| 仅总负责人 | 自检→交叉检→总负责人验收→封门 |

**状态流转：**

布置中 → 提交验收 → 待验收 →（楼栋/总负责人检查）→ 需返工（退回） / 通过 → 封门

验收时对照检查标准逐一确认，不通过需填写原因。

**总负责人面板：** 按楼栋展示所有教室的验收状态，全部通过后可一键封门。

### 2.6 考后恢复

考试结束后，管理员切换状态到"恢复阶段"，系统将恢复任务自动分配回原布置小组。

**恢复清单：**
1. 椅子搬回室内
2. 撕除门帖（不留痕迹）
3. 撕除座位贴
4. 撕除禁带物品标识
5. 清理胶带残留

进度跟踪与布置阶段一致。可配置超时提醒（考试结束后 X 小时未完成）。

## 3. 现有代码改动

### main.py
- 新增数据模型：Building, Classroom, RecruitmentClassroom, RecruitmentGroup, RecruitmentGroupMember, RecruitmentGroupClassroom, BuildingSupervisor, TaskProgress (原 ClassroomTaskProgress), AcceptanceRecord
- 新增 API：教室管理 CRUD、分组排班、清单进度、验收、恢复
- 现有 Recruitment 表新增字段（general_supervisor_id, has_floor_supervisors）
- 现有 Registration 表新增性别字段
- 现有 Classroom 表新增 is_enabled 字段
- **删除自动分组算法**：auto_assign_groups() 和 save_groups_to_db() 不再使用
- **新增手动分组 API**：设置总负责人、设置楼栋负责人、创建/删除组、拖拽成员、分配教室
- **新增 BuildingSupervisor 模型**：独立于分组表存储楼栋负责人，验收面板直接读此表
- **RecruitmentGroupMember 唯一约束**：(group_id, registration_id) 防重复入组
- **级联删除**：删除招募时按序清理所有关联表（验收记录→任务进度→分组→教室配置→楼栋负责人→报名记录→招募）
- **`import json` 统一在文件顶部**，不再散落在各路由函数体内
- **set_group_members API 去重**：插入前用 set 过滤重复 registration_id
- **set_general_supervisor API**：registration_id 改为 `int | None`，支持传 null 移除
- **finalize_grouping**：增加教室分配检查，每组必须至少分配一间教室
- **update_classroom**：增加 `is_enabled` 参数

### admin.html
- 新增"教室管理"页面
- 发布招募表单新增"选择考场"区块 + "验收配置"区块（has_floor_supervisors 勾选）
- **分组排班面板重写**：改为 4 标签页手动流程（总负责人→楼栋负责人→拖拽分组→分配教室）
- 新增"验收总览"面板
- 编辑教室弹窗增加"启用为可用考场"开关（is_enabled）
- 编辑侧滑面板增加验收配置区块（has_floor_supervisors）
- **saveAllGroupMembers**：改为 Promise.all 等待所有请求 + 跨组重复检测 + 失败提示
- **saveAllClassroomAssignments**：对所有组（含零勾选）发送 PUT 清理旧分配
- **renderGeneralTab**：改为下拉选择（与楼栋负责人一致），选中 `（不指定）` 即可取消
- **总负责人/楼栋负责人互斥**：前后端双重保障，设置总负责人时自动从楼栋负责人中移除
- **renderSupervisorTab**：自动从考场推导楼栋分区，展示每个分区的教室列表 + 空值保护
- **统一 CSRF 提取**：所有内联 cookie 解析替换为 getCsrf() 函数
- **HTML 结构修正**：清除重复 closing div
- **招募列表移除导出按钮**（查看报名弹窗内已有导出入口）
- **confirm modal z-index 修正**：确保确认对话框出现在分组弹窗之上

### student.html
- 报名表单新增性别字段
- 新增"我的分组"视图
- 新增"布置清单"打勾界面
- 新增"恢复任务"视图

### 新增文件
- 无。所有逻辑保持单文件后端（main.py），前端通过 admin.html / student.html 扩展。

**备注：** 分组拖拽交互可能需要引入简单的拖拽库（如 SortableJS CDN）来支持教室/人员拖拽调整。

## 4. 未涵盖范围

- 成绩管理（超出考场布置范畴）
- 支付集成（本系统不涉及费用）
- 考生报名（与考场布置人员是两个不同群体）
- 座位编排算法（座位号具体贴在哪个位置由现场人员按规则执行，系统只记录考场数量）
