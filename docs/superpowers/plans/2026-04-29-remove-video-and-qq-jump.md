# Remove Video & Add QQ Jump Link — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove unused video watching feature and add one-click QQ group joining link with fallback.

**Architecture:** Backend cleanup (main.py: remove VideoWatch model and 3 endpoints) + frontend cleanup (student.html: remove video players and JS) + admin cleanup (admin.html: remove video column) + file cleanup + QQ jump link enhancement (student.html: tencent:// protocol link with group number fallback).

**Tech Stack:** FastAPI, SQLAlchemy, SQLite, Bootstrap 5, Jinja2

---

### Task 1: Backend — Remove Video Model and Endpoints

**Files:**
- Modify: `app/main.py`

- [ ] **Step 1: Remove VIDEO_URL config and VideoWatch model**

In `app/main.py`:
- Delete line `VIDEO_URL = os.getenv("KAOWU_VIDEO_URL", "")` (~line 47-48)
- Delete the entire `VideoWatch` model class (~lines 114-120, and the empty line before it)

- [ ] **Step 2: Remove video references from view_registrations**

In `app/main.py`, find the `view_registrations` endpoint (~line 429-442):
- Remove the `watched_students` query (querying `VideoWatch.student_id`)
- Remove the `video_watched` field from each registration dict in the response

- [ ] **Step 3: Delete video-related endpoints**

Delete these 3 endpoints from `app/main.py`:
- `GET /api/video-url` (~lines 446-449)
- `POST /api/mark-video-watched` (~lines 451-473)
- `POST /api/video-watch-status` (~lines 475-494)

- [ ] **Step 4: Remove video_url from registration response**

In the `POST /api/reg` endpoint (~line 566), remove `"video_url": VIDEO_URL` from the success response dict.

### Task 2: Frontend — Remove Video HTML and JS

**Files:**
- Modify: `app/static/student.html`

- [ ] **Step 1: Remove the main page video section**

Delete the "教室布置要求视频" card (currently lines ~74-99), which contains:
- The card header and body with `<video id="pageVideoPlayer">`
- The `videoWatchForm` div (inputs: student_id, name, phone + confirm button)
- The `videoWatchResult` div

- [ ] **Step 2: Remove the modal video section**

In the `qqGroupModal` (~lines 140-171), remove:
- The `modalVideoSection` div (contains `modalVideoPlayer`, `modalConfirmWatchedBtn`, `modalWatchedBadge`)

- [ ] **Step 3: Remove video-related JS code**

Delete these JS blocks:
- `showQQGroupModal` function's videoUrl parameter handling (conditionally showing modalVideoSection, line ~193-218)
- `modalConfirmWatchedBtn` click handler (~lines 226-253)
- The `hidden.bs.modal` event handler for reseting video state (~lines 257-263)
- `pageConfirmWatchedBtn` click handler (~lines 266-297)
- `loadVideoUrl` function (~lines 299-308)
- `loadVideoUrl()` call in DOMContentLoaded (~line 686)

- [ ] **Step 4: Remove "观看视频" button from registration records**

In the registration records rendering code (~lines 539-549), remove the "观看视频" button element.

### Task 3: Admin — Remove Video Column

**Files:**
- Modify: `app/static/admin.html`

- [ ] **Step 1: Remove "已观视频" header and data**

- Remove the header cell "已观视频" from the registrations table (~line 138)
- Remove the `r.video_watched` data display for each row (~lines 573-581)

### Task 4: File Cleanup

- [ ] **Step 1: Remove video files and config**

- Delete `app/static/video/exam-room-setup.mp4`
- Delete `app/static/video/.gitkeep`
- Remove video volume mount from `docker-compose.yml` (`./app/static/video:/app/static/video`)
- Remove `KAOWU_VIDEO_URL` line from `.env`

### Task 5: Add QQ Group Jump Link

**Files:**
- Modify: `app/static/student.html`

- [ ] **Step 1: Update registration success modal with jump link**

In the success modal (`qqGroupModal`), change the QQ group display:
- Replace the current green box that shows "请加入考务工作QQ群" + group number with:
  - A link/button: `<a href="tencent://groupwpa?subcmd=RequestJoinGroup&group={群号}" target="_blank" class="btn btn-success btn-lg">一键加入QQ群</a>`
  - Below it in smaller text: `群号：{群号}（如无法跳转请手动复制）`
- Update `showQQGroupModal(qqGroup)` to render this new layout (remove videoUrl parameter)

- [ ] **Step 2: Update "我的报名" records with jump link**

In the records rendering (~lines 521-537), replace `📢 请加群：{item.qq_group}` with:
- A link: `<a href="tencent://groupwpa?subcmd=RequestJoinGroup&group={item.qq_group}" target="_blank">一键加群</a>`
- Followed by: `群号：{item.qq_group}`

### Task 6: Verify

- [ ] **Step 1: Start dev server and test**

```bash
cd /Users/wff/KAIfa/kaowu-system/app && uvicorn main:app --reload --port 8000
```

Verify:
1. Landing page loads without errors
2. Student portal loads, can see active recruitments
3. Register for a recruitment → modal shows QQ jump link (not video), group number displayed
4. "My registrations" shows QQ jump link with group number
5. Admin panel loads, registration list no longer has "已观视频" column
6. No video-related 404 errors in console
