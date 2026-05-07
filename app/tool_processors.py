import math
import os
import random
import re
import zipfile
from io import BytesIO

import openpyxl
import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font
from openpyxl.utils.dataframe import dataframe_to_rows


WEIGHT_GAP = 1.0
WEIGHT_JIA_OLD = 1.5

GRADUATE_ID_COL = "身份证号码"
GRADUATE_MAJOR_COL = "专业"
GRADUATE_LEVEL_COL = "培养层次"
SCORE_ID_COL = "ks_sfz"
SCORE_VALUE_COL = "zf"
PASS_SCORE = 425
TARGET_LEVELS = ["本科", "专科"]
EXCLUDE_MAJORS = [
    "舞蹈学(本)", "音乐学(本)", "音乐表演(本)",
    "社会体育指导与管理(本)", "体育教育(本)",
    "公共艺术(本)", "环境设计(本)", "美术学(本)", "英语(本)",
]


def _require_columns(df: pd.DataFrame, required: set[str], label: str):
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"{label} 缺少必要列：{', '.join(sorted(missing))}")


def _safe_sheet_name(name: str) -> str:
    cleaned = re.sub(r"[\[\]:*?/\\]", "_", name).strip()
    return (cleaned or "Sheet")[:31]


def get_classroom_name(room_name: str) -> str:
    if "-" in str(room_name):
        return str(room_name).split("-")[0]
    return str(room_name)


def assign_invigilators(teachers_bytes: bytes, rooms_bytes: bytes, room_count: int) -> BytesIO:
    teachers_df = pd.read_excel(BytesIO(teachers_bytes))
    rooms_df = pd.read_excel(BytesIO(rooms_bytes))
    _require_columns(teachers_df, {"id", "name", "gender", "college"}, "监考员表")
    _require_columns(rooms_df, {"room_no", "room_name"}, "考场表")

    if room_count < 1:
        raise ValueError("考场数量必须大于 0")
    if room_count > len(rooms_df):
        raise ValueError(f"考场数量不能超过考场表总数 {len(rooms_df)}")

    teachers = []
    for _, row in teachers_df.iterrows():
        teachers.append({
            "id": int(row["id"]),
            "name": str(row["name"]).strip(),
            "gender": str(row["gender"]).strip(),
            "college": str(row["college"]).strip(),
        })

    teachers_sorted = sorted(teachers, key=lambda x: x["id"])
    total = len(teachers_sorted)
    for idx, teacher in enumerate(teachers_sorted):
        teacher["seniority_rank"] = total - idx

    rooms = rooms_df.head(room_count).to_dict("records")
    female_teachers = [t for t in teachers if t["gender"] == "女"]
    if len(female_teachers) < len(rooms):
        raise ValueError("女性监考人数不足，无法满足所有考场乙位要求")
    if len(teachers) < len(rooms) * 2:
        raise ValueError("监考员总人数不足，无法满足每个考场两名监考员要求")

    random.shuffle(female_teachers)
    yi_assignments = {}
    used_teacher_ids = set()
    for room, yi in zip(rooms, female_teachers):
        yi_assignments[room["room_no"]] = yi
        used_teacher_ids.add(yi["id"])

    classroom_has_male = {get_classroom_name(r["room_name"]): False for r in rooms}
    remaining_teachers = [t for t in teachers if t["id"] not in used_teacher_ids]
    arrangements = []
    failures = []

    for room in rooms:
        room_no = room["room_no"]
        room_name = room["room_name"]
        classroom = get_classroom_name(room_name)
        yi = yi_assignments[room_no]

        best_jia = None
        best_score = -1
        for candidate in remaining_teachers:
            if candidate["college"] == yi["college"]:
                continue
            if not classroom_has_male[classroom] and candidate["gender"] != "男":
                continue
            gap = abs(candidate["id"] - yi["id"])
            score = WEIGHT_GAP * gap + WEIGHT_JIA_OLD * candidate["seniority_rank"]
            if score > best_score:
                best_score = score
                best_jia = candidate

        if best_jia is None:
            failures.append(str(room_name))
            continue

        remaining_teachers.remove(best_jia)
        if best_jia["gender"] == "男":
            classroom_has_male[classroom] = True

        arrangements.append({
            "考场号": room_no,
            "考场名称": room_name,
            "监考员甲": f"{best_jia['name']}({best_jia['id']},{best_jia['gender']})",
            "监考员乙": f"{yi['name']}({yi['id']},{yi['gender']})",
        })

    if failures:
        raise ValueError(f"以下考场无法完成分配：{', '.join(failures)}")

    output = BytesIO()
    pd.DataFrame(arrangements).to_excel(output, index=False)
    output.seek(0)
    return output


def generate_seat_labels_pdf(num_rooms: int, num_seats: int = 30, cols: int = 3, rows: int = 10, font_size: int = 40) -> BytesIO:
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import mm
        from reportlab.pdfgen import canvas
    except ImportError as exc:
        raise RuntimeError("缺少 reportlab 依赖，请先安装 requirements.txt") from exc

    if num_rooms < 1 or num_seats < 1:
        raise ValueError("考场数量和座位数必须大于 0")
    if cols < 1 or rows < 1:
        raise ValueError("列数和行数必须大于 0")

    page_width, page_height = A4
    left_margin = right_margin = top_margin = bottom_margin = 10 * mm
    h_spacing = v_spacing = 2 * mm
    usable_width = page_width - left_margin - right_margin - h_spacing * (cols - 1)
    usable_height = page_height - top_margin - bottom_margin - v_spacing * (rows - 1)
    label_width = usable_width / cols
    label_height = usable_height / rows
    rooms_per_page = cols * rows

    output = BytesIO()
    pdf = canvas.Canvas(output, pagesize=A4)

    for seat in range(1, num_seats + 1):
        pages_needed = math.ceil(num_rooms / rooms_per_page)
        for page in range(pages_needed):
            start_room = page * rooms_per_page + 1
            rooms_this_page = min(num_rooms - page * rooms_per_page, rooms_per_page)
            for col in range(cols):
                for row in range(rows):
                    idx = row + col * rows
                    if idx >= rooms_this_page:
                        continue
                    room = start_room + idx
                    x = left_margin + col * (label_width + h_spacing)
                    y = page_height - top_margin - (row + 1) * label_height - row * v_spacing
                    pdf.setLineWidth(0.8)
                    pdf.setStrokeColor(colors.black)
                    pdf.rect(x, y, label_width, label_height)
                    pdf.setFont("Helvetica", font_size)
                    pdf.drawCentredString(
                        x + label_width / 2,
                        y + label_height / 2 - font_size / 2.8,
                        f"{room}-{seat:02d}",
                    )
            pdf.showPage()

    pdf.save()
    output.seek(0)
    return output


def merge_excel_sheets(file_bytes: bytes) -> BytesIO:
    wb = openpyxl.load_workbook(BytesIO(file_bytes), data_only=True)
    merged_sheet_name = "合并数据"
    if merged_sheet_name in wb.sheetnames:
        del wb[merged_sheet_name]

    merged_sheet = wb.create_sheet(title=merged_sheet_name)
    header_data = []
    row_num = 1

    for sheet_name in list(wb.sheetnames):
        if sheet_name == merged_sheet_name:
            continue
        sheet = wb[sheet_name]
        if sheet.max_row < 1:
            continue

        for row in range(1, sheet.max_row + 1):
            current_row_data = [
                sheet.cell(row=row, column=col).value if sheet.cell(row=row, column=col).value is not None else ""
                for col in range(1, sheet.max_column + 1)
            ]
            if row == 1 and not header_data:
                header_data = current_row_data
                for col, value in enumerate(current_row_data, start=1):
                    merged_sheet.cell(row=row_num, column=col, value=value)
                row_num += 1
                continue
            if current_row_data == header_data:
                continue
            if all(str(cell).strip() == "" for cell in current_row_data):
                continue
            for col, value in enumerate(current_row_data, start=1):
                merged_sheet.cell(row=row_num, column=col, value=value)
            row_num += 1

    sheet_names = wb.sheetnames
    sheet_names.remove(merged_sheet_name)
    sheet_names.insert(0, merged_sheet_name)
    wb._sheets = [wb[name] for name in sheet_names]

    output = BytesIO()
    wb.save(output)
    wb.close()
    output.seek(0)
    return output


def extract_grade_from_filename(filename: str) -> tuple[str, int]:
    match = re.search(r"(\d{4})届", filename)
    if match:
        grade_year = int(match.group(1))
        return f"{grade_year}届", grade_year
    return os.path.splitext(filename)[0], 9999


def read_dbf_files(file_paths: list[str]) -> pd.DataFrame:
    try:
        from dbfread import DBF
    except ImportError as exc:
        raise RuntimeError("缺少 dbfread 依赖，请先安装 requirements.txt") from exc

    all_dbf_data = []
    for file_path in file_paths:
        dbf_table = DBF(file_path, encoding="gbk")
        all_dbf_data.append(pd.DataFrame(iter(dbf_table)))
    if not all_dbf_data:
        raise ValueError("未找到 DBF 成绩文件")
    return pd.concat(all_dbf_data, ignore_index=True).drop_duplicates()


def clean_score_data(score_df: pd.DataFrame, graduate_id_list: list) -> set:
    _require_columns(score_df, {SCORE_ID_COL, SCORE_VALUE_COL}, "成绩表")
    cleaned = score_df.copy()
    cleaned[SCORE_VALUE_COL] = pd.to_numeric(cleaned[SCORE_VALUE_COL], errors="coerce")
    valid_score_df = cleaned[
        (cleaned[SCORE_VALUE_COL].notna()) & (cleaned[SCORE_VALUE_COL] >= PASS_SCORE)
    ].copy()
    valid_score_df = valid_score_df[valid_score_df[SCORE_ID_COL].isin(graduate_id_list)]
    return set(valid_score_df[SCORE_ID_COL].drop_duplicates().tolist())


def calculate_single_level_detail(graduate_df: pd.DataFrame, pass_ids: set, exam_type: str, level: str, grade: str) -> pd.DataFrame:
    total_all = len(graduate_df)
    pass_all = len(pass_ids)
    rate_all = round((pass_all / total_all) * 100, 2) if total_all > 0 else 0.0

    valid_graduate_df = graduate_df[~graduate_df[GRADUATE_MAJOR_COL].isin(EXCLUDE_MAJORS)]
    total_valid = len(valid_graduate_df)
    valid_ids = set(valid_graduate_df[GRADUATE_ID_COL].tolist())
    pass_valid = len(pass_ids & valid_ids)
    rate_valid = round((pass_valid / total_valid) * 100, 2) if total_valid > 0 else 0.0

    major_stats = []
    for major, major_df in graduate_df.groupby(GRADUATE_MAJOR_COL):
        major_ids = set(major_df[GRADUATE_ID_COL].tolist())
        major_pass = len(pass_ids & major_ids)
        major_total = len(major_df)
        major_rate = round((major_pass / major_total) * 100, 2) if major_total > 0 else 0.0
        major_stats.append({
            "届别": grade,
            "培养层次": level,
            "专业": major,
            "专业总人数": major_total,
            f"{exam_type}通过人数": major_pass,
            f"{exam_type}通过率(%)": major_rate,
        })

    overall_all = {
        "届别": grade,
        "培养层次": level,
        "专业": "全部该层次（含所有专业）",
        "专业总人数": total_all,
        f"{exam_type}通过人数": pass_all,
        f"{exam_type}通过率(%)": rate_all,
    }
    overall_valid = {
        "届别": grade,
        "培养层次": level,
        "专业": "有效整体（排除指定专业）",
        "专业总人数": total_valid,
        f"{exam_type}通过人数": pass_valid,
        f"{exam_type}通过率(%)": rate_valid,
    }
    return pd.DataFrame([overall_all, overall_valid] + major_stats)


def _write_single_grade_level_excel(archive: zipfile.ZipFile, grade: str, level: str, cet4_result: pd.DataFrame, cet6_result: pd.DataFrame):
    output = BytesIO()
    wb = Workbook()
    wb.remove(wb.active)
    for sheet_title, df in (("四级通过率统计", cet4_result), ("六级通过率统计", cet6_result)):
        ws = wb.create_sheet(title=sheet_title)
        for row in dataframe_to_rows(df, index=False, header=True):
            ws.append(row)
        for cell in ws[1]:
            cell.font = Font(bold=True)
            cell.alignment = Alignment(horizontal="center")
        for column in ws.columns:
            ws.column_dimensions[column[0].column_letter].width = 15
    wb.save(output)
    archive.writestr(f"{grade}{level}四六级通过率统计.xlsx", output.getvalue())


def calculate_cet_pass_rates(graduate_files: list[tuple[str, bytes]], cet4_paths: list[str], cet6_paths: list[str]) -> BytesIO:
    if not graduate_files:
        raise ValueError("请上传至少一个毕业生届别 Excel")
    if not cet4_paths or not cet6_paths:
        raise ValueError("请分别上传四级和六级 DBF 成绩文件")

    cet4_score_df = read_dbf_files(cet4_paths)
    cet6_score_df = read_dbf_files(cet6_paths)
    all_major_trend_data = []
    all_grade_level_data = []

    zip_output = BytesIO()
    with zipfile.ZipFile(zip_output, "w", zipfile.ZIP_DEFLATED) as archive:
        for filename, file_bytes in graduate_files:
            graduate_df = pd.read_excel(BytesIO(file_bytes))
            _require_columns(graduate_df, {GRADUATE_ID_COL, GRADUATE_MAJOR_COL, GRADUATE_LEVEL_COL}, f"{filename}")
            graduate_df = graduate_df.drop_duplicates(subset=[GRADUATE_ID_COL]).reset_index(drop=True)
            grade_name, grade_year = extract_grade_from_filename(filename)

            for level in TARGET_LEVELS:
                level_df = graduate_df[graduate_df[GRADUATE_LEVEL_COL] == level].copy()
                level_total = len(level_df)
                if level_total == 0:
                    all_grade_level_data.append({
                        "届别": grade_name,
                        "培养层次": level,
                        "总人数": 0,
                        "四级通过人数": 0,
                        "四级通过率(%)": 0.0,
                        "六级通过人数": 0,
                        "六级通过率(%)": 0.0,
                        "有效总人数": 0,
                        "有效四级通过率(%)": 0.0,
                        "有效六级通过率(%)": 0.0,
                    })
                    continue

                level_id_list = level_df[GRADUATE_ID_COL].tolist()
                cet4_pass_ids = clean_score_data(cet4_score_df, level_id_list)
                cet6_pass_ids = clean_score_data(cet6_score_df, level_id_list)
                cet4_detail_df = calculate_single_level_detail(level_df, cet4_pass_ids, "四级", level, grade_name)
                cet6_detail_df = calculate_single_level_detail(level_df, cet6_pass_ids, "六级", level, grade_name)
                _write_single_grade_level_excel(archive, grade_name, level, cet4_detail_df, cet6_detail_df)

                valid_df = level_df[~level_df[GRADUATE_MAJOR_COL].isin(EXCLUDE_MAJORS)]
                valid_ids = set(valid_df[GRADUATE_ID_COL].tolist())
                cet4_pass_all = len(cet4_pass_ids)
                cet6_pass_all = len(cet6_pass_ids)
                all_grade_level_data.append({
                    "届别": grade_name,
                    "培养层次": level,
                    "总人数": level_total,
                    "四级通过人数": cet4_pass_all,
                    "四级通过率(%)": round((cet4_pass_all / level_total) * 100, 2),
                    "六级通过人数": cet6_pass_all,
                    "六级通过率(%)": round((cet6_pass_all / level_total) * 100, 2),
                    "有效总人数": len(valid_df),
                    "有效四级通过率(%)": round((len(cet4_pass_ids & valid_ids) / len(valid_df)) * 100, 2) if len(valid_df) else 0.0,
                    "有效六级通过率(%)": round((len(cet6_pass_ids & valid_ids) / len(valid_df)) * 100, 2) if len(valid_df) else 0.0,
                })

                for major, major_df in level_df.groupby(GRADUATE_MAJOR_COL):
                    major_ids = set(major_df[GRADUATE_ID_COL].tolist())
                    major_total = len(major_df)
                    all_major_trend_data.append({
                        "培养层次": level,
                        "专业": major,
                        "届别": grade_name,
                        "届别年份": grade_year,
                        "总人数": major_total,
                        "四级通过人数": len(cet4_pass_ids & major_ids),
                        "四级通过率(%)": round((len(cet4_pass_ids & major_ids) / major_total) * 100, 2) if major_total else 0.0,
                        "六级通过人数": len(cet6_pass_ids & major_ids),
                        "六级通过率(%)": round((len(cet6_pass_ids & major_ids) / major_total) * 100, 2) if major_total else 0.0,
                    })

        trend_raw_df = pd.DataFrame(all_major_trend_data)
        if not trend_raw_df.empty:
            all_grades_sorted = sorted(set(trend_raw_df["届别"]), key=lambda x: extract_grade_from_filename(x)[1])
            trend_data = []
            for (level, major), group_df in trend_raw_df.groupby(["培养层次", "专业"]):
                row = {"培养层次": level, "专业": major}
                for grade in all_grades_sorted:
                    grade_df = group_df[group_df["届别"] == grade]
                    if len(grade_df) > 0:
                        row[f"{grade}-总人数"] = grade_df["总人数"].iloc[0]
                        row[f"{grade}-四级通过率(%)"] = grade_df["四级通过率(%)"].iloc[0]
                        row[f"{grade}-六级通过率(%)"] = grade_df["六级通过率(%)"].iloc[0]
                    else:
                        row[f"{grade}-总人数"] = "-"
                        row[f"{grade}-四级通过率(%)"] = "-"
                        row[f"{grade}-六级通过率(%)"] = "-"
                trend_data.append(row)
            trend_df = pd.DataFrame(trend_data).sort_values(by=["培养层次", "专业"])
        else:
            trend_df = pd.DataFrame()

        summary_df = pd.DataFrame(all_grade_level_data).sort_values(by=["届别", "培养层次"])
        summary_output = BytesIO()
        with pd.ExcelWriter(summary_output, engine="openpyxl") as writer:
            trend_df.to_excel(writer, sheet_name="专业通过率趋势表", index=False)
            summary_df.to_excel(writer, sheet_name="整体通过率汇总表", index=False)
            for ws in writer.book.worksheets:
                for cell in ws[1]:
                    cell.font = Font(bold=True, size=11)
                    cell.alignment = Alignment(horizontal="center")
                for column in ws.columns:
                    max_length = max(len(str(cell.value)) if cell.value is not None else 0 for cell in column)
                    ws.column_dimensions[column[0].column_letter].width = min(max(max_length + 2, 12), 24)
        archive.writestr("多届专业通过率趋势分析表.xlsx", summary_output.getvalue())

    zip_output.seek(0)
    return zip_output
