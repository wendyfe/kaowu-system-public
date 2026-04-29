# Remove Video Feature & Add QQ Group Jump Link

## Overview

Two changes to the kaowu-system:
1. Remove the unused video watching feature (backend + frontend)
2. Enhance the QQ group joining experience: show a `tencent://` protocol jump link with group number as fallback

## Part 1: Remove Video Feature

### Backend (app/main.py)

- Delete `VIDEO_URL` config (line ~47-48)
- Delete `VideoWatch` model (line ~114-120)
- Delete `GET /api/video-url` endpoint (line ~446-449)
- Delete `POST /api/mark-video-watched` endpoint (line ~451-473)
- Delete `POST /api/video-watch-status` endpoint (line ~475-494)
- Remove `video_url` field from registration response (line ~566)
- Remove `watched_students` query and `video_watched` field from `view_registrations` (line ~429-442)

### Frontend (app/static/student.html)

- Delete the "教室布置要求视频" card section (lines ~74-99, `#pageVideoPlayer`, `#videoWatchForm`)
- Delete video player from the modal (`#modalVideoSection`, `#modalVideoPlayer`, `#modalConfirmWatchedBtn`, `#modalWatchedBadge`)
- Delete all video-related JS:
  - `showQQGroupModal` videoUrl parameter handling (lines ~193-218)
  - `modalConfirmWatchedBtn` click handler (lines ~226-253)
  - `pageConfirmWatchedBtn` click handler (lines ~266-297)
  - `loadVideoUrl` function (lines ~299-308)
  - `loadVideoUrl()` call in DOMContentLoaded (line ~686)
- Delete "观看视频" button from registration records list (lines ~539-549)

### Admin (app/static/admin.html)

- Remove "已观视频" column from table header (line ~138)
- Remove `video_watched` data display for each row (lines ~573-581)

### Files to clean up

- Delete `app/static/video/` directory and contents (exam-room-setup.mp4, .gitkeep)
- Remove `KAOWU_VIDEO_URL` from `.env`
- Remove video volume mount from `docker-compose.yml`

## Part 2: QQ Group Jump Link

### Mechanism

Use `tencent://groupwpa?subcmd=RequestJoinGroup&group={qq_group}` as the primary jump link. This is the official QQ protocol — opens QQ directly with a "request to join group" dialog. Works on desktop and mobile where QQ is installed.

Fallback: group number text is always displayed below the jump button, so users can manually copy it if QQ is not installed or the protocol fails.

### Changes

**Registration success modal (student.html):**
- Replace the current green "请加入考务工作QQ群" box (with just text) with:
  - A prominent "一键加群" link/button: `<a href="tencent://groupwpa?subcmd=RequestJoinGroup&group={群号}" target="_blank">一键加群</a>`
  - Below it, show the group number in smaller text as fallback: `群号：{群号}（如无法跳转请手动复制）`

**"我的报名" records list (student.html):**
- Replace `📢 请加群：{item.qq_group}` with a similar jump link + fallback text

### No backend changes needed for this part — qq_group is already returned by the API.
