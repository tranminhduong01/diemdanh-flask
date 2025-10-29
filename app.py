import traceback
import mysql.connector
import base64
from flask import Flask, request, redirect, url_for, flash, session, render_template, jsonify, current_app, send_file
import os
from openpyxl.workbook import Workbook
from werkzeug.utils import secure_filename
# ❌ Nặng - KHÔNG hỗ trợ trên Render (AI + Nhận diện khuôn mặt)
# import face_recognition
# import numpy as np
# from PIL import Image, ImageOps, ImageFile
import io
from datetime import date, datetime
import random
from werkzeug.security import check_password_hash, generate_password_hash
import json
from flask_apscheduler import APScheduler
from flask_socketio import SocketIO, emit
# ❌ Không dùng khi deploy (AI chatbot)
# from ai import get_ai_response
# from google.genai import Client, types
from email.message import EmailMessage
import smtplib
from email.mime.text import MIMEText
from flask_mail import Mail, Message
from itsdangerous import URLSafeTimedSerializer
# ❌ Nếu không có file .env thì cũng có thể bỏ qua
from dotenv import load_dotenv

# Nếu dùng PIL ở local, giữ lại dòng dưới:
# ImageFile.LOAD_TRUNCATED_IMAGES = True

load_dotenv()

app = Flask(__name__)
app.secret_key = "123456"  # Khóa bí mật session
socketio = SocketIO(app)

# =============================
# HÀM KẾT NỐI DATABASE
# =============================
def get_connection():
    return mysql.connector.connect(
        host="localhost",
        user="root",
        password="123456",
        database="face_attendance",
        auth_plugin="mysql_native_password"
    )

def add_notification(user_id, title, message):
    print("📢 Hàm add_notification chạy")
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO notifications (user_id, title, message, is_read, created_at)
        VALUES (%s, %s, %s, 0, NOW())
    """, (user_id, title, message))
    conn.commit()
    cursor.close()
    conn.close()




# =============================
# ROUTE MẶC ĐỊNH → LOGIN
# =============================
@app.route("/")
def index():
    return redirect(url_for("login"))

# =============================
# TRANG ĐĂNG KÝ
# =============================
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form["name"]
        username = request.form["username"]
        password = request.form["password"]
        role = request.form.get("role", "student")

        # ✅ Lấy độ dài tối thiểu từ setting (mặc định 8 nếu chưa cấu hình)
        min_length = get_setting("password_min_length", 8)
        if len(password) < min_length:
            flash(f"Mật khẩu phải có ít nhất {min_length} ký tự!", "danger")
            return redirect(url_for("register"))

        conn = get_connection()
        cursor = conn.cursor()

        try:
            cursor.execute("SELECT * FROM users WHERE username = %s", (username,))
            existing_user = cursor.fetchone()

            if existing_user:
                flash("Tên đăng nhập đã tồn tại!", "danger")
                return redirect(url_for("register"))

            hashed_pw = generate_password_hash(password)

            cursor.execute(
                "INSERT INTO users (name, username, password, role) VALUES (%s, %s, %s, %s)",
                (name, username, hashed_pw, role)
            )
            conn.commit()
            flash("Đăng ký thành công! Mời đăng nhập.", "success")
            return redirect(url_for("login"))

        except mysql.connector.Error as e:
            flash(f"Lỗi database: {str(e)}", "danger")
        finally:
            cursor.close()
            conn.close()

    return render_template("register.html")

@app.context_processor
def inject_user():
    user_id = session.get("user_id")
    if not user_id:
        return {"user": None}

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM users WHERE id=%s", (user_id,))
    user = cursor.fetchone()
    cursor.close()
    conn.close()

    return {"user": user}




# =============================
# TRANG ĐĂNG NHẬP
# =============================
def get_setting(key, default=None):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT setting_value, value_type FROM system_settings WHERE setting_key = %s", (key,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    if not row:
        return default

    value = row["setting_value"]
    if row["value_type"] == "int":
        return int(value)
    elif row["value_type"] == "bool":
        return value.lower() in ["1", "true", "yes"]
    return value


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]

        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        try:
            cursor.execute("SELECT * FROM users WHERE username = %s", (username,))
            user = cursor.fetchone()

            if not user:
                flash("❌ Sai username hoặc password!", "danger")
                return redirect(url_for("login"))

            # ✅ kiểm tra khóa tạm thời
            if user.get("locked_until") and user["locked_until"] > datetime.now():
                remaining = (user["locked_until"] - datetime.now()).seconds // 60 + 1
                flash(f"🔒 Tài khoản đang bị tạm khóa. Vui lòng thử lại sau {remaining} phút.", "danger")
                return redirect(url_for("login"))

            # ✅ kiểm tra mật khẩu (CHUẨN VỚI generate_password_hash)
            if check_password_hash(user["password"], password):
                # reset failed_attempts và locked_until
                cursor.execute(
                    "UPDATE users SET failed_attempts = 0, locked_until = NULL WHERE id = %s",
                    (user["id"],)
                )
                conn.commit()

                # tạo session
                session["user_id"] = user["id"]
                session["username"] = user["username"]
                session["role"] = user["role"]

                flash("✅ Đăng nhập thành công!", "success")

                # ✅ phân quyền theo role
                if user["role"] == "admin":
                    return redirect(url_for("admin_dashboard"))
                elif user["role"] == "teacher":
                    return redirect(url_for("teacher_dashboard"))
                else:
                    return redirect(url_for("student_dashboard"))
            else:
                # ❌ sai mật khẩu
                max_attempts = get_setting("max_login_attempts", 5)  # lấy từ system_settings
                new_attempts = user["failed_attempts"] + 1

                if new_attempts >= max_attempts:
                    lock_time = datetime.now() + timedelta(minutes=5)  # khóa 5 phút
                    cursor.execute(
                        "UPDATE users SET failed_attempts = 0, locked_until = %s WHERE id = %s",
                        (lock_time, user["id"])
                    )
                    conn.commit()
                    flash("🚫 Bạn đã nhập sai quá nhiều lần. Tài khoản bị khóa 5 phút!", "danger")
                else:
                    cursor.execute(
                        "UPDATE users SET failed_attempts = %s WHERE id = %s",
                        (new_attempts, user["id"])
                    )
                    conn.commit()
                    flash(f"Sai mật khẩu! Bạn còn {max_attempts - new_attempts} lần thử.", "warning")

        except mysql.connector.Error as e:
            flash(f"Lỗi database: {str(e)}", "danger")
        finally:
            cursor.close()
            conn.close()

    return render_template("login.html")

# =============================
# DASHBOARD MỖI ROLE
# =============================
@app.route("/admin")
def admin_dashboard():
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    # Đếm số user
    cursor.execute("SELECT COUNT(*) AS total FROM users")
    user_count = cursor.fetchone()["total"]

    # Đếm số lớp
    cursor.execute("SELECT COUNT(*) AS total FROM classes")
    class_count = cursor.fetchone()["total"]

    # Tính % điểm danh hôm nay
    cursor.execute("SELECT COUNT(*) AS total FROM attendance_records WHERE date = CURDATE()")
    total_today = cursor.fetchone()["total"]

    cursor.execute("SELECT COUNT(*) AS present FROM attendance_records WHERE date = CURDATE() AND status_in = 'present'")
    present_today = cursor.fetchone()["present"]

    attendance_rate = 0
    if total_today > 0:
        attendance_rate = round((present_today / total_today) * 100, 2)

    # Hoạt động gần đây (user mới, lớp mới)
    activities = []

    # Người dùng mới
    cursor.execute("SELECT username, id FROM users ORDER BY id DESC LIMIT 5")
    for row in cursor.fetchall():
        activities.append({
            "icon": "bi-person-plus",
            "title": "Người dùng mới đăng ký",
            "desc": f"{row['username']} vừa đăng ký tài khoản",
            "time": f"ID: {row['id']}"
        })

    # Lớp học mới
    cursor.execute("SELECT class_name, created_at FROM classes ORDER BY created_at DESC LIMIT 5")
    for row in cursor.fetchall():
        activities.append({
            "icon": "bi-journal-plus",
            "title": "Lớp học mới",
            "desc": f"Lớp {row['class_name']} đã được tạo",
            "time": row['created_at'].strftime("%Y-%m-%d %H:%M:%S")
        })

    # >>> Dữ liệu cho biểu đồ thống kê điểm danh 7 ngày qua <<<
    cursor.execute("""
        SELECT DATE(date) AS ngay, COUNT(*) AS so_luot
        FROM attendance_records
        WHERE date >= CURDATE() - INTERVAL 6 DAY
        GROUP BY DATE(date)
        ORDER BY ngay
    """)
    rows = cursor.fetchall()

    # Nếu ngày nào chưa có dữ liệu thì Chart.js vẫn cần label → data
    labels = [r['ngay'].strftime("%d/%m") for r in rows]
    data_chart = [r['so_luot'] for r in rows]

    conn.close()

    return render_template(
        "Admin/Admin.html",
        user_count=user_count,
        class_count=class_count,
        attendance_rate=attendance_rate,
        activities=activities,
        labels=labels,
        data_chart=data_chart
    )

def format_time(value):
    if isinstance(value, timedelta):
        total_seconds = int(value.total_seconds())
        hours, remainder = divmod(total_seconds, 3600)
        minutes, _ = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}"
    return value

app.jinja_env.filters['format_time'] = format_time

# Hàm đổi thời gian thành "x phút trước"
@app.template_filter('time_ago')
def time_ago(value):
    from datetime import datetime
    if not value:  # nếu None hoặc rỗng thì trả về chuỗi rỗng
        return ""
    try:
        # nếu value chưa phải datetime thì convert
        if not isinstance(value, datetime):
            from dateutil import parser
            value = parser.parse(str(value))

        now = datetime.now()
        diff = now - value
        seconds = diff.total_seconds()

        if seconds < 60:
            return f"{int(seconds)} giây trước"
        elif seconds < 3600:
            return f"{int(seconds // 60)} phút trước"
        elif seconds < 86400:
            return f"{int(seconds // 3600)} giờ trước"
        else:
            return f"{int(seconds // 86400)} ngày trước"
    except Exception as e:
        print("⚠️ time_ago filter error:", e)
        return ""


app.jinja_env.filters['time_ago'] = time_ago


@app.route("/teacher")
def teacher_dashboard():
    if "role" not in session or session["role"] != "teacher":
        return redirect(url_for("login"))

    user_id = session["user_id"]
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    # 🧠 Lấy thông tin giảng viên để hiển thị trong menu_gv.html
    cursor.execute("""
        SELECT 
            u.name, u.avatar, t.major
        FROM users u
        LEFT JOIN teacher t ON u.id = t.user_id
        WHERE u.id = %s
    """, (user_id,))
    teacher = cursor.fetchone()

    # 1️⃣ Lấy số lớp mà giảng viên này đang dạy
    cursor.execute("SELECT COUNT(*) AS total FROM classes WHERE teacher_id = %s", (user_id,))
    lop_hoc = cursor.fetchone()["total"]

    # 2️⃣ Tổng sinh viên
    cursor.execute("""
        SELECT COUNT(DISTINCT student_id) AS total
        FROM enrollments e
        JOIN classes c ON e.class_id = c.id
        WHERE c.teacher_id = %s
    """, (user_id,))
    sinh_vien = cursor.fetchone()["total"]

    # 3️⃣ Đơn xin phép chờ duyệt
    cursor.execute("""
        SELECT COUNT(*) AS total
        FROM leave_requests r
        JOIN classes c ON r.classes_id = c.id
        WHERE c.teacher_id = %s AND r.status = 'pending'
    """, (user_id,))
    don_cho_duyet = cursor.fetchone()["total"]

    # 4️⃣ Buổi học hôm nay
    cursor.execute("""
        SELECT COUNT(*) AS total
        FROM sessions s
        JOIN classes c ON s.class_id = c.id
        WHERE c.teacher_id = %s AND DATE(s.date) = CURDATE()
    """, (user_id,))
    buoi_hom_nay = cursor.fetchone()["total"]

    # 5️⃣ Lịch học hôm nay
    cursor.execute("""
        SELECT s.start_time, s.end_time, s.session_number, s.date,
               c.class_name, c.room,
               (SELECT COUNT(*) FROM sessions WHERE class_id = c.id) AS total_sessions
        FROM sessions s
        JOIN classes c ON s.class_id = c.id
        WHERE c.teacher_id = %s AND DATE(s.date) = CURDATE()
        ORDER BY s.start_time
    """, (user_id,))
    lich_hom_nay = cursor.fetchall()

    # ==============================
    # 6️⃣ Hoạt động gần đây (an toàn với None)
    # ==============================
    activities = []

    from datetime import datetime, date, timedelta

    def ensure_datetime(value):
        """
        Trả về:
         - datetime nếu value hợp lệ,
         - None nếu value is None hoặc không parse được.
        """
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        if isinstance(value, date):
            # date -> datetime at 00:00
            return datetime.combine(value, datetime.min.time())
        # Nếu DB trả về chuỗi (iso) -> thử parse
        try:
            return datetime.fromisoformat(str(value))
        except Exception:
            return None

    # helper để thêm activity: đảm bảo có 'time' (None hoặc datetime) và 'sort_time' (datetime)
    def push_activity(act_type, title, description, time_value):
        t = ensure_datetime(time_value)
        activities.append({
            "type": act_type,
            "title": title,
            "description": description,
            "time": t,                     # dùng để hiển thị; có thể là None
            "sort_time": t or datetime.min  # dùng để sort (luôn datetime)
        })

    # Tin nhắn mới
    cursor.execute("""
        SELECT m.noi_dung, m.thoi_gian, s.name AS sender_name
        FROM messages m
        JOIN users s ON m.nguoi_gui_id = s.id
        WHERE m.nguoi_nhan_id = %s
        ORDER BY m.thoi_gian DESC
        LIMIT 3
    """, (user_id,))
    for row in cursor.fetchall():
        push_activity("message",
                      "Tin nhắn mới",
                      f"Từ {row['sender_name']}: {row['noi_dung']}",
                      row["thoi_gian"])

    # Lớp học mới
    cursor.execute("""
        SELECT c.class_name, c.created_at, u.name AS teacher_name
        FROM classes c
        JOIN users u ON c.teacher_id = u.id
        ORDER BY c.created_at DESC
        LIMIT 3
    """)
    for row in cursor.fetchall():
        push_activity("class",
                      "Lớp học mới",
                      f"{row['class_name']} (GV: {row['teacher_name']})",
                      row["created_at"])

    # Điểm danh gần đây
    cursor.execute("""
        SELECT a.status_in, a.time_in, u.name, c.class_name
        FROM attendance_records a
        JOIN enrollments e ON a.enrollment_id = e.id
        JOIN users u ON e.student_id = u.id
        JOIN classes c ON e.class_id = c.id
        ORDER BY a.time_in DESC
        LIMIT 3
    """)
    for row in cursor.fetchall():
        push_activity("attendance",
                      f"Điểm danh: {row['status_in']}",
                      f"{row['name']} - {row['class_name']}",
                      row["time_in"])

    # Ghi danh mới
    cursor.execute("""
        SELECT e.created_at, u.name, c.class_name
        FROM enrollments e
        JOIN users u ON e.student_id = u.id
        JOIN classes c ON e.class_id = c.id
        ORDER BY e.created_at DESC
        LIMIT 3
    """)
    for row in cursor.fetchall():
        push_activity("enrolment",
                      "Sinh viên ghi danh",
                      f"{row['name']} vào lớp {row['class_name']}",
                      row["created_at"])

    # Đơn xin phép
    cursor.execute("""
        SELECT r.request_date, r.status, u.name, c.class_name
        FROM leave_requests r
        JOIN users u ON r.user_id = u.id
        JOIN classes c ON r.classes_id = c.id
        ORDER BY r.request_date DESC
        LIMIT 3
    """)
    for row in cursor.fetchall():
        push_activity("leave",
                      "Đơn xin phép",
                      f"{row['name']} - {row['class_name']} ({row['status']})",
                      row["request_date"])

    activities = sorted(
        activities,
        key=lambda x: x["time"] if isinstance(x["time"], datetime) else datetime.min,
        reverse=True
    )[:5]

    cursor.close()
    conn.close()

    return render_template("GV/Giangvien.html",
                           teacher=teacher,
                           ten=session.get("username"),
                           lop_hoc=lop_hoc,
                           sinh_vien=sinh_vien,
                           lich_hom_nay=lich_hom_nay,
                           don_cho_duyet=don_cho_duyet,
                           buoi_hom_nay=buoi_hom_nay,
                           activities=activities)




@app.route("/student")
def student_dashboard():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login"))

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    # 🔹 Lấy thông tin user (tên + avatar)
    cursor.execute("SELECT * FROM users WHERE id = %s", (user_id,))
    user = cursor.fetchone()

    # 🔹 Lấy danh sách giảng viên
    cursor.execute("SELECT id, name FROM users WHERE role = 'teacher'")
    giang_vien = cursor.fetchall()

    # 🔹 Lấy 3 lớp có nhiều sinh viên ghi danh nhất + tên giảng viên
    query = """
        SELECT c.id, c.class_name, c.room, c.day_of_week, c.start_time, c.end_time,
               c.max_students, c.start_date, COUNT(e.student_id) AS student_count,
               u.name AS teacher_name
        FROM classes c
        LEFT JOIN enrollments e ON c.id = e.class_id
        LEFT JOIN users u ON c.teacher_id = u.id
        GROUP BY c.id
        ORDER BY student_count DESC
        LIMIT 3;
    """
    cursor.execute(query)
    featured_classes = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template(
        "HS/sinhvien.html",
        user=user,  # ✅ Truyền object user
        giang_vien=giang_vien,
        classes=featured_classes
    )


# =============================
# DANH SÁCH LỚP CỦA GIÁO VIÊN
# =============================
@app.route("/lophoc")
def lophoc():
    if "role" not in session or session["role"] != "teacher":
        return redirect(url_for("login"))

    teacher_id = session["user_id"]
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    # ✅ Lấy thông tin giáo viên (avatar + name + major)
    cursor.execute("""
        SELECT 
            u.name, u.avatar, t.major
        FROM users u
        LEFT JOIN teacher t ON u.id = t.user_id
        WHERE u.id = %s
    """, (teacher_id,))
    teacher = cursor.fetchone()

    # ✅ Lấy danh sách lớp do giáo viên này dạy
    cursor.execute("""
        SELECT 
            c.id,
            c.class_name,
            c.room,
            c.day_of_week,
            c.start_time,
            c.end_time,
            c.max_students,
            c.start_date,
            c.weeks,
            c.created_at,
            u.name AS teacher_name
        FROM classes c
        JOIN users u ON c.teacher_id = u.id
        WHERE c.teacher_id = %s
        ORDER BY c.created_at DESC
    """, (teacher_id,))
    classes = cursor.fetchall()

    conn.close()

    return render_template(
        "GV/Lophoc.html",
        classes=classes,
        teacher=teacher,
        ten=teacher["name"] if teacher else "Giáo viên"
    )


# =============================
# CẬP NHẬT LỚP
# =============================
@app.route('/update_class', methods=['POST'])
def update_class():
    if "role" not in session or session["role"] != "teacher":
        return jsonify({'success': False, 'error': 'Unauthorized'})

    try:
        data = request.get_json()
        conn = get_connection()
        cursor = conn.cursor()

        query = """
            UPDATE classes 
            SET class_name=%s, room=%s, day_of_week=%s, start_time=%s, 
                end_time=%s, max_students=%s, start_date=%s, weeks=%s
            WHERE id=%s AND teacher_id=%s
        """
        values = (
            data['class_name'], data['room'], data['day_of_week'],
            data['start_time'], data['end_time'], data['max_students'],
            data['start_date'], data['weeks'],
            data['id'], session["user_id"]   # dùng user_id trong session
        )

        cursor.execute(query, values)
        conn.commit()
        conn.close()

        return jsonify({'success': True})

    except Exception as e:
        print("Error updating class:", e)
        return jsonify({'success': False, 'error': str(e)})

# =============================
# XOÁ LỚP
# =============================
@app.route('/delete_class', methods=['POST'])
def delete_class():
    if "role" not in session or session["role"] != "teacher":
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    try:
        data = request.get_json()
        class_id = data.get("id")
        teacher_id = session["user_id"]

        conn = get_connection()
        cursor = conn.cursor()

        # 1. Xóa attendance_records thông qua sessions
        cursor.execute("""
            DELETE ar FROM attendance_records ar
            JOIN sessions s ON ar.session_id = s.id
            WHERE s.class_id = %s
        """, (class_id,))

        # 2. Xóa enrollments (sinh viên ghi danh vào lớp)
        cursor.execute("DELETE FROM enrollments WHERE class_id = %s", (class_id,))

        # 3. Xóa sessions (buổi học của lớp)
        cursor.execute("DELETE FROM sessions WHERE class_id = %s", (class_id,))

        # 4. Xóa lớp (chỉ khi giáo viên đó là người tạo)
        cursor.execute("DELETE FROM classes WHERE id = %s AND teacher_id = %s", (class_id, teacher_id))

        conn.commit()
        conn.close()

        return jsonify({'success': True})

    except Exception as e:
        print("Error deleting class:", e)
        return jsonify({'success': False, 'error': str(e)}), 500




# =============================
# QUẢN LÝ SINH VIÊN TRONG LỚP
# =============================
@app.route("/QLsinhvien")
def QLsinhvien():
    if "role" not in session or session["role"] != "teacher":
        return redirect(url_for("login"))

    teacher_id = session["user_id"]
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    # ✅ Lấy thông tin giáo viên (avatar + name + major)
    cursor.execute("""
        SELECT 
            u.name, u.avatar, t.major
        FROM users u
        LEFT JOIN teacher t ON u.id = t.user_id
        WHERE u.id = %s
    """, (teacher_id,))
    teacher = cursor.fetchone()

    # ✅ Lấy danh sách lớp mà giảng viên này dạy
    cursor.execute("""
        SELECT 
            c.id,
            c.class_name,
            c.room,
            c.day_of_week,
            c.start_time,
            c.end_time,
            c.max_students,
            c.start_date,
            c.weeks,
            c.created_at,
            u.name AS teacher_name
        FROM classes c
        JOIN users u ON c.teacher_id = u.id
        WHERE c.teacher_id = %s
        ORDER BY c.created_at DESC
    """, (teacher_id,))
    classes = cursor.fetchall()

    conn.close()

    return render_template(
        "GV/QLSinhvien.html",
        classes=classes,
        teacher=teacher,
        ten=teacher["name"] if teacher else session.get("username", "Giáo viên")
    )

import os

@app.route("/api/lop/<int:class_id>/sinhvien")
def get_students(class_id):
    import sys, traceback
    try:
        mssv = request.args.get("mssv", "").strip()
        name = request.args.get("name", "").strip()
        print(f"Params - mssv: '{mssv}', name: '{name}'", flush=True)

        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        query = "SELECT * FROM enrollments WHERE class_id=%s"
        params = [class_id]

        if mssv:
            query += " AND TRIM(mssv) LIKE %s"
            params.append(f"%{mssv}%")
        if name:
            query += " AND LOWER(TRIM(full_name)) LIKE %s COLLATE utf8mb4_0900_ai_ci"
            params.append(f"%{name.lower()}%")

        cursor.execute(query, params)
        students = cursor.fetchall()

        for student in students:
            if student['face_image']:
                normalized_path = student['face_image'].replace("\\", "/")
                relative_path = normalized_path.split("static/")[1]
                student["face_image_url"] = url_for('static', filename=relative_path)
            else:
                student["face_image_url"] = url_for('static', filename='uploads/default.png')

        cursor.close()
        conn.close()

        return jsonify(students)
    except Exception as e:
        print("ERROR:", file=sys.stdout, flush=True)
        traceback.print_exc(file=sys.stdout)
        return jsonify({"error": str(e)})


@app.route("/api/enrollment/<int:id>", methods=["PUT"])
def update_enrollment(id):
    data = request.json
    try:
        conn = get_connection()
        cursor = conn.cursor()

        cursor.execute("""
            UPDATE enrollments
            SET full_name = %s,
                mssv = %s,
                lop = %s,
                major = %s,
                phone = %s,
                email = %s
            WHERE id = %s
        """, (
            data.get("full_name"),
            data.get("mssv"),
            data.get("lop"),
            data.get("major"),
            data.get("phone"),
            data.get("email"),
            id
        ))

        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        print("Lỗi khi cập nhật:", e)
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/enrollment/<int:id>', methods=['DELETE'])
def delete_enrollment(id):
    try:
        conn = get_connection()
        cursor = conn.cursor()

        # Kiểm tra tồn tại
        cursor.execute("SELECT id FROM enrollments WHERE id = %s", (id,))
        enrollment = cursor.fetchone()
        if not enrollment:
            return jsonify({"success": False, "error": "Không tìm thấy bản ghi"}), 404

        # Thực hiện xóa
        cursor.execute("DELETE FROM enrollments WHERE id = %s", (id,))
        conn.commit()

        return jsonify({"success": True})
    except Exception as e:
        print("Lỗi khi xóa enrollment:", e)
        return jsonify({"success": False, "error": "Lỗi server"}), 500
    finally:
        if conn:
            cursor.close()
            conn.close()






# =============================
# LOGOUT
# =============================
@app.route("/logout")
def logout():
    session.clear()
    flash("Đã đăng xuất!", "info")
    return redirect(url_for("login"))

# =============================
# API TẠO LỚP
# =============================



def create_sessions_for_class(class_id, start_date, weeks, start_time, end_time, day_of_week):
    conn = get_connection()
    cursor = conn.cursor()

    if isinstance(day_of_week, int):
        target_weekday = day_of_week
    else:
        target_weekday = WEEKDAY_MAP[day_of_week]
    current_date = datetime.strptime(start_date, "%Y-%m-%d")  # YYYY-MM-DD

    # Tìm ngày đầu tiên đúng thứ
    while current_date.weekday() != target_weekday:
        current_date += timedelta(days=1)

    # Sinh các buổi học
    for i in range(weeks):
        session_date = current_date + timedelta(weeks=i)

        cursor.execute("""
            INSERT INTO sessions (class_id, session_number, date, start_time, end_time)
            VALUES (%s, %s, %s, %s, %s)
        """, (class_id, i+1, session_date.date(), start_time, end_time))

        # ✅ Lấy lại session_id vừa mới insert
        session_id = cursor.lastrowid

        print(f"➡️ Tạo buổi học {i + 1} cho class {class_id} vào ngày {session_date}")
        schedule_absent_job(session_id, session_date.date(), end_time)

    conn.commit()
    cursor.close()
    conn.close()

WEEKDAY_MAP = {
    "Thứ 2": 0,
    "Thứ 3": 1,
    "Thứ 4": 2,
    "Thứ 5": 3,
    "Thứ 6": 4,
    "Thứ 7": 5,
    "Chủ nhật": 6
}

WEEKDAY_MAP_REVERSE = {
    0: "Thứ 2",
    1: "Thứ 3",
    2: "Thứ 4",
    3: "Thứ 5",
    4: "Thứ 6",
    5: "Thứ 7",
    6: "Chủ nhật"
}


@app.route("/api/taolop", methods=["POST"])
def api_create_class():
    if "role" not in session or session["role"] != "teacher":
        return jsonify({"error": "Không có quyền"}), 403

    print("🚀 API tạo lớp được gọi")

    data = request.json
    class_name = data.get("class_name")
    room = data.get("room")
    start_time = data.get("start_time")  # "HH:MM"
    end_time = data.get("end_time")      # "HH:MM"
    max_students = data.get("max_students")
    start_date = data.get("start_date")  # "YYYY-MM-DD"
    weeks = int(data.get("weeks", 0))
    teacher_id = session["user_id"]

    dt = datetime.strptime(start_date, "%Y-%m-%d").date()
    day_of_week = WEEKDAY_MAP_REVERSE[dt.weekday()]  # Trả ra 'Thứ 2' chẳng hạn

    conn = get_connection()
    cursor = conn.cursor()

    # ✅ Check trùng lịch
    start_time_obj = datetime.strptime(start_time, "%H:%M").time()
    end_time_obj = datetime.strptime(end_time, "%H:%M").time()

    cursor.execute("""
        SELECT id, class_name, start_time, end_time 
        FROM classes
        WHERE teacher_id = %s
          AND day_of_week = %s
          AND (
              (start_time <= %s AND end_time > %s) OR
              (start_time < %s AND end_time >= %s) OR
              (%s <= start_time AND %s >= end_time)
          )
    """, (teacher_id, day_of_week,
          start_time_obj, start_time_obj,
          end_time_obj, end_time_obj,
          start_time_obj, end_time_obj))

    conflict = cursor.fetchone()
    if conflict:
        cursor.close()
        conn.close()
        return jsonify({
            "error": f"Trùng lịch với lớp '{conflict[1]}' ({conflict[2]} - {conflict[3]})"
        }), 400

    # 1️⃣ Thêm lớp học mới
    cursor.execute("""
        INSERT INTO classes 
        (class_name, teacher_id, room, day_of_week, start_time, end_time, max_students, start_date, weeks, created_at) 
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
    """, (class_name, teacher_id, room, day_of_week, start_time_obj, end_time_obj, max_students, dt, weeks))
    class_id = cursor.lastrowid
    conn.commit()
    cursor.close()
    conn.close()

    # 2️⃣ ✅ Gọi hàm tạo buổi học (và schedule job auto-vắng)
    create_sessions_for_class(class_id, start_date, weeks, start_time_obj, end_time_obj, day_of_week)

    # 3️⃣ Thông báo
    add_notification(teacher_id, "Tạo lớp học thành công",
        f"Bạn đã tạo lớp '{class_name}' bắt đầu từ ngày {dt.strftime('%d/%m/%Y')} tại phòng {room}.")

    return jsonify({
        "class_id": class_id,
        "day_of_week": day_of_week,
        "start_date": dt.strftime("%d/%m/%Y")
    })



@app.route("/api/buoi_hoc/<int:class_id>")
def get_buoi_hoc(class_id):
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT id, session_number, date, start_time, end_time
            FROM sessions
            WHERE class_id = %s
            ORDER BY session_number ASC
        """, (class_id,))
        sessions = cursor.fetchall()
        cursor.close()
        conn.close()

        formatted_sessions = []
        for s in sessions:
            # format TIME (timedelta) thành HH:MM
            def format_time(t):
                if t is None:
                    return None
                total_seconds = int(t.total_seconds())
                hours = total_seconds // 3600
                minutes = (total_seconds % 3600) // 60
                return f"{hours:02d}:{minutes:02d}"

            formatted_sessions.append({
                "id": s["id"],
                "session_number": s["session_number"],
                "date": s["date"].strftime("%d/%m/%Y") if s["date"] else None,
                "start_time": format_time(s["start_time"]),
                "end_time": format_time(s["end_time"]),
            })

        return jsonify(formatted_sessions)
    except Exception as e:
        print("Error fetching sessions:", e)
        return jsonify([])




# =============================
# SINH VIÊN GHI DANH LỚP
# =============================
@app.route("/ghidanh")
def ghidanh():
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    query = """
        SELECT 
            c.id,
            c.class_name,
            c.room,
            c.day_of_week,
            c.start_time,
            c.end_time,
            c.max_students,
            c.start_date,
            c.weeks,
            c.created_at,
            u.name AS teacher_name
        FROM classes c
        JOIN users u ON c.teacher_id = u.id
        ORDER BY c.created_at DESC
    """
    cursor.execute(query)
    classes = cursor.fetchall()

    cursor.close()
    conn.close()
    return render_template("HS/Ghidanh.html", classes=classes)


@app.route("/ghi_vao_lop/<int:class_id>", methods=["GET", "POST"])
def ghi_vao_lop(class_id):
    if "user_id" not in session:
        return redirect("/login")

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    # 🧩 Lấy thông tin lớp từ bảng classes (để hiển thị thông tin cơ bản)
    cursor.execute("SELECT * FROM classes WHERE id = %s", (class_id,))
    lop = cursor.fetchone()
    if not lop:
        cursor.close()
        conn.close()
        return "Không tìm thấy lớp!"

    # 🧩 Kiểm tra sinh viên đã ghi danh chưa
    # Kiểm tra sinh viên đã ghi danh chưa
    cursor.execute(
        "SELECT lop FROM enrollments WHERE student_id = %s AND class_id = %s",
        (session["user_id"], class_id)
    )
    da_ghi_danh_row = cursor.fetchone()
    da_ghi_danh = da_ghi_danh_row is not None

    # Lấy tên lớp sinh viên đã ghi danh
    da_ghi_danh_class_name = da_ghi_danh_row["lop"] if da_ghi_danh_row else None

    # 🧩 Nếu sinh viên gửi yêu cầu ghi danh
    if request.method == "POST" and not da_ghi_danh:
        # Lấy tên lớp sinh viên nhập từ form
        ten_lop_sinh_vien = request.form.get("lop").strip()

        cursor.execute(
            "INSERT INTO enrollments (student_id, class_id, lop) VALUES (%s, %s, %s)",
            (session["user_id"], class_id, ten_lop_sinh_vien)
        )

        conn.commit()

        # 🔔 Gửi thông báo cho giáo viên
        cursor.execute("SELECT teacher_id FROM classes WHERE id = %s", (class_id,))
        gv = cursor.fetchone()
        if gv and gv["teacher_id"]:
            add_notification(
                gv["teacher_id"],
                "Sinh viên mới ghi danh",
                f"Một sinh viên mới đã ghi danh vào lớp <b>{ten_lop_sinh_vien}</b>."
            )

        # 🔔 Gửi thông báo cho chính sinh viên
        add_notification(
            session["user_id"],
            "Ghi danh thành công",
            f"Bạn đã ghi danh vào lớp <b>{ten_lop_sinh_vien}</b> thành công!"
        )

        cursor.close()
        conn.close()

        flash("Ghi danh thành công!", "success")
        return redirect(f"/ghi_vao_lop/{class_id}")

    cursor.close()
    conn.close()
    return render_template(
        "HS/ghi_vao_lop.html",
        lop=lop,
        da_ghi_danh=da_ghi_danh,
        da_ghi_danh_class_name=da_ghi_danh_class_name
    )








UPLOAD_FOLDER = "static/uploads"


def _load_pil_image(file_bytes):
    from io import BytesIO
    return Image.open(BytesIO(file_bytes))

# --- Hàm enroll_student ---
def enroll_student(student_id, class_id, full_name, mssv, lop, major, phone, email, face_image=None, face_encoding=None):
    """
    Ghi danh sinh viên vào lớp và tạo attendance_records cho tất cả các buổi học.
    """
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # 1️⃣ Lưu enrollments
        cursor.execute("""
            INSERT INTO enrollments 
                (student_id, class_id, full_name, mssv, lop, major, phone, email, face_image, face_encoding, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        """, (
            student_id, class_id, full_name, mssv, lop, major, phone, email,
            face_image, face_encoding
        ))
        conn.commit()
        enrollment_id = cursor.lastrowid
        print(f"✅ Đã enroll student {student_id}, enrollment_id={enrollment_id}")

        # 2️⃣ Lấy tất cả session của lớp
        cursor.execute("SELECT id, DATE(date) AS session_date FROM sessions WHERE class_id = %s", (class_id,))
        sessions = cursor.fetchall()
        print(f"🔎 Số sessions tìm thấy: {len(sessions)}")

        # 3️⃣ Tạo attendance_records
        for ses in sessions:
            session_id = ses["id"]
            session_date = ses["session_date"]
            # Chuyển kiểu datetime sang string nếu cần
            if isinstance(session_date, (datetime.date, datetime.datetime)):
                session_date = session_date.strftime("%Y-%m-%d")

            try:
                cursor.execute("""
                    INSERT INTO attendance_records
                    (enrollment_id, session_id, date, status_in, time_in, status_out, time_out, score)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    enrollment_id, session_id, session_date,
                    'none', None, 'not_checked_out', None, 0.0
                ))
                print(f"➡️ Tạo attendance_record: enrollment_id={enrollment_id}, session_id={session_id}, date={session_date}")
            except mysql.connector.Error as e:
                print(f"❌ Lỗi insert attendance_record: {e}")
        conn.commit()
        print("💾 Đã tạo attendance_records cho tất cả sessions thành công!")

    except mysql.connector.Error as e:
        conn.rollback()
        print(f"❌ Lỗi enroll_student: {e}")
        raise  # route sẽ catch và flash

    finally:
        cursor.close()
        conn.close()


# --- Route ghi danh ---
@app.route("/luu_ghidanh/<int:class_id>", methods=["POST"])
def luu_ghidanh(class_id):
    # Lấy thông tin từ form
    full_name = request.form.get("full_name")
    mssv = request.form.get("mssv")
    major = request.form.get("major")
    phone = request.form.get("phone")
    lop = request.form.get("lop")
    email = request.form.get("email")
    student_id = session.get("user_id")

    if not student_id:
        flash("Bạn cần đăng nhập để ghi danh!", "warning")
        return redirect(url_for("login"))

    face_image_path = None
    face_encoding = None

    # --- 1. Upload file ---
    face_file = request.files.get("face_image")
    if face_file and face_file.filename:
        try:
            file_bytes = face_file.read()
            pil = _load_pil_image(file_bytes)
            pil = ImageOps.exif_transpose(pil).convert("RGB")
            filename = secure_filename(f"{mssv}_{int(datetime.now().timestamp())}.jpg")
            save_path = os.path.join(UPLOAD_FOLDER, filename)
            os.makedirs(UPLOAD_FOLDER, exist_ok=True)
            pil.save(save_path, format="JPEG", quality=92)
            face_image_path = save_path
            print("📸 Đã lưu ảnh upload:", face_image_path)
        except Exception as e:
            print("❌ Lỗi lưu ảnh upload:", e)
            flash("Lỗi khi xử lý ảnh upload. Vui lòng thử lại.", "danger")
            return redirect(url_for("ghi_vao_lop", class_id=class_id))

    # --- 2. Ảnh chụp từ camera (base64) ---
    face_capture = request.form.get("face_capture")
    if not face_image_path and face_capture and face_capture.startswith("data:image"):
        try:
            header, b64 = face_capture.split(",", 1)
            img_data = base64.b64decode(b64)
            pil = _load_pil_image(img_data)
            pil = ImageOps.exif_transpose(pil).convert("RGB")
            filename = secure_filename(f"{mssv}_capture_{int(datetime.now().timestamp())}.jpg")
            save_path = os.path.join(UPLOAD_FOLDER, filename)
            os.makedirs(UPLOAD_FOLDER, exist_ok=True)
            pil.save(save_path, format="JPEG", quality=92)
            face_image_path = save_path
            print("📸 Đã lưu ảnh camera:", face_image_path)
        except Exception as e:
            print("❌ Lỗi lưu ảnh camera:", e)
            flash("Lỗi khi xử lý ảnh camera. Vui lòng thử lại.", "danger")
            return redirect(url_for("ghi_vao_lop", class_id=class_id))

    # --- 3. Nhận diện khuôn mặt ---
    if face_image_path:
        try:
            import cv2, numpy as np
            image = cv2.imread(face_image_path)
            if image is None:
                raise Exception("Ảnh không đọc được!")

            rgb_image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
            face_locations = face_recognition.face_locations(rgb_image, model="hog")
            print("🔍 Số khuôn mặt tìm thấy:", len(face_locations))

            if face_locations:
                face_encodings = face_recognition.face_encodings(rgb_image, face_locations)
                if face_encodings:
                    face_encoding = json.dumps(face_encodings[0].tolist())
                    print("✅ Đã trích xuất face_encoding, độ dài:", len(face_encoding))
                else:
                    os.remove(face_image_path)
                    flash("❌ Không thể trích xuất đặc trưng khuôn mặt.", "danger")
                    return redirect(url_for("ghi_vao_lop", class_id=class_id))
            else:
                os.remove(face_image_path)
                flash("❌ Không tìm thấy khuôn mặt trong ảnh!", "danger")
                return redirect(url_for("ghi_vao_lop", class_id=class_id))
        except Exception as e:
            print("❌ Lỗi khi xử lý ảnh:", e)
            if face_image_path and os.path.exists(face_image_path):
                os.remove(face_image_path)
            flash("Lỗi khi xử lý ảnh. Vui lòng thử lại.", "danger")
            return redirect(url_for("ghi_vao_lop", class_id=class_id))

    # --- 4. Lưu DB ---
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    # Lấy class_name từ bảng classes
    cursor.execute("SELECT class_name, teacher_id FROM classes WHERE id = %s", (class_id,))
    class_info = cursor.fetchone()

    try:
        # Lưu enrollments (lop = class_name)
        sql = """
            INSERT INTO enrollments 
            (student_id, class_id, full_name, mssv, lop, major, phone, email, face_image, face_encoding) 
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        cursor.execute(sql, (
            student_id, class_id, full_name, mssv, lop,
            major, phone, email, face_image_path, face_encoding
        ))
        conn.commit()
        enrollment_id = cursor.lastrowid
        print("✅ Lưu enrollments thành công! enrollment_id =", enrollment_id)

        # Tạo attendance records
        cursor.execute(
            "SELECT id, DATE(date) as session_date FROM sessions WHERE class_id = %s",
            (class_id,)
        )
        sessions = cursor.fetchall()
        if sessions:
            for ses in sessions:
                cursor.execute("""
                    INSERT INTO attendance_records 
                    (enrollment_id, session_id, date, status_in, status_out, score) 
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (enrollment_id, ses["id"], ses["session_date"], 'none', 'not_checked_out', 0.0))
            conn.commit()
            print("💾 Đã lưu attendance_records vào SQL!")

        # Thông báo cho sinh viên
        add_notification(
            student_id,
            "Ghi danh thành công",
            f"Bạn đã ghi danh vào lớp {class_info['class_name']} thành công!"
        )

        # Thông báo cho giáo viên
        if class_info.get("teacher_id"):
            add_notification(
                class_info["teacher_id"],
                "Sinh viên mới ghi danh",
                f"Sinh viên {full_name} (MSSV {mssv}) vừa ghi danh vào lớp {class_info['class_name']}."
            )

        flash("Ghi danh thành công!", "success")
        return redirect(url_for("ghi_vao_lop", class_id=class_id))

    except mysql.connector.Error as e:
        conn.rollback()
        print("❌ Lỗi database:", e)
        if face_image_path and os.path.exists(face_image_path):
            os.remove(face_image_path)
        flash(f"Lỗi hệ thống khi lưu dữ liệu: {e}", "danger")
        return redirect(url_for("ghi_vao_lop", class_id=class_id))

    finally:
        cursor.close()
        conn.close()


# Cập nhật thông tin enrollment theo id



# --- get_face_encoding ổn định ---
def get_face_encoding(src, upsample=1, jitters=1, model="small"):
    """
    src: path | bytes | file-like | data-url | numpy array | PIL Image
    Returns: numpy array (128,) or None
    """
    try:
        # Sử dụng cách tiếp cận khác: load ảnh thông qua OpenCV
        # thay vì sử dụng trực tiếp từ PIL
        if isinstance(src, (str, bytes)):
            # Nếu là đường dẫn file hoặc dữ liệu bytes
            if isinstance(src, str) and os.path.exists(src):
                # Đọc ảnh từ file
                image = face_recognition.load_image_file(src)
            elif isinstance(src, bytes):
                # Đọc ảnh từ dữ liệu bytes
                image = face_recognition.load_image_file(io.BytesIO(src))
            else:
                return None
        else:
            # Chuyển đổi các định dạng khác về numpy array
            rgb = _to_rgb_uint8_array(src)
            image = rgb

        # Đảm bảo ảnh là định dạng uint8
        if image.dtype != np.uint8:
            image = image.astype(np.uint8)

        # Tìm khuôn mặt
        face_locations = face_recognition.face_locations(
            image,
            number_of_times_to_upsample=upsample,
            model=model
        )

        if not face_locations:
            return None

        # Trích xuất đặc trưng khuôn mặt
        face_encodings = face_recognition.face_encodings(
            image,
            known_face_locations=face_locations,
            num_jitters=jitters
        )

        return face_encodings[0] if face_encodings else None

    except Exception as e:
        print(f"Lỗi get_face_encoding: {e}")
        return None

@app.route("/api/diemdanh/thaydoi", methods=["PUT"])
def update_attendance_status():
    data = request.json
    print("📥 Dữ liệu nhận từ client:", data)  # 👈 Thêm dòng này
    try:
        enrollment_id = data.get("enrollment_id")
        session_id = data.get("session_id")
        field = data.get("field")
        value = data.get("value")

        print("📦 Debug:", enrollment_id, session_id, field, value)  # 👈 Thêm dòng này

        if field not in ["status_in", "status_out"]:
            return jsonify({"success": False, "error": "Trường không hợp lệ"}), 400

        conn = get_connection()
        cursor = conn.cursor()

        cursor.execute(f"""
            UPDATE attendance_records
            SET {field} = %s
            WHERE enrollment_id = %s AND session_id = %s
        """, (value, enrollment_id, session_id))

        conn.commit()
        cursor.close()
        conn.close()

        return jsonify({"success": True})
    except Exception as e:
        print("❌ Lỗi khi cập nhật trạng thái:", e)
        return jsonify({"success": False, "error": str(e)}), 500




from datetime import datetime, timedelta, time as dtime

@app.route("/api/diemdanh/<int:session_id>/<string:action>", methods=["POST"])
def diem_danh(session_id, action):
    cursor = None
    conn = None
    img_saved_path = None
    try:
        if action not in ["in", "out"]:
            return jsonify({"success": False, "message": "Action không hợp lệ (chỉ nhận 'in' hoặc 'out')"}), 400

        file = request.files.get("image")
        if not file:
            return jsonify({"success": False, "message": "Không tìm thấy ảnh trong request"}), 400

        # Đọc dữ liệu ảnh
        try:
            file.stream.seek(0)
        except:
            pass
        file_bytes = file.read()

        face_vec = get_face_encoding(file_bytes)
        temp_folder = os.path.join(current_app.root_path, "static", "temp")
        os.makedirs(temp_folder, exist_ok=True)
        filename = secure_filename(file.filename or f"frame_{int(datetime.now().timestamp())}.jpg")
        img_saved_path = os.path.join(temp_folder, filename)

        # Nếu chưa có vector thì xử lý lại qua PIL
        if face_vec is None:
            pil = _load_pil_image(file_bytes)
            pil = ImageOps.exif_transpose(pil).convert("RGB")
            pil.save(img_saved_path, format="JPEG", quality=90)
            face_vec = get_face_encoding(img_saved_path)

        if face_vec is None:
            return jsonify({"success": False, "message": "Không phát hiện được khuôn mặt nào trong ảnh"}), 400

        # ====== Lấy thông tin buổi học và lớp ======
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT s.class_id, s.session_number, s.start_time, c.teacher_id, c.class_name
            FROM sessions s
            JOIN classes c ON s.class_id = c.id
            WHERE s.id = %s
        """, (session_id,))
        session = cursor.fetchone()

        if not session:
            return jsonify({"success": False, "message": "Không tìm thấy buổi học"}), 404

        class_name = session["class_name"]
        class_id = session["class_id"]
        session_number = session["session_number"]
        teacher_id = session["teacher_id"]
        session_start_time = session["start_time"]

        if isinstance(session_start_time, timedelta):
            seconds = session_start_time.total_seconds()
            hours = int(seconds // 3600)
            minutes = int((seconds % 3600) // 60)
            secs = int(seconds % 60)
            session_start_time = datetime.strptime(f"{hours:02}:{minutes:02}:{secs:02}", "%H:%M:%S").time()

        # ====== Lấy danh sách sinh viên của lớp ======
        cursor.execute("""
            SELECT e.id AS enrollment_id, e.full_name, e.mssv, e.face_encoding, e.student_id
            FROM enrollments e
            WHERE e.class_id = %s
        """, (class_id,))
        students = cursor.fetchall()

        matched = None
        best_dist = float("inf")
        threshold = 0.5
        for sv in students:
            if sv.get("face_encoding"):
                try:
                    db_vec = np.array(json.loads(sv["face_encoding"]), dtype=float)
                    dist = np.linalg.norm(face_vec - db_vec)
                    if dist < threshold and dist < best_dist:
                        best_dist = dist
                        matched = sv
                except:
                    continue

        if not matched:
            return jsonify({"success": False, "message": "Không khớp với SV nào trong lớp"}), 404

        today = datetime.today().date()
        now_dt = datetime.now()
        class_start = datetime.combine(today, session_start_time)
        grace_minutes = 15
        status = "present" if now_dt <= (class_start + timedelta(minutes=grace_minutes)) else "late"

        # ====== Lấy record đã tạo sẵn khi ghi danh ======
        cursor.execute("""
            SELECT id, time_in, time_out, status_in, status_out
            FROM attendance_records
            WHERE enrollment_id = %s AND session_id = %s
        """, (matched["enrollment_id"], session_id))
        record = cursor.fetchone()

        if not record:
            return jsonify({"success": False, "message": "Không tìm thấy record điểm danh"}), 404

        # ---------- CHECK-IN ----------
        if action == "in":
            if record["time_in"]:
                return jsonify({
                    "success": True,
                    "message": f"{matched['full_name']} đã check-in trước đó",
                    "student": matched,
                    "time_in": record["time_in"].strftime("%H:%M:%S") if record["time_in"] else None
                })
            else:
                score = 5 if status == "present" else 2 if status == "late" else 0
                cursor.execute("""
                    UPDATE attendance_records
                    SET time_in = %s, status_in = %s, score = %s
                    WHERE id = %s
                """, (now_dt, status, score, record["id"]))
                conn.commit()

                # Thông báo cho sinh viên
                add_notification(
                    matched["student_id"],
                    "Điểm danh thành công",
                    f"Bạn đã check-in vào buổi {session_number} của lớp {class_name} lúc {now_dt.strftime('%H:%M')}."
                )

                # Thông báo cho giáo viên
                if teacher_id:
                    add_notification(
                        teacher_id,
                        "Sinh viên điểm danh",
                        f"Sinh viên {matched['full_name']} ({matched['mssv']}) đã check-in lúc {now_dt.strftime('%H:%M:%S')}."
                    )

                # ✅ Sau khi cập nhật, lấy danh sách sinh viên hiện tại trong bảng attendance_records
                cursor.execute("""
                    SELECT 
                        ar.id AS record_id, 
                        e.id AS enrollment_id,
                        e.full_name, 
                        e.mssv,
                        ar.status_in, 
                        ar.status_out,
                        ar.time_in, 
                        ar.time_out
                    FROM attendance_records ar
                    JOIN enrollments e ON ar.enrollment_id = e.id
                    WHERE ar.session_id = %s
                """, (session_id,))
                attendance_list = cursor.fetchall()

                return jsonify({
                    "success": True,
                    "message": f"Check-in thành công cho {matched['full_name']}",
                    "student": matched,
                    "time_in": now_dt.strftime("%H:%M:%S"),
                    "score": score,
                    "attendance_list": attendance_list  # 👈 gửi danh sách mới nhất về client
                })

        # ---------- CHECK-OUT ----------
        elif action == "out":
            if not record["time_in"]:
                return jsonify({"success": False, "message": "Chưa check-in nên không thể check-out"}), 400

            if record["time_out"]:
                return jsonify({
                    "success": True,
                    "message": f"{matched['full_name']} đã check-out trước đó",
                    "student": matched,
                    "time_out": record["time_out"].strftime("%H:%M:%S") if record["time_out"] else None
                })
            else:
                cursor.execute("""
                    UPDATE attendance_records
                    SET time_out = %s, status_out = %s, score = score + 5
                    WHERE id = %s
                """, (now_dt, "checked_out", record["id"]))
                conn.commit()

                add_notification(
                    matched["student_id"],
                    "Hoàn tất buổi học",
                    f"Bạn đã check-out khỏi buổi {session_number} của lớp {class_name} lúc {now_dt.strftime('%H:%M')}."
                )

                if teacher_id:
                    add_notification(
                        teacher_id,
                        "Sinh viên rời lớp",
                        f"Sinh viên {matched['full_name']} ({matched['mssv']}) đã check-out lúc {now_dt.strftime('%H:%M:%S')}."
                    )

                # ✅ Sau khi check-out, cập nhật danh sách điểm danh
                cursor.execute("""
                    SELECT 
                        ar.id AS record_id, 
                        e.id AS enrollment_id,
                        e.full_name, 
                        e.mssv,
                        ar.status_in, 
                        ar.status_out,
                        ar.time_in, 
                        ar.time_out
                    FROM attendance_records ar
                    JOIN enrollments e ON ar.enrollment_id = e.id
                    WHERE ar.session_id = %s
                """, (session_id,))
                attendance_list = cursor.fetchall()

                return jsonify({
                    "success": True,
                    "message": f"Check-out thành công cho {matched['full_name']} (+5 điểm)",
                    "student": matched,
                    "time_out": now_dt.strftime("%H:%M:%S"),
                    "attendance_list": attendance_list
                })

    except Exception as e:
        print("Lỗi route /api/diemdanh:", e)
        return jsonify({"success": False, "message": f"Lỗi khi điểm danh: {str(e)}"}), 500

    finally:
        try:
            if cursor: cursor.close()
            if conn: conn.close()
            if img_saved_path and os.path.exists(img_saved_path):
                os.remove(img_saved_path)
        except:
            pass





@app.route("/ds_diemdanh", methods=["GET", "POST"])
def ds_diemdanh():
    if "role" not in session or session["role"] != "teacher":
        return redirect(url_for("login"))

    teacher_id = session["user_id"]
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    # ✅ Lấy thông tin giáo viên (name, avatar, major)
    cursor.execute("""
        SELECT 
            u.name, u.avatar, t.major
        FROM users u
        LEFT JOIN teacher t ON u.id = t.user_id
        WHERE u.id = %s
    """, (teacher_id,))
    teacher = cursor.fetchone()

    # ✅ Lấy danh sách lớp mà giảng viên phụ trách
    query = """
        SELECT 
            c.id,
            c.class_name,
            c.room,
            c.day_of_week,
            c.start_time,
            c.end_time,
            c.max_students,
            c.start_date,
            c.weeks,
            c.created_at,
            u.name AS teacher_name
        FROM classes c
        JOIN users u ON c.teacher_id = u.id
        WHERE c.teacher_id = %s
        ORDER BY c.created_at DESC
    """
    cursor.execute(query, (teacher_id,))
    classes = cursor.fetchall()

    conn.close()

    # ✅ Format lại ngày tháng cho dễ đọc
    for cls in classes:
        if isinstance(cls.get("start_date"), datetime):
            cls["start_date"] = cls["start_date"].strftime("%d/%m/%Y")
        if isinstance(cls.get("created_at"), datetime):
            cls["created_at"] = cls["created_at"].strftime("%d/%m/%Y %H:%M:%S")

    # ✅ Truyền thêm "teacher" vào để hiển thị avatar + name + chuyên ngành trong menu
    return render_template(
        "GV/DS_Diemdanh.html",
        classes=classes,
        teacher=teacher,
        ten=teacher["name"] if teacher else session.get("username", "Giáo viên")
    )

@app.route("/api/attendance_data/<int:session_id>")
def get_attendance_data(session_id):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("""
        SELECT e.id AS enrollment_id, e.mssv, e.full_name,
               a.time_in, a.status_in, a.time_out, a.status_out, a.score
        FROM attendance_records a
        JOIN enrollments e ON a.enrollment_id = e.id
        WHERE a.session_id = %s
    """, (session_id,))
    data = cursor.fetchall()
    conn.close()
    return jsonify(data)

@app.route("/diemdanh/<int:session_id>")
def diemdanh(session_id):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    # Lấy thông tin buổi học
    cursor.execute("""
        SELECT s.id AS session_id, s.session_number, s.date, s.start_time, s.end_time,
               c.id AS class_id, c.class_name
        FROM sessions s
        JOIN classes c ON s.class_id = c.id
        WHERE s.id = %s
    """, (session_id,))
    buoi_hoc = cursor.fetchone()

    if not buoi_hoc:
        conn.close()
        return "Không tìm thấy buổi học này", 404

    # ✅ Lấy danh sách sinh viên + thông tin điểm danh (nếu có)
    cursor.execute("""
        SELECT 
            e.id AS enrollment_id,
            e.mssv,
            e.full_name,
            e.face_image,
            ar.status_in,
            ar.time_in,
            ar.status_out,
            ar.time_out,
            ar.score
        FROM enrollments e
        LEFT JOIN attendance_records ar 
            ON ar.enrollment_id = e.id AND ar.session_id = %s
        WHERE e.class_id = %s
        ORDER BY e.full_name ASC
    """, (session_id, buoi_hoc["class_id"]))
    students = cursor.fetchall()

    conn.close()

    return render_template("GV/DiemDanh.html", buoi_hoc=buoi_hoc, students=students)





app.config['SAVE_DON_NGHI_HOC'] = "static/LUU_NGHI_HOC"
os.makedirs(app.config['SAVE_DON_NGHI_HOC'], exist_ok=True)  # chắc chắn folder tồn tại

@app.route('/don_nghi_hoc', methods=['GET', 'POST'])
def don_nghi_hoc():
    user_id = session.get('user_id')
    if not user_id:
        return redirect('/login')

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    # Lấy các lớp mà sinh viên đã ghi danh
    cursor.execute("""
        SELECT c.id, c.class_name
        FROM enrollments e
        JOIN classes c ON e.class_id = c.id
        WHERE e.student_id = %s
    """, (user_id,))
    classes = cursor.fetchall()

    cursor.close()
    conn.close()

    if request.method == 'GET':
        return render_template('HS/Don_Nghi_Hoc.html', classes=classes)

    # ===== POST: gửi đơn nghỉ học =====
    session_id = request.form.get('session_id')
    classes_id = request.form.get('classes_id')
    reason = request.form.get('reason')
    proof_file = request.files.get('proof_file')

    if not all([session_id, classes_id, reason]):
        return "Vui lòng điền đầy đủ thông tin!"

    proof_filename = None
    if proof_file and proof_file.filename:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = secure_filename(proof_file.filename)
        proof_filename = f"{user_id}_{timestamp}_{filename}"
        filepath = os.path.join(app.config['SAVE_DON_NGHI_HOC'], proof_filename)
        proof_file.save(filepath)

    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        # Lấy thông tin buổi học
        cursor.execute("""
            SELECT s.date, s.session_number, c.class_name, c.teacher_id
            FROM sessions s
            JOIN classes c ON s.class_id = c.id
            WHERE s.id = %s
        """, (session_id,))
        info = cursor.fetchone()
        if not info:
            return "Buổi học không tồn tại!"

        session_date = info['date']
        class_name = info['class_name']
        teacher_id = info['teacher_id']

        # Lưu đơn nghỉ học
        cursor.execute("""
            INSERT INTO leave_requests
            (user_id, request_date, session_date, classes_id, session_id, reason, proof_file, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            user_id,
            datetime.now().date(),
            session_date,
            classes_id,
            session_id,
            reason,
            proof_filename,
            'Pending'
        ))
        conn.commit()

        # Thông báo
        add_notification(user_id, "Đã gửi đơn nghỉ học",
                         f"Bạn đã gửi đơn nghỉ học lớp {class_name}, buổi {info['session_number']} ({session_date}).")
        if teacher_id:
            add_notification(teacher_id, "Đơn nghỉ học mới",
                             f"Sinh viên gửi đơn nghỉ học lớp {class_name}, buổi {info['session_number']} ({session_date}).")
            print("session_id:", session_id)
            print("classes_id:", classes_id)
            print("reason:", reason)
            print("proof_file:", proof_file)

        return "Yêu cầu nghỉ học của bạn đã được gửi thành công!"
    except Exception as err:
        return f'Lỗi khi gửi đơn: {err}'
    finally:
        cursor.close()
        conn.close()


@app.route('/api/get_sessions/<int:class_id>')
def get_sessions(class_id):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT id, session_number, date
        FROM sessions
        WHERE class_id = %s
        ORDER BY date, session_number
    """, (class_id,))
    sessions = cursor.fetchall()
    cursor.close()
    conn.close()

    # Format ngày thành dd/MM/yyyy
    for s in sessions:
        s['date'] = s['date'].strftime('%d/%m/%Y')  # 30/10/2025

    return jsonify(sessions)




@app.route("/Ds_don_nghi")
def Ds_don_nghi():
    user_id = session.get("user_id")
    role = session.get("role")

    if not user_id:
        return redirect(url_for("login"))

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    base_query = """
        SELECT lr.request_id, e.full_name, c.class_name, lr.request_date, lr.session_date,
               lr.reason, lr.proof_file, lr.status
        FROM leave_requests lr
        JOIN classes c ON lr.classes_id = c.id
        JOIN enrollments e ON lr.user_id = e.student_id AND lr.classes_id = e.class_id
    """

    if role == "student":
        # ✅ Sinh viên chỉ thấy đơn của mình
        query = base_query + " WHERE lr.user_id = %s"
        cursor.execute(query, (user_id,))
    elif role in ["teacher", "admin"]:
        # ✅ Giáo viên + admin thấy tất cả đơn
        cursor.execute(base_query)
    else:
        cursor.close()
        conn.close()
        return "Không có quyền truy cập!", 403

    don_nghi_list = cursor.fetchall()

    # ✅ Nếu là giáo viên → lấy thêm thông tin hiển thị trên menu
    teacher = None
    if role == "teacher":
        cursor.execute("""
            SELECT 
                u.name, u.avatar, t.major
            FROM users u
            LEFT JOIN teacher t ON u.id = t.user_id
            WHERE u.id = %s
        """, (user_id,))
        teacher = cursor.fetchone()

    cursor.close()
    conn.close()

    return render_template(
        "GV/Ds_don_nghi.html",
        don_nghi_list=don_nghi_list,
        role=role,
        teacher=teacher,
        ten=teacher["name"] if teacher else session.get("username", "Giáo viên")
    )

@app.route("/xuat_excel_don_nghi")
def xuat_excel_don_nghi():
    user_id = session.get("user_id")
    role = session.get("role")

    if not user_id:
        return redirect(url_for("login"))

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    base_query = """
        SELECT lr.request_id, e.full_name, c.class_name, lr.request_date, lr.start_date,
               lr.reason, lr.proof_file, lr.status
        FROM leave_requests lr
        JOIN classes c ON lr.classes_id = c.id
        JOIN enrollments e ON lr.user_id = e.student_id AND lr.classes_id = e.class_id
    """

    if role == "student":
        query = base_query + " WHERE lr.user_id = %s"
        cursor.execute(query, (user_id,))
    else:
        cursor.execute(base_query)

    don_nghi_list = cursor.fetchall()
    cursor.close()
    conn.close()

    # ✅ Tạo workbook Excel
    wb = Workbook()
    ws = wb.active
    ws.title = "Đơn nghỉ học"

    # ✅ Ghi tiêu đề cột
    ws.append(["Mã đơn", "Họ tên", "Lớp", "Ngày gửi", "Ngày nghỉ", "Lý do", "File minh chứng", "Trạng thái"])

    # ✅ Ghi dữ liệu
    for don in don_nghi_list:
        ws.append([
            don["request_id"],
            don["full_name"],
            don["class_name"],
            str(don["request_date"]),
            str(don["start_date"]),
            don["reason"],
            don["proof_file"] or "Không có",
            don["status"]
        ])

    # ✅ Xuất file Excel tạm trong RAM
    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    # ✅ Gửi file về trình duyệt
    return send_file(
        output,
        as_attachment=True,
        download_name="Danh_sach_don_nghi.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )



@app.route("/update_status/<int:request_id>", methods=["POST"])
def update_status(request_id):
    role = session.get("role")
    if role not in ["teacher", "admin"]:
        return "Bạn không có quyền duyệt!", 403

    new_status = request.form.get("status")
    if new_status not in ["Approved", "Rejected"]:
        return "Trạng thái không hợp lệ!", 400

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE leave_requests
        SET status = %s
        WHERE request_id = %s
    """, (new_status, request_id))
    conn.commit()
    cursor.close()
    conn.close()

    return redirect(url_for("Ds_don_nghi"))


@app.route("/tin_nhan/<int:nguoi_kia_id>")
def lay_tin_nhan(nguoi_kia_id):
    if "user_id" not in session:
        return jsonify({"error": "Chưa đăng nhập"}), 401

    user_id = session["user_id"]

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("""
        SELECT m.*, u1.name as ten_nguoi_gui, u2.name as ten_nguoi_nhan
        FROM messages m
        JOIN users u1 ON m.nguoi_gui_id = u1.id
        JOIN users u2 ON m.nguoi_nhan_id = u2.id
        WHERE (m.nguoi_gui_id = %s AND m.nguoi_nhan_id = %s)
           OR (m.nguoi_gui_id = %s AND m.nguoi_nhan_id = %s)
        ORDER BY m.thoi_gian ASC
    """, (user_id, nguoi_kia_id, nguoi_kia_id, user_id))

    tin_nhan = cursor.fetchall()
    cursor.close()
    conn.close()

    # Format thời gian
    for msg in tin_nhan:
        if msg['thoi_gian']:
            msg['thoi_gian'] = msg['thoi_gian'].strftime("%H:%M")

    return jsonify(tin_nhan)


@app.route("/gui_tin_nhan", methods=["POST"])
def gui_tin_nhan():
    if "user_id" not in session:
        return jsonify({"error": "Chưa đăng nhập"}), 401

    data = request.json
    user_id = session["user_id"]
    nguoi_nhan_id = data.get("nguoi_nhan_id")
    noi_dung = data.get("noi_dung", "").strip()

    if not nguoi_nhan_id or not noi_dung:
        return jsonify({"error": "Thiếu thông tin"}), 400

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO messages (nguoi_gui_id, nguoi_nhan_id, noi_dung, thoi_gian)
        VALUES (%s, %s, %s, NOW())
    """, (user_id, nguoi_nhan_id, noi_dung))

    conn.commit()
    cursor.close()
    conn.close()

    return jsonify({"success": True})


@app.route("/chat")
def chat_sinh_vien():
    if "user_id" not in session:
        return redirect("/login")

    user_id = session["user_id"]
    user_role = session.get("role")

    # ✅ Auto redirect nếu là teacher
    if user_role == "teacher":
        return redirect(f"/chat_gv/{user_id}")

    # ✅ Chỉ cho phép student
    if user_role != "student":
        return "Bạn không có quyền truy cập trang này", 403

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    # ✅ Lấy tên sinh viên đang đăng nhập
    cursor.execute("SELECT name FROM users WHERE id = %s", (user_id,))
    user = cursor.fetchone()

    # ✅ Lấy danh sách giảng viên đã từng nhắn tin
    cursor.execute("""
        SELECT DISTINCT u.id, u.name
        FROM users u
        JOIN messages m 
          ON (m.nguoi_gui_id = u.id AND m.nguoi_nhan_id = %s)
          OR (m.nguoi_nhan_id = u.id AND m.nguoi_gui_id = %s)
        WHERE u.role = 'teacher' AND u.id != %s
    """, (user_id, user_id, user_id))
    danh_sach_giang_vien = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template(
        "HS/Chat.html",
        danh_sach_giang_vien=danh_sach_giang_vien,
        user_id=user_id,
        user_name=user["name"] if user else "Sinh viên"
    )


@app.route("/api/mark_read/<int:user_id>", methods=["POST"])
def mark_read(user_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE messages
        SET da_xem = 1
        WHERE nguoi_nhan_id = %s
    """, (user_id,))
    conn.commit()
    cursor.close()
    conn.close()
    return jsonify({"success": True})


@app.route("/api/conversations/<int:user_id>")
def conversations(user_id):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    # Đếm số tin nhắn chưa đọc theo từng người gửi
    cursor.execute("""
        SELECT nguoi_gui_id, COUNT(*) AS unread_count
        FROM messages
        WHERE nguoi_nhan_id = %s AND da_xem = 0
        GROUP BY nguoi_gui_id
    """, (user_id,))
    unread_map = {row["nguoi_gui_id"]: row["unread_count"] for row in cursor.fetchall()}

    # Lấy danh sách cuộc trò chuyện (ai đã nhắn qua lại với user_id)
    cursor.execute("""
        SELECT u.id AS user_id, u.name, u.email,
               m.noi_dung AS last_message, m.thoi_gian AS last_time
        FROM users u
        JOIN (
            SELECT CASE
                       WHEN nguoi_gui_id = %s THEN nguoi_nhan_id
                       ELSE nguoi_gui_id
                   END AS other_id,
                   MAX(id) AS last_msg_id
            FROM messages
            WHERE nguoi_gui_id = %s OR nguoi_nhan_id = %s
            GROUP BY other_id
        ) t ON u.id = t.other_id
        JOIN messages m ON m.id = t.last_msg_id
        ORDER BY m.thoi_gian DESC
    """, (user_id, user_id, user_id))
    conversations = cursor.fetchall()

    # Gắn unread_count vào từng cuộc trò chuyện
    for conv in conversations:
        conv["unread_count"] = unread_map.get(conv["user_id"], 0)
        conv["last_time"] = conv["last_time"].isoformat()

    cursor.close()
    conn.close()

    return jsonify(conversations)



@app.route("/chat_gv/<int:giang_vien_id>")
def chat_gv(giang_vien_id):
    if "user_id" not in session:
        return redirect("/login")

    user_role = session.get("role")
    user_id = session["user_id"]

    # ✅ Auto redirect nếu là student
    if user_role == "student":
        return redirect("/chat")

    # ✅ Chỉ cho phép teacher và đúng ID
    if user_role != "teacher" or user_id != giang_vien_id:
        return "Không có quyền truy cập", 403

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    # ✅ Lấy danh sách sinh viên đã từng nhắn tin với giảng viên này
    cursor.execute("""
        SELECT DISTINCT u.id, u.name, u.avatar
        FROM users u
        JOIN messages m 
          ON (m.nguoi_gui_id = u.id AND m.nguoi_nhan_id = %s)
          OR (m.nguoi_nhan_id = u.id AND m.nguoi_gui_id = %s)
        WHERE u.role = 'student'
    """, (giang_vien_id, giang_vien_id))
    danh_sach_sinh_vien = cursor.fetchall()

    # ✅ Lấy thông tin giảng viên để hiển thị trên menu
    cursor.execute("""
        SELECT 
            u.name, u.avatar, t.major
        FROM users u
        LEFT JOIN teacher t ON u.id = t.user_id
        WHERE u.id = %s
    """, (giang_vien_id,))
    teacher = cursor.fetchone()

    cursor.close()
    conn.close()

    return render_template(
        "GV/Chat_gv.html",
        danh_sach_sinh_vien=danh_sach_sinh_vien,
        giang_vien_id=giang_vien_id,
        user_id=user_id,
        teacher=teacher,
        ten=teacher["name"] if teacher else session.get("username", "Giáo viên")
    )


period_times = [
    ("07:00","07:55"),  # Tiết 1
    ("08:00","08:55"),  # Tiết 2
    ("09:00","09:55"),  # Tiết 3
    ("10:00","10:55"),  # Tiết 4
    ("11:00","11:55"),  # Tiết 5

    ("12:30","13:25"),  # Tiết 6
    ("13:30","14:25"),  # Tiết 7
    ("14:30","15:25"),  # Tiết 8
    ("15:30","16:25"),  # Tiết 9
    ("16:30","17:25"),  # Tiết 10
]


# convert sang time object
PERIODS = [(datetime.strptime(s, "%H:%M").time(),
            datetime.strptime(e, "%H:%M").time()) for s,e in period_times]

def time_to_period(start_time, end_time):
    """Trả về (start_period, end_period) 1-based hoặc None nếu không map được."""
    st = _parse_time_field(start_time)
    et = _parse_time_field(end_time)
    if not st or not et:
        return None
    # nếu end <= start => coi như invalid (bạn có thể thay chính sách)
    if et < st:
        return None

    # start_idx = first period whose period_end >= start_time
    start_idx = next((i for i, (p_s, p_e) in enumerate(PERIODS) if p_e >= st), None)
    # end_idx = last period whose period_start <= end_time
    end_idx = next((i for i in range(len(PERIODS)-1, -1, -1) if PERIODS[i][0] <= et), None)

    if start_idx is None or end_idx is None or end_idx < start_idx:
        return None

    return start_idx + 1, end_idx + 1

def parse_time_field(t):
    """Chấp nhận: datetime.time, 'HH:MM:SS', 'HH:MM'"""
    if t is None:
        return None
    if isinstance(t, dtime):
        return t
    s = str(t).strip()
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(s, fmt).time()
        except Exception:
            pass
    return None

def is_class_in_week(class_start_date, class_weeks, current_week_start):
    """
    class_start_date: date bắt đầu lớp (datetime.date)
    class_weeks: số tuần học (int)
    current_week_start: ngày bắt đầu của tuần đang render (datetime.date)
    """
    # tuần đầu tiên lớp bắt đầu
    week0 = class_start_date.isocalendar()[1]
    year0 = class_start_date.isocalendar()[0]

    # tuần hiện tại đang render
    week_cur = current_week_start.isocalendar()[1]
    year_cur = current_week_start.isocalendar()[0]

    # tính số tuần lệch
    delta_weeks = (current_week_start - class_start_date).days // 7

    return (delta_weeks >= 0) and (delta_weeks < class_weeks)

def parse_date_field(d):
    if d is None:
        return None
    if isinstance(d, date):
        return d
    s = str(d)[:10]
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None

def periods_overlapping(start_time, end_time):
    """Return list of period indices (0-based) that overlap with [start_time, end_time)."""
    idxs = []
    for i, (ps, pe) in enumerate(PERIODS):
        # overlap if not (end <= period_start or start >= period_end)
        if not (end_time <= ps or start_time >= pe):
            idxs.append(i)
    return idxs

@app.route("/thoikhoabieu")
def thoikhoabieu():
    user_id = session.get("user_id")
    role = session.get("role")
    if not user_id:
        return redirect(url_for("login"))
    if role != "student":
        flash("Chỉ sinh viên mới được xem thời khóa biểu!", "danger")
        return redirect(url_for("student_dashboard"))

    week_offset = int(request.args.get("week_offset", 0))
    today = date.today()
    start_of_week = today - timedelta(days=today.weekday()) + timedelta(weeks=week_offset)  # thứ 2
    end_of_week = start_of_week + timedelta(days=6)

    # fetch classes the student is enrolled in
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT 
            c.id, c.class_name, c.room, c.day_of_week, 
            c.start_time, c.end_time, c.start_date, c.weeks,
            t.name AS teacher_name
        FROM enrollments e
        JOIN classes c ON e.class_id = c.id
        LEFT JOIN users t ON c.teacher_id = t.id
        WHERE e.student_id = %s
    """, (user_id,))
    raw = cursor.fetchall()
    cursor.close()
    conn.close()

    # normalize + filter + map to periods
    normalized = []
    day_map = {
        "Thứ 2": 2, "Thứ 3": 3, "Thứ 4": 4, "Thứ 5": 5,
        "Thứ 6": 6, "Thứ 7": 7, "Chủ Nhật": 8,
        "Thu 2": 2, "Thu 3": 3, "Thu 4": 4, "Thu 5": 5, "Thu 6": 6, "Thu 7": 7, "CN": 8
    }

    for r in raw:
        # normalize day
        dow = r.get("day_of_week")
        if isinstance(dow, str):
            dow_clean = dow.strip()
            day_num = day_map.get(dow_clean)
        else:
            try:
                day_num = int(dow)
            except Exception:
                day_num = None

        if not day_num:
            continue

        # parse times
        st = parse_time_field(r.get("start_time"))
        et = parse_time_field(r.get("end_time"))
        if not st or not et or et <= st:
            continue

        # parse start_date và weeks
        sdate = parse_date_field(r.get("start_date"))
        weeks = int(r.get("weeks") or 0)
        if not sdate or weeks <= 0:
            continue

        # tính tuần hiện tại (so với start_date)
        delta_weeks = (start_of_week - sdate).days // 7

        # nếu tuần hiện tại < 0 (chưa bắt đầu) hoặc >= weeks (hết môn) → bỏ qua
        if delta_weeks < 0 or delta_weeks >= weeks:
            continue

        # tìm tiết học
        idxs = periods_overlapping(st, et)
        if not idxs:
            continue

        start_period = min(idxs) + 1
        end_period = max(idxs) + 1
        duration = end_period - start_period + 1

        normalized.append({
            "id": r.get("id"),
            "class_name": r.get("class_name"),
            "room": r.get("room"),
            "day": day_num,
            "start_time": st.strftime("%H:%M"),
            "end_time": et.strftime("%H:%M"),
            "start_period": start_period,
            "end_period": end_period,
            "duration": duration,
            "teacher_name": r.get("teacher_name")  # thêm dòng này
        })

    # build grid: grid[period][day] with period 1..15 and day 2..8
    grid = {i: {d: None for d in range(2,9)} for i in range(1, len(PERIODS)+1)}
    for cls in normalized:
        d = cls["day"]
        sp = cls["start_period"]
        ep = cls["end_period"]
        # place
        for p in range(sp, ep+1):
            if p == sp:
                grid[p][d] = cls
            else:
                grid[p][d] = "SPAN"  # marker: covered by previous rowspan

    # debug print if needed
    # print("GRID:", grid)
    return render_template(
        "HS/thoikhoabieu.html",
        grid=grid,
        week_start=start_of_week.strftime("%d/%m/%Y"),
        week_end=end_of_week.strftime("%d/%m/%Y"),
        week_offset=week_offset
    )


# Xem buổi học -> danh sách sinh viên + trạng thái điểm danh
# Xem lớp: hiển thị các buổi học
@app.route("/xemlop/<int:class_id>")
def xem_lop(class_id):
    if "role" not in session or session["role"] != "teacher":
        return redirect(url_for("login"))

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    # Lấy thông tin lớp
    cursor.execute("SELECT class_name FROM classes WHERE id = %s", (class_id,))
    lop = cursor.fetchone()

    # Lấy danh sách buổi học
    cursor.execute("""
        SELECT id, session_number, date, start_time, end_time
        FROM sessions
        WHERE class_id = %s
        ORDER BY session_number ASC
    """, (class_id,))
    sessions = cursor.fetchall()

    # Format ngày tháng
    for s in sessions:
        if s["date"]:
            s["date"] = s["date"].strftime("%d/%m/%Y")
        s["start_time"] = str(s["start_time"])
        s["end_time"] = str(s["end_time"])

    conn.close()
    return render_template("GV/xemlop.html", lop=lop, sessions=sessions)


@app.route("/api/buoi_hoc/<int:session_id>/sinhvien")
def get_sinhvien_theo_buoi(session_id):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    # Lấy danh sách sinh viên điểm danh theo buổi học
    cursor.execute("""
        SELECT 
            e.id AS enrollment_id,      -- 👈 thêm dòng này để frontend có thể gửi đúng ID
            e.mssv,
            e.full_name,
            ar.date,
            ar.status_in,
            ar.time_in,
            ar.status_out,
            ar.time_out,
            ar.score
        FROM attendance_records ar
        JOIN enrollments e ON ar.enrollment_id = e.id
        WHERE ar.session_id = %s
        ORDER BY e.full_name ASC
    """, (session_id,))

    data = cursor.fetchall()
    conn.close()

    # Xử lý dữ liệu null và định dạng
    for row in data:
        row["date"] = row["date"].strftime("%d/%m/%Y") if row["date"] else "—"
        row["time_in"] = row["time_in"].strftime("%H:%M:%S") if row["time_in"] else "—"
        row["time_out"] = row["time_out"].strftime("%H:%M:%S") if row["time_out"] else "—"
        row["status_in"] = row["status_in"] if row["status_in"] else "none"
        row["status_out"] = row["status_out"] if row["status_out"] else "not_checked_out"
        row["score"] = float(row["score"]) if row["score"] is not None else 0.0

    return jsonify(data)




# Hiển thị danh sách users
@app.route("/admin/users")
def admin_users():
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM users")
    users = cursor.fetchall()
    conn.close()
    return render_template("Admin/QL_user.html", users=users)


# Xóa user
@app.route("/admin/users/delete/<int:id>")
def delete_user(id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM users WHERE id = %s", (id,))
    conn.commit()
    conn.close()
    return redirect(url_for("admin_users"))

# Sửa user
@app.route("/admin/users/edit/<int:id>", methods=["POST"])
def edit_user(id):
    name = request.form["name"]
    username = request.form["username"]
    role = request.form["role"]

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET name=%s, username=%s, role=%s WHERE id=%s",
                   (name, username, role, id))
    conn.commit()
    conn.close()
    return redirect(url_for("admin_users"))

# Trang danh sách lớp học
@app.route("/admin/classes")
def admin_classes():
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT c.id, c.class_name, c.room, c.day_of_week, 
               c.start_time, c.end_time, c.max_students, 
               c.start_date, c.weeks, 
               t.name AS teacher_name
        FROM classes c
        JOIN users t ON c.teacher_id = t.id
    """)
    classes = cursor.fetchall()
    conn.close()
    return render_template("Admin/QL_lophoc.html", classes=classes)


@app.route("/admin/classes/delete/<int:id>", methods=["POST"])
def admin_delete_class(id):
    conn = get_connection()
    cursor = conn.cursor()

    # Chỉ cần xóa class, MySQL sẽ tự động xóa sessions + enrollments liên quan
    cursor.execute("DELETE FROM classes WHERE id = %s", (id,))

    conn.commit()
    conn.close()
    flash("Xóa lớp học thành công (bao gồm cả buổi học và enrollments liên quan)!", "success")
    return redirect(url_for("admin_classes"))


@app.route("/admin/classes/update/<int:id>", methods=["POST"])
def admin_update_class(id):
    try:
        data = request.get_json(force=True)  # bắt JSON
    except Exception as e:
        return jsonify({"success": False, "error": "Invalid JSON: " + str(e)}), 400

    conn = get_connection()
    cursor = conn.cursor()

    try:
        # Lấy teacher_id: ưu tiên teacher_id nếu có, nếu không thì map từ teacher_name
        teacher_id = None
        if "teacher_id" in data and data["teacher_id"]:
            try:
                teacher_id = int(data["teacher_id"])
            except:
                return jsonify({"success": False, "message": "teacher_id phải là số"}), 400
        elif "teacher_name" in data and data["teacher_name"]:
            cursor.execute("SELECT id FROM users WHERE name = %s", (data["teacher_name"],))
            row = cursor.fetchone()
            if not row:
                return jsonify({"success": False, "message": "Không tìm thấy giảng viên: " + data["teacher_name"]}), 400
            # fetchone trả tuple (id,) hoặc dict tuỳ driver; handle cả hai
            teacher_id = row[0] if isinstance(row, (list, tuple)) else row.get("id") if isinstance(row, dict) else row

        if teacher_id is None:
            return jsonify({"success": False, "message": "Thiếu thông tin giảng viên (teacher_id hoặc teacher_name)"}), 400

        cursor.execute("""
            UPDATE classes
            SET class_name=%s, teacher_id=%s, room=%s, day_of_week=%s,
                start_time=%s, end_time=%s, max_students=%s,
                start_date=%s, weeks=%s
            WHERE id=%s
        """, (
            data.get("class_name"),
            teacher_id,
            data.get("room"),
            data.get("day_of_week"),
            data.get("start_time"),
            data.get("end_time"),
            data.get("max_students"),
            data.get("start_date"),
            data.get("weeks"),
            id
        ))
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        conn.rollback()
        # trả JSON chi tiết lỗi để frontend debug (ở production bạn có thể hide chi tiết)
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        cursor.close()
        conn.close()


@app.route("/admin/baocao")
def admin_baocao():
    # Nếu là AJAX thì trả về JSON
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        filter_type = request.args.get("filter", "all")
        date_str = request.args.get("date")  # yyyy-mm-dd

        conn = get_connection()
        cursor = conn.cursor()

        # --- Tổng users (không filter)
        cursor.execute("SELECT COUNT(*) FROM users")
        total_users = cursor.fetchone()[0]

        # --- Tổng classes theo filter
        where_classes = ""
        params_classes = []
        if filter_type == "day" and date_str:
            where_classes = "WHERE DATE(start_date) = %s"
            params_classes = [date_str]
        elif filter_type == "week" and date_str:
            where_classes = "WHERE YEARWEEK(start_date, 1) = YEARWEEK(%s, 1)"
            params_classes = [date_str]
        elif filter_type == "month" and date_str:
            where_classes = "WHERE YEAR(start_date) = YEAR(%s) AND MONTH(start_date) = MONTH(%s)"
            params_classes = [date_str, date_str]

        cursor.execute(f"SELECT COUNT(*) FROM classes {where_classes}", params_classes)
        total_classes = cursor.fetchone()[0]

        # --- Tổng sessions theo filter (nếu có bảng sessions)
        try:
            where_sessions = ""
            params_sessions = []
            if filter_type == "day" and date_str:
                where_sessions = "WHERE DATE(date) = %s"
                params_sessions = [date_str]
            elif filter_type == "week" and date_str:
                where_sessions = "WHERE YEARWEEK(date, 1) = YEARWEEK(%s, 1)"
                params_sessions = [date_str]
            elif filter_type == "month" and date_str:
                where_sessions = "WHERE YEAR(date) = YEAR(%s) AND MONTH(date) = MONTH(%s)"
                params_sessions = [date_str, date_str]

            cursor.execute(f"SELECT COUNT(*) FROM sessions {where_sessions}", params_sessions)
            total_sessions = cursor.fetchone()[0]
        except:
            total_sessions = 0

        # --- Tổng attendance_records (dùng time_in)
        where_attendance = ""
        params_attendance = []
        if filter_type == "day" and date_str:
            where_attendance = "WHERE DATE(time_in) = %s"
            params_attendance = [date_str]
        elif filter_type == "week" and date_str:
            where_attendance = "WHERE YEARWEEK(time_in, 1) = YEARWEEK(%s, 1)"
            params_attendance = [date_str]
        elif filter_type == "month" and date_str:
            where_attendance = "WHERE YEAR(time_in) = YEAR(%s) AND MONTH(time_in) = MONTH(%s)"
            params_attendance = [date_str, date_str]

        cursor.execute(f"SELECT COUNT(*) FROM attendance_records {where_attendance}", params_attendance)
        total_attendance = cursor.fetchone()[0]

        # --- Biểu đồ điểm danh 7 ngày gần nhất
        cursor.execute("""
            SELECT DATE(time_in) AS ngay, COUNT(*) AS so_luong
            FROM attendance_records
            WHERE time_in >= DATE_SUB(CURDATE(), INTERVAL 7 DAY)
            GROUP BY DATE(time_in)
            ORDER BY DATE(time_in)
        """)
        attendance_rows = cursor.fetchall()
        attendance_labels = [str(r[0]) for r in attendance_rows]
        attendance_counts = [r[1] for r in attendance_rows]

        # --- Biểu đồ số lớp theo tháng (dùng created_at của classes)
        cursor.execute("""
            SELECT MONTH(created_at) AS thang, COUNT(*) AS so_luong
            FROM classes
            GROUP BY MONTH(created_at)
            ORDER BY thang
        """)
        class_rows = cursor.fetchall()
        class_labels = [f"Tháng {r[0]}" for r in class_rows]
        class_counts = [r[1] for r in class_rows]

        conn.close()

        return jsonify({
            "totalUsers": total_users,
            "totalClasses": total_classes,
            "totalSessions": total_sessions,
            "totalAttendance": total_attendance,
            "attendanceChartLabels": attendance_labels,
            "attendanceChartData": attendance_counts,
            "classChartLabels": class_labels,
            "classChartData": class_counts
        })
    # Nếu không phải AJAX thì render template HTML
    return render_template("Admin/Baocao.html")


@app.route("/admin/caidat", methods=["GET"])
def admin_settings():
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM system_settings ORDER BY category, id")
    settings = cursor.fetchall()
    cursor.close()
    conn.close()
    return render_template("Admin/Caidat.html", settings=settings)

@app.route("/admin/settings/save", methods=["POST"])
def save_settings():
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    for key, value in request.form.items():
        if key.startswith("setting_"):
            setting_id = key.split("_")[1]
            cursor.execute("""
                UPDATE system_settings
                SET setting_value=%s, updated_by=%s, updated_at=NOW()
                WHERE id=%s AND is_editable=1
            """, (value, session.get("user_id") or 1, setting_id))
    conn.commit()
    cursor.close()
    conn.close()
    flash("Cập nhật cấu hình thành công!", "success")
    return redirect(url_for("admin_settings"))

@app.route("/admin/settings/add", methods=["POST"])
def add_setting():
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    category = request.form.get("category")
    setting_key = request.form.get("setting_key")
    setting_name = request.form.get("setting_name")
    setting_value = request.form.get("setting_value")
    description = request.form.get("description")
    note = request.form.get("note")

    # ✅ Kiểm tra trùng setting_key
    cursor.execute("SELECT id FROM system_settings WHERE setting_key = %s", (setting_key,))
    exists = cursor.fetchone()
    if exists:
        flash(f"Khóa cài đặt '{setting_key}' đã tồn tại. Vui lòng nhập khóa khác!", "danger")
        cursor.close()
        conn.close()
        return redirect(url_for("admin_settings"))

    # ✅ Thêm mới
    cursor.execute("""
        INSERT INTO system_settings 
        (category, setting_key, setting_value, description, note, is_editable, created_by, created_at)
        VALUES (%s, %s, %s, %s, %s, 1, %s, NOW())
    """, (
        category, setting_key, setting_value,
        description, note, session.get("user_id") or 1
    ))

    conn.commit()
    cursor.close()
    conn.close()
    flash("Thêm cấu hình mới thành công!", "success")
    return redirect(url_for("admin_settings"))


@app.route("/admin/settings/edit/<int:id>", methods=["POST"])
def edit_setting(id):
    category = request.form["category"]
    key = request.form["setting_key"]
    value = request.form["setting_value"]
    description = request.form.get("description", "")
    note = request.form.get("note", "")
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE system_settings
        SET category=%s, setting_key=%s, setting_value=%s, description=%s, note=%s, updated_at=NOW()
        WHERE id=%s
    """, (category, key, name, value, description, note, id))
    conn.commit()
    cursor.close()
    conn.close()
    flash("Chỉnh sửa cấu hình thành công!", "info")
    return redirect(url_for("admin_settings"))

@app.route("/admin/settings/delete/<int:id>", methods=["POST"])
def delete_setting(id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM system_settings WHERE id=%s", (id,))
    conn.commit()
    cursor.close()
    conn.close()
    flash("Đã xóa cấu hình thành công!", "danger")
    return redirect(url_for("admin_settings"))


@app.route("/gioithieu")
def gioithieu():
    return render_template("HS/gioithieu.html")

UPLOAD_FOLDER1 = os.path.join("static", "avatars")
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif"}

app.config["UPLOAD_FOLDER1"] = UPLOAD_FOLDER1
os.makedirs(app.config["UPLOAD_FOLDER1"], exist_ok=True)

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS
@app.route("/info_canhan", methods=["GET", "POST"])
def info_canhan():
    user_id = session.get("user_id")
    if not user_id:
        flash("Bạn cần đăng nhập để xem thông tin cá nhân.", "warning")
        return redirect(url_for("login"))

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # --- Nếu người dùng gửi form ---
        if request.method == "POST":
            # ==============
            # CẬP NHẬT USERS
            # ==============
            if "update_user" in request.form:
                name = request.form.get("name")
                username = request.form.get("username")
                email = request.form.get("email")

                # --- Xử lý file upload (ảnh đại diện) ---
                file = request.files.get("avatar")
                if file and allowed_file(file.filename):
                    filename = secure_filename(file.filename)
                    save_path = os.path.join(app.config["UPLOAD_FOLDER1"], f"user_{user_id}_{filename}")
                    file.save(save_path)

                    avatar_path = f"avatars/user_{user_id}_{filename}"

                    cursor.execute("""
                        UPDATE users
                        SET avatar = %s
                        WHERE id = %s
                    """, (avatar_path, user_id))

                # --- Cập nhật thông tin user ---
                cursor.execute("""
                    UPDATE users
                    SET name=%s, username=%s, email=%s
                    WHERE id=%s
                """, (name, username, email, user_id))

                conn.commit()
                flash("✅ Cập nhật thông tin tài khoản thành công.", "success")

            # ===================
            # CẬP NHẬT SINH VIÊN
            # ===================
            elif "update_student" in request.form:
                phone = request.form.get("phone")
                birthday = request.form.get("birthday")
                gender = request.form.get("gender")
                school = request.form.get("school")
                class_name = request.form.get("class")
                course_year = request.form.get("course_year")
                major = request.form.get("major")

                # Kiểm tra sinh viên đã có record chưa
                cursor.execute("SELECT * FROM student WHERE user_id = %s", (user_id,))
                existing = cursor.fetchone()

                if existing:
                    cursor.execute("""
                        UPDATE student
                        SET phone=%s, birthday=%s, gender=%s, school=%s, class=%s, course_year=%s, major=%s
                        WHERE user_id=%s
                    """, (phone, birthday, gender, school, class_name, course_year, major, user_id))
                else:
                    cursor.execute("""
                        INSERT INTO student (user_id, phone, birthday, gender, school, class, course_year, major)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    """, (user_id, phone, birthday, gender, school, class_name, course_year, major))

                conn.commit()
                flash("✅ Cập nhật thông tin sinh viên thành công.", "success")

        # --- Lấy lại dữ liệu sau khi cập nhật ---
        cursor.execute("""
            SELECT id, name, username, email, avatar
            FROM users
            WHERE id = %s
        """, (user_id,))
        user = cursor.fetchone()

        cursor.execute("""
            SELECT phone, birthday, gender, school, class, course_year, major
            FROM student
            WHERE user_id = %s
        """, (user_id,))
        student = cursor.fetchone() or {}

    finally:
        cursor.close()
        conn.close()

    if not user:
        flash("Không tìm thấy thông tin người dùng.", "danger")
        return redirect(url_for("login"))

    return render_template("HS/Info_canhan.html", user=user, student=student)



def send_otp_email(to_email, otp_code):
    sender = "2124802010398@student.tdmu.edu.vn"
    app_password = "gwuu xcgy deok hjfz"  # App Password 16 ký tự

    subject = "Mã OTP đổi mật khẩu"
    body = f"Mã OTP của bạn là: {otp_code}"

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body, subtype="plain", charset="utf-8")

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(sender, app_password)
            # send_message tự xử lý encoding cho headers + body
            server.send_message(msg)
        print("✅ Email gửi thành công tới", to_email)
    except Exception as e:
        print("❌ Lỗi gửi email:", e)
        raise

@app.route("/doi_mat_khau", methods=["GET", "POST"])
def doi_mat_khau():
    user_id = session.get("user_id")
    if not user_id:
        flash("Vui lòng đăng nhập trước.", "warning")
        return redirect(url_for("login"))

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("SELECT * FROM users WHERE id=%s", (user_id,))
    user = cursor.fetchone()

    if not user:
        flash("Không tìm thấy tài khoản.", "danger")
        return redirect(url_for("login"))

    # Giá trị mặc định cho form
    form_data = {
        "old_password": "",
        "new_password": "",
        "confirm_password": "",
        "otp": ""
    }

    if request.method == "POST":
        action = request.form.get("action")

        # Cập nhật form_data từ dữ liệu người dùng nhập
        form_data["old_password"] = request.form.get("old_password", "")
        form_data["new_password"] = request.form.get("new_password", "")
        form_data["confirm_password"] = request.form.get("confirm_password", "")
        form_data["otp"] = request.form.get("otp", "")

        if action == "send_otp":
            if not user.get("email"):
                flash("Tài khoản chưa có email. Vui lòng cập nhật email trước.", "danger")
            else:
                otp = str(random.randint(100000, 999999))
                session["otp"] = otp
                session["otp_user"] = user_id

                print("👉 Debug: OTP tạo ra =", otp)
                print("👉 Debug: Gửi tới email =", user["email"])

                try:
                    send_otp_email(user["email"], otp)
                    flash("✅ Đã gửi mã OTP đến email của bạn.", "success")
                except Exception as e:
                    print("❌ Debug: Lỗi khi gửi email =", e)
                    flash(f"Lỗi gửi email: {e}", "danger")

        elif action == "change_password":
            old_password = form_data["old_password"]
            new_password = form_data["new_password"]
            confirm_password = form_data["confirm_password"]
            otp = form_data["otp"]

            print("👉 Debug: OTP nhập vào =", otp)
            print("👉 Debug: OTP trong session =", session.get("otp"))
            print("👉 Debug: user_id trong session =", session.get("otp_user"))
            print("👉 Debug: user_id thực =", user_id)

            if not check_password_hash(user["password"], old_password):
                flash("❌ Mật khẩu cũ không đúng.", "danger")
            elif new_password != confirm_password:
                flash("❌ Mật khẩu mới không khớp.", "danger")
            elif session.get("otp") != otp or session.get("otp_user") != user_id:
                flash("❌ Mã OTP không hợp lệ hoặc đã hết hạn.", "danger")
            else:
                new_hashed = generate_password_hash(new_password)
                cursor.execute("UPDATE users SET password=%s WHERE id=%s", (new_hashed, user_id))
                conn.commit()
                flash("✅ Đổi mật khẩu thành công!", "success")
                session.pop("otp", None)
                session.pop("otp_user", None)
                cursor.close()
                conn.close()
                return redirect(url_for("info_canhan"))

    cursor.close()
    conn.close()
    return render_template("doi_mat_khau.html", form_data=form_data)

UPLOAD_FOLDER1 = os.path.join("static", "avatars")
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif"}

app.config["UPLOAD_FOLDER1"] = UPLOAD_FOLDER1
os.makedirs(app.config["UPLOAD_FOLDER1"], exist_ok=True)

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


@app.route("/gv_caidat", methods=["GET", "POST"])
def gv_caidat():
    user_id = session.get("user_id")
    if not user_id:
        flash("Vui lòng đăng nhập trước.")
        return redirect(url_for('login'))

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    if request.method == "POST":
        # 🧩 Lấy dữ liệu form an toàn
        def safe_get(field, default=None):
            value = request.form.get(field)
            if value is None or value.strip() == "":
                return default
            return value.strip()

        name = safe_get("name", "")
        username = safe_get("username", "")
        email = safe_get("email", "")
        phone = safe_get("phone", "")
        major = safe_get("major", "")
        birthday = safe_get("birthday", None)  # để None nếu trống
        gender = safe_get("gender", None)
        school = safe_get("school", "")
        education = safe_get("education", "")

        print("Form data:", name, username, email, phone, major, birthday, gender, school, education)
        print("User ID:", user_id)

        try:
            # ✅ Cập nhật bảng users
            cursor.execute("""
                UPDATE users 
                SET name=%s, username=%s, email=%s 
                WHERE id=%s
            """, (name, username, email, user_id))

            # ✅ Kiểm tra giảng viên tồn tại chưa
            cursor.execute("SELECT teacher_id FROM teacher WHERE user_id=%s", (user_id,))
            teacher_exists = cursor.fetchone()
            print("Teacher exists:", teacher_exists)

            if teacher_exists:
                # ✅ Update nếu đã có
                cursor.execute("""
                    UPDATE teacher 
                    SET phone=%s, major=%s, birthday=%s, gender=%s, school=%s, education=%s
                    WHERE user_id=%s
                """, (phone, major, birthday, gender, school, education, user_id))
            else:
                # ✅ Insert nếu chưa có
                cursor.execute("""
                    INSERT INTO teacher (user_id, phone, major, birthday, gender, school, education)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (user_id, phone, major, birthday, gender, school, education))

            conn.commit()
            flash("Cập nhật thông tin thành công!", "success")

        except Exception as e:
            import traceback
            print("🔥 DB update error:", e)
            traceback.print_exc()
            flash("Lỗi khi cập nhật thông tin.", "danger")

        finally:
            conn.close()
            return redirect(url_for('gv_caidat'))  # POST -> redirect tránh reload form

    # 🧩 GET: lấy dữ liệu để hiển thị lại
    cursor.execute("""
        SELECT 
            u.id, u.name, u.username, u.email,
            t.phone, t.major, t.birthday, t.gender, t.school, t.education, u.avatar
        FROM users u
        LEFT JOIN teacher t ON u.id = t.user_id
        WHERE u.id = %s
    """, (user_id,))
    teacher = cursor.fetchone()

    conn.close()
    return render_template("GV/caidat.html", teacher=teacher)





@app.route("/gv_caidat_save_avatar", methods=["POST"])
def gv_caidat_save_avatar():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"success": False, "error": "Chưa đăng nhập"})

    avatar_file = request.files.get("avatar")
    avatar_url = request.form.get("avatar_url")
    avatar_filename = None

    try:
        if avatar_file and allowed_file(avatar_file.filename):
            ext = avatar_file.filename.rsplit(".", 1)[1].lower()
            original_name = avatar_file.filename.rsplit(".", 1)[0]
            avatar_filename = f"avatars/user_{user_id}_{original_name}.{ext}"
            save_path = os.path.join(app.config["UPLOAD_FOLDER1"], f"user_{user_id}_{original_name}.{ext}")
            avatar_file.save(save_path)
        elif avatar_url:
            avatar_filename = avatar_url
        else:
            return jsonify({"success": False, "error": "Không có file hoặc URL"})

        # --- Cập nhật DB ---
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET avatar=%s WHERE id=%s", (avatar_filename, user_id))
        conn.commit()
        conn.close()

        return jsonify({"success": True, "avatar": avatar_filename})

    except Exception as e:
        print(e)
        return jsonify({"success": False, "error": str(e)})

@app.route("/thong_ke")
def gv_thongke():
    # Nếu là AJAX thì trả về JSON
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        filter_type = request.args.get("filter", "all")
        date_str = request.args.get("date")  # yyyy-mm-dd

        conn = get_connection()
        cursor = conn.cursor()

        # --- Tổng users (không filter)
        cursor.execute("SELECT COUNT(*) FROM users")
        total_users = cursor.fetchone()[0]

        # --- Tổng classes theo filter
        where_classes = ""
        params_classes = []
        if filter_type == "day" and date_str:
            where_classes = "WHERE DATE(start_date) = %s"
            params_classes = [date_str]
        elif filter_type == "week" and date_str:
            where_classes = "WHERE YEARWEEK(start_date, 1) = YEARWEEK(%s, 1)"
            params_classes = [date_str]
        elif filter_type == "month" and date_str:
            where_classes = "WHERE YEAR(start_date) = YEAR(%s) AND MONTH(start_date) = MONTH(%s)"
            params_classes = [date_str, date_str]

        cursor.execute(f"SELECT COUNT(*) FROM classes {where_classes}", params_classes)
        total_classes = cursor.fetchone()[0]

        # --- Tổng sessions theo filter (nếu có bảng sessions)
        try:
            where_sessions = ""
            params_sessions = []
            if filter_type == "day" and date_str:
                where_sessions = "WHERE DATE(date) = %s"
                params_sessions = [date_str]
            elif filter_type == "week" and date_str:
                where_sessions = "WHERE YEARWEEK(date, 1) = YEARWEEK(%s, 1)"
                params_sessions = [date_str]
            elif filter_type == "month" and date_str:
                where_sessions = "WHERE YEAR(date) = YEAR(%s) AND MONTH(date) = MONTH(%s)"
                params_sessions = [date_str, date_str]

            cursor.execute(f"SELECT COUNT(*) FROM sessions {where_sessions}", params_sessions)
            total_sessions = cursor.fetchone()[0]
        except:
            total_sessions = 0

        # --- Tổng attendance_records (dùng time_in)
        where_attendance = ""
        params_attendance = []
        if filter_type == "day" and date_str:
            where_attendance = "WHERE DATE(time_in) = %s"
            params_attendance = [date_str]
        elif filter_type == "week" and date_str:
            where_attendance = "WHERE YEARWEEK(time_in, 1) = YEARWEEK(%s, 1)"
            params_attendance = [date_str]
        elif filter_type == "month" and date_str:
            where_attendance = "WHERE YEAR(time_in) = YEAR(%s) AND MONTH(time_in) = MONTH(%s)"
            params_attendance = [date_str, date_str]

        cursor.execute(f"SELECT COUNT(*) FROM attendance_records {where_attendance}", params_attendance)
        total_attendance = cursor.fetchone()[0]

        # --- Biểu đồ điểm danh 7 ngày gần nhất
        cursor.execute("""
            SELECT DATE(time_in) AS ngay, COUNT(*) AS so_luong
            FROM attendance_records
            WHERE time_in >= DATE_SUB(CURDATE(), INTERVAL 7 DAY)
            GROUP BY DATE(time_in)
            ORDER BY DATE(time_in)
        """)
        attendance_rows = cursor.fetchall()
        attendance_labels = [str(r[0]) for r in attendance_rows]
        attendance_counts = [r[1] for r in attendance_rows]

        # --- Biểu đồ số lớp theo tháng
        cursor.execute("""
            SELECT MONTH(created_at) AS thang, COUNT(*) AS so_luong
            FROM classes
            GROUP BY MONTH(created_at)
            ORDER BY thang
        """)
        class_rows = cursor.fetchall()
        class_labels = [f"Tháng {r[0]}" for r in class_rows]
        class_counts = [r[1] for r in class_rows]

        conn.close()

        return jsonify({
            "totalUsers": total_users,
            "totalClasses": total_classes,
            "totalSessions": total_sessions,
            "totalAttendance": total_attendance,
            "attendanceChartLabels": attendance_labels,
            "attendanceChartData": attendance_counts,
            "classChartLabels": class_labels,
            "classChartData": class_counts
        })

    # Nếu không phải AJAX thì render template HTML
    if "role" not in session or session["role"] != "teacher":
        return redirect(url_for("login"))

    teacher_id = session["user_id"]
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    # ✅ Lấy thông tin giáo viên
    cursor.execute("""
        SELECT 
            u.name, u.avatar, t.major
        FROM users u
        LEFT JOIN teacher t ON u.id = t.user_id
        WHERE u.id = %s
    """, (teacher_id,))
    teacher = cursor.fetchone()

    conn.close()

    return render_template(
        "GV/thong_ke.html",
        teacher=teacher,
        ten=teacher["name"] if teacher else session.get("username", "Giáo viên")
    )


@app.route("/menu_gv")
def gv_menu():
    user_id = session.get("user_id")
    if not user_id:
        flash("Vui lòng đăng nhập trước.")
        return redirect(url_for('login'))

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("""
        SELECT 
            u.id, u.name, u.username, u.email, u.avatar,
            t.major
        FROM users u
        LEFT JOIN teacher t ON u.id = t.user_id
        WHERE u.id = %s
    """, (user_id,))
    teacher = cursor.fetchone()

    conn.close()

    return render_template("menu_gv.html", teacher=teacher)


app.config.update(
    MAIL_SERVER='smtp.gmail.com',
    MAIL_PORT=587,
    MAIL_USE_TLS=True,
    MAIL_USERNAME='2124802010398@student.tdmu.edu.vn',      # đổi thành email của bạn
    MAIL_PASSWORD='jvrb fwbu rvum znvz'         # dùng App Password của Gmail
)
mail = Mail(app)

# ✅ Tạo token serializer
s = URLSafeTimedSerializer(app.secret_key)

# ✅ Kết nối MySQL

# ---------- QUÊN MẬT KHẨU ----------
@app.route("/quen_mat_khau", methods=["GET", "POST"])
def quen_mat_khau():
    if request.method == "POST":
        email = request.form["email"]

        conn = get_connection()
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM users WHERE email=%s", (email,))
        user = cur.fetchone()

        if user:
            token = s.dumps(email, salt='reset-password')
            expire_time = datetime.now() + timedelta(hours=1)
            cur.execute("UPDATE users SET reset_token=%s, token_expire=%s WHERE email=%s",
                        (token, expire_time, email))
            conn.commit()

            # ✅ Thay bằng link public của ngrok
            ngrok_url = "https://diemdanh-flask-jf27.onrender.com"
            reset_url = f"{ngrok_url}/dat_lai_mat_khau/{token}"

            # ---------- GỬI EMAIL ----------
            msg = Message('Đặt lại mật khẩu',
                          sender='youremail@gmail.com',
                          recipients=[email])
            msg.body = f"Nhấp vào liên kết để đặt lại mật khẩu: {reset_url}"
            mail.send(msg)

            flash("✅ Đã gửi email khôi phục mật khẩu, vui lòng kiểm tra hộp thư!", "success")
        else:
            flash("❌ Email không tồn tại trong hệ thống!", "danger")

        cur.close()
        conn.close()
        return redirect("/quen_mat_khau")

    return render_template("quen_mat_khau.html")


# ---------- ĐẶT LẠI MẬT KHẨU ----------
@app.route("/dat_lai_mat_khau/<token>", methods=["GET", "POST"])
def dat_lai_mat_khau(token):
    try:
        email = s.loads(token, salt='reset-password', max_age=3600)
    except Exception as e:
        print(e)
        return "❌ Liên kết không hợp lệ hoặc đã hết hạn!"

    if request.method == "POST":
        new_password = request.form["password"]
        # ✅ Dùng generate_password_hash (Flask mặc định)
        hashed = generate_password_hash(new_password)

        conn = get_connection()
        cur = conn.cursor()
        cur.execute("UPDATE users SET password=%s, reset_token=NULL, token_expire=NULL WHERE email=%s",
                    (hashed, email))
        conn.commit()
        cur.close()
        conn.close()

        return "✅ Mật khẩu của bạn đã được đặt lại thành công!"

    return render_template("dat_lai_mat_khau.html")

app.config["UPLOAD_FOLDER_NEWS"] = "static/news"
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif"}


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route("/admin/news")
def admin_news_list():
    if session.get("role") != "admin":
        flash("Access denied!", "danger")
        return redirect("/")

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM news ORDER BY created_at DESC")
    news = cursor.fetchall()
    cursor.close()
    conn.close()
    return render_template("Admin/News.html", news=news)


@app.route("/admin/news/add", methods=["POST"])
def add_news():
    title = request.form["title"]
    short_description = request.form["short_description"]
    content = request.form["content"]
    source = request.form["source"]
    author = request.form["author"]
    category = request.form["category"]
    is_visible = 1 if "is_visible" in request.form else 0

    # Xử lý ảnh
    image = request.files["image"]
    filename = None
    if image and image.filename != "":
        filename = secure_filename(image.filename)
        image.save(os.path.join(app.config["UPLOAD_FOLDER_NEWS"], filename))

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO news (title, short_description, content, image, source, author, created_at, category, is_visible)
        VALUES (%s, %s, %s, %s, %s, %s, NOW(), %s, %s)
    """, (title, short_description, content, filename, source, author, category, is_visible))
    conn.commit()
    cursor.close()
    conn.close()
    flash("Thêm tin tức thành công!", "success")
    return redirect("/admin/news")


@app.route("/admin/news/update/<int:id>", methods=["POST"])
def update_news(id):
    title = request.form["title"]
    short_description = request.form["short_description"]
    content = request.form["content"]
    source = request.form["source"]
    author = request.form["author"]
    category = request.form["category"]
    is_visible = 1 if "is_visible" in request.form else 0

    image = request.files["image"]
    filename = None
    if image and image.filename != "":
        filename = secure_filename(image.filename)
        image.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))

    conn = get_connection()
    cursor = conn.cursor()

    if filename:
        cursor.execute("""
            UPDATE news SET title=%s, short_description=%s, content=%s, image=%s, source=%s,
                            author=%s, category=%s, is_visible=%s WHERE id=%s
        """, (title, short_description, content, filename, source, author, category, is_visible, id))
    else:
        cursor.execute("""
            UPDATE news SET title=%s, short_description=%s, content=%s, source=%s,
                            author=%s, category=%s, is_visible=%s WHERE id=%s
        """, (title, short_description, content, source, author, category, is_visible, id))

    conn.commit()
    cursor.close()
    conn.close()
    flash("Cập nhật tin tức thành công!", "info")
    return redirect("/admin/news")


@app.route("/admin/news/delete/<int:id>", methods=["POST"])
def delete_news(id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM news WHERE id=%s", (id,))
    conn.commit()
    cursor.close()
    conn.close()
    flash("Đã xóa tin tức!", "danger")
    return redirect("/admin/news")

# 🧠 Quản lý FAQ
@app.route('/admin/faq')
def faq_list():
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM faq ORDER BY id DESC")
    faqs = cursor.fetchall()
    cursor.close()
    conn.close()
    return render_template('admin/QL_faq.html', faqs=faqs)

@app.route('/admin/faq/add', methods=['POST'])
def faq_add():
    cau_hoi = request.form['cau_hoi']
    cau_tra_loi = request.form['cau_tra_loi']

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO faq (cau_hoi, cau_tra_loi) VALUES (%s, %s)", (cau_hoi, cau_tra_loi))
    conn.commit()
    cursor.close()
    conn.close()
    flash("Đã thêm câu hỏi FAQ!", "success")
    return redirect(url_for('faq_list'))

@app.route('/admin/faq/edit/<int:id>', methods=['POST'])
def faq_edit(id):
    cau_hoi = request.form['edit_cau_hoi']
    cau_tra_loi = request.form['edit_cau_tra_loi']

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE faq SET cau_hoi=%s, cau_tra_loi=%s WHERE id=%s", (cau_hoi, cau_tra_loi, id))
    conn.commit()
    cursor.close()
    conn.close()
    flash("Đã cập nhật FAQ!", "info")
    return redirect(url_for('faq_list'))

@app.route('/admin/faq/delete/<int:id>', methods=['POST'])
def faq_delete(id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM faq WHERE id=%s", (id,))
    conn.commit()
    cursor.close()
    conn.close()
    flash("Đã xóa câu hỏi FAQ!", "danger")
    return redirect(url_for('faq_list'))


@app.route("/tintuc")
def tintuc():
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)    # ✅ Chỉ lấy tin có is_visible = 1, sắp xếp theo ngày mới nhất
    cursor.execute("SELECT * FROM news WHERE is_visible = 1 ORDER BY created_at DESC")
    news_list = cursor.fetchall()
    cursor.close()
    return render_template("HS/tin_tuc.html", news_list=news_list)

@app.route("/tintuc/<int:news_id>")
def news_detail(news_id):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM news WHERE id = %s", (news_id,))
    news_item = cursor.fetchone()
    cursor.close()

    if not news_item:
        return "Không tìm thấy bài viết", 404

    return render_template("HS/detail_tin_tuc.html", news=news_item)

@app.route("/lich_su_nghi_phep", methods=['GET'])
def lich_su_nghi_phep():
    if "user_id" not in session:
        return redirect("/login")

    user_id = session["user_id"]

    # Lấy các tham số lọc từ form (GET)
    month = request.args.get('month')
    year = request.args.get('year')
    status = request.args.get('status')

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    # Câu query cơ bản
    query = """
        SELECT lr.request_id, lr.request_date, lr.session_date, lr.reason, lr.proof_file, lr.status
        FROM leave_requests lr
        WHERE lr.user_id = %s
    """
    params = [user_id]

    # Thêm điều kiện lọc nếu có chọn
    if month:
        query += " AND MONTH(lr.request_date) = %s"
        params.append(month)

    if year:
        query += " AND YEAR(lr.request_date) = %s"
        params.append(year)

    if status:
        query += " AND lr.status = %s"
        params.append(status)

    query += " ORDER BY lr.request_date DESC"

    cursor.execute(query, params)
    requests = cursor.fetchall()
    cursor.close()
    conn.close()

    return render_template("HS/history_nghi_phep.html", requests=requests)


@app.route("/lich_su_diem_danh", methods=['GET'])
def lich_su_diem_danh():
    if "user_id" not in session:
        return redirect("/login")

    user_id = session["user_id"]

    # ✅ Lấy giá trị lọc từ request
    month = request.args.get('month')
    year = request.args.get('year')
    status = request.args.get('status')

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    # ✅ Base query
    query = """
        SELECT 
            a.id,
            a.date,
            a.status_in,
            a.time_in,
            a.time_out,
            a.status_out,
            a.score,
            s.session_number,
            c.class_name
        FROM attendance_records a
        JOIN sessions s ON a.session_id = s.id
        JOIN enrollments e ON a.enrollment_id = e.id
        JOIN classes c ON e.class_id = c.id
        WHERE e.student_id = %s
          AND a.status_in <> 'none'
    """

    params = [user_id]

    # ✅ Thêm điều kiện theo tháng
    if month:
        query += " AND MONTH(a.date) = %s"
        params.append(month)

    # ✅ Thêm điều kiện theo năm
    if year:
        query += " AND YEAR(a.date) = %s"
        params.append(year)

    # ✅ Thêm điều kiện theo trạng thái
    if status:
        query += " AND a.status_in = %s"
        params.append(status)

    query += " ORDER BY a.date DESC"

    cursor.execute(query, params)
    attendance_list = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template(
        "HS/history_diem_danh.html",
        attendance_list=attendance_list
    )





#client = Client(api_key="AIzaSyDymlFtdZhgmaA8Jw1FQnSaENuz5GZ1cdA")  # Thay bằng API key thật


# --- Hàm lấy context từ DB ---
def get_faq_context():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT cau_hoi, cau_tra_loi FROM faq")
    rows = cursor.fetchall()
    context = "\n".join([f"Hỏi: {r[0]} - Đáp: {r[1]}" for r in rows])
    cursor.close()
    conn.close()
    return context

# --- Lấy dữ liệu sinh viên/lớp ---
def get_student_context():
    conn = get_connection()
    cursor = conn.cursor()
    query = """
        SELECT 
            u.name, 
            s.phone, 
            s.birthday, 
            s.gender, 
            s.school, 
            s.class, 
            s.course_year, 
            s.major
        FROM student s
        JOIN users u ON s.user_id = u.id
    """
    cursor.execute(query)
    rows = cursor.fetchall()

    if not rows:
        return "Hiện chưa có thông tin sinh viên nào trong hệ thống."

    # Tạo mô tả ngắn gọn từng sinh viên
    context = "\n".join([
        f"Sinh viên: {r[0]}, Giới tính: {r[3]}, Ngành: {r[7]}, "
        f"Trường: {r[4]}, Lớp: {r[5]}, Khóa: {r[6]}, "
        f"SĐT: {r[1]}, Ngày sinh: {r[2]}"
        for r in rows
    ])

    cursor.close()
    conn.close()
    return context

def get_class_context():
    conn = get_connection()
    cursor = conn.cursor()
    query = """
        SELECT 
            c.class_name, 
            u.name AS teacher_name, 
            c.room, 
            c.day_of_week, 
            c.start_time, 
            c.end_time, 
            c.max_students, 
            c.start_date, 
            c.weeks
        FROM classes c
        JOIN users u ON c.teacher_id = u.id
    """
    cursor.execute(query)
    rows = cursor.fetchall()

    # Nếu không có lớp học nào
    if not rows:
        return "Hiện chưa có lớp học nào trong hệ thống."

    # Ghép thông tin từng lớp thành đoạn mô tả
    context = "\n".join([
        f"Lớp: {r[0]}, Giảng viên: {r[1]}, Phòng: {r[2]}, "
        f"Lịch: {r[3]} từ {r[4]} đến {r[5]}, "
        f"Số SV tối đa: {r[6]}, Khai giảng: {r[7]}, Thời lượng: {r[8]} tuần"
        for r in rows
    ])

    cursor.close()
    conn.close()
    return context

def get_user_context():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT 
            id, name, username, email, role, avatar
        FROM users
        ORDER BY id ASC
    """)
    rows = cursor.fetchall()

    cursor.close()
    conn.close()

    if not rows:
        return "Không có người dùng nào trong hệ thống."

    # Chuyển thành dạng mô tả dễ hiểu cho AI
    context_lines = []
    for r in rows:
        user_id = r[0]
        name = r[1]
        username = r[2]
        email = r[3] or "Không có email"
        role = r[4]
        avatar = r[5] or "Không có ảnh"

        context_lines.append(
            f"ID: {user_id}, Tên: {name}, Username: {username}, Email: {email}, Vai trò: {role}, Ảnh đại diện: {avatar}"
        )

    return "\n".join(context_lines)

def get_teacher_context():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT 
            t.teacher_id,
            u.name,
            u.username,
            t.phone,
            t.major,
            t.school,
            t.education,
            t.gender,
            t.birthday
        FROM teacher t
        JOIN users u ON t.user_id = u.id
        ORDER BY t.teacher_id ASC
    """)
    rows = cursor.fetchall()

    cursor.close()
    conn.close()

    if not rows:
        return "Không có giảng viên nào trong hệ thống."

    # Format dữ liệu để AI đọc hiểu tốt hơn
    context_lines = []
    for r in rows:
        teacher_id = r[0]
        name = r[1]
        username = r[2]
        phone = r[3] or "Không có"
        major = r[4] or "Không rõ"
        school = r[5] or "Không rõ"
        education = r[6] or "Không rõ"
        gender = r[7] or "Không xác định"
        birthday = r[8].strftime("%d/%m/%Y") if r[8] else "Không có dữ liệu"

        context_lines.append(
            f"Giảng viên ID: {teacher_id}, Tên: {name}, Username: {username}, "
            f"Giới tính: {gender}, Ngày sinh: {birthday}, SĐT: {phone}, "
            f"Chuyên ngành: {major}, Trường: {school}, Trình độ học vấn: {education}"
        )

    return "\n".join(context_lines)

def get_attendance_context():
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    query = """
        SELECT 
            u.name AS student_name,
            c.class_name AS class_name,
            ar.date,
            ar.status_in,
            ar.time_in,
            ar.status_out,
            ar.time_out,
            ar.score
        FROM attendance_records ar
        JOIN enrollments e ON ar.enrollment_id = e.id
        JOIN users u ON e.student_id = u.id
        JOIN classes c ON e.class_id = c.id
        ORDER BY ar.date DESC;
    """
    cursor.execute(query)
    rows = cursor.fetchall()

    cursor.close()
    conn.close()

    if not rows:
        return "Hiện chưa có bản ghi điểm danh nào trong hệ thống."

    # ✅ Ghép dữ liệu thành context mô tả dễ hiểu
    context_lines = []
    for r in rows:
        line = (
            f"Sinh viên: {r['student_name']}, "
            f"Lớp: {r['class_name']}, "
            f"Ngày: {r['date']}, "
            f"Trạng thái vào: {r['status_in']}, "
            f"Giờ vào: {r['time_in']}, "
            f"Trạng thái ra: {r['status_out']}, "
            f"Giờ ra: {r['time_out']}, "
            f"Điểm chuyên cần: {r['score']}"
        )
        context_lines.append(line)

    return "\n".join(context_lines)


def get_enrollment_context():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT 
            e.id,
            e.full_name,
            e.mssv,
            e.lop,
            e.major,
            e.phone,
            e.email,
            c.class_name,
            c.teacher_id,
            u.name AS teacher_name
        FROM enrollments e
        JOIN classes c ON e.class_id = c.id
        JOIN users u ON c.teacher_id = u.id
        ORDER BY e.id ASC
    """)
    rows = cursor.fetchall()

    cursor.close()
    conn.close()

    if not rows:
        return "Không có dữ liệu đăng ký lớp học (enrollments)."

    # Format dữ liệu dễ đọc cho AI
    context_lines = []
    for r in rows:
        enrollment_id = r[0]
        full_name = r[1]
        mssv = r[2]
        lop = r[3]
        major = r[4]
        phone = r[5]
        email = r[6]
        class_name = r[7]
        teacher_id = r[8]
        teacher_name = r[9]

        context_lines.append(
            f"Đăng ký ID: {enrollment_id}, Sinh viên: {full_name} (MSSV: {mssv}, Lớp: {lop}, "
            f"Chuyên ngành: {major}, SĐT: {phone}, Email: {email}) "
            f"→ Học lớp: {class_name}, Giảng viên phụ trách: {teacher_name} (ID: {teacher_id})"
        )

    return "\n".join(context_lines)


# --- Gọi AI ---
def ask_ai(user_question, role="student"):
    # --- Lấy dữ liệu từ DB ---
    faq_context = get_faq_context()
    student_context = get_student_context()
    class_context = get_class_context()
    attendance_context = get_attendance_context()
    user_context = get_user_context()
    teacher_context = get_teacher_context()
    enrollment_context = get_enrollment_context()

    # --- Giải thích cấu trúc dữ liệu ---
    schema_explanation = """
    Hệ thống quản lý học tập có các bảng dữ liệu:
    - users: thông tin tài khoản (id, username, password, email, role, avatar)
    - students: thông tin sinh viên (id, mssv, full_name, lop, truong, major, gender, date_of_birth)
    - teachers: thông tin giảng viên (teacher_id, user_id, email, phone, major, school)
    - classes: lớp học (id, class_name, teacher_id, room, day_of_week, start_time, end_time)
    - enrollments: sinh viên đăng ký lớp học
    - attendance_records: dữ liệu điểm danh từng buổi
    - faq: câu hỏi thường gặp
    """

    # --- Ghép dữ liệu ---
    full_context = f"""
    [FAQ]
    {faq_context}

    [USERS]
    {user_context}

    [TEACHERS]
    {teacher_context}

    [STUDENTS]
    {student_context}

    [CLASSES]
    {class_context}

    [ENROLLMENTS]
    {enrollment_context}

    [ATTENDANCE]
    {attendance_context}
    """

    # --- Prompt riêng theo vai trò ---
    if role == "teacher":
        role_prompt = """
        Bạn là trợ lý AI dành riêng cho **giảng viên**.
        Mục tiêu:
        - Hỗ trợ giảng viên về việc quản lý lớp, sinh viên, điểm danh, thống kê chuyên cần, v.v.
        - Có thể trả lời về cách xem danh sách sinh viên, sửa điểm danh, hoặc cách chấm điểm.
        - Nếu câu hỏi chung (như “hệ thống hoạt động sao”, “đăng nhập lỗi”) thì trả lời bình thường.
        - Trả lời bằng tiếng Việt, giọng tự nhiên, ngắn gọn, dễ hiểu.
        Ví dụ:
        - “Làm sao xem sinh viên vắng học hôm nay?”
        - “Cách thêm lớp học mới?”
        - “Làm sao xem thống kê điểm danh của lớp tôi?”
        """
    else:
        role_prompt = """
        Bạn là trợ lý AI dành cho **sinh viên**.
        Mục tiêu:
        - Giúp sinh viên tra cứu thông tin điểm danh, lịch học, lớp học, giáo viên, và các vấn đề học tập.
        - Có thể giải thích cách sử dụng website, cách xin nghỉ học, hoặc nộp đơn.
        - Trả lời bằng tiếng Việt, ngắn gọn, thân thiện, dễ hiểu.
        Ví dụ:
        - “Hôm nay tôi có tiết nào?”
        - “Điểm danh của tôi hôm nay là gì?”
        - “Làm sao xin nghỉ học?”
        """

    # --- Prompt hoàn chỉnh ---
    prompt = f"""
    {role_prompt}

    --- CẤU TRÚC DỮ LIỆU ---
    {schema_explanation}

    --- DỮ LIỆU HIỆN CÓ ---
    {full_context}

    --- CÂU HỎI ---
    {user_question}
    """

    # --- Gọi AI ---
    response = client.models.generate_content(
        model="models/gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(thinking_budget=0)
        )
    )

    return response.text




# --- Route chat ---
@app.route("/chat", methods=["GET", "POST"])
def chat():
    if request.method == "POST":
        user_msg = request.form.get("message")  # hoặc request.json['message'] nếu AJAX
        ai_reply = ask_ai(user_msg, role="student")
        return render_template("chat.html", user_msg=user_msg, ai_reply=ai_reply)
    return render_template("chat.html")

# --- Socket xử lý chat ---
@socketio.on("send_message")
def handle_message(data):
    user_msg = data["message"]
    user = data.get("user", "anonymous")

    # Gọi AI
    ai_reply = ask_ai(user_msg)

    # Lưu chat vào DB (user + AI)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO chat_bot (sender, message) VALUES (%s, %s)", ("user", user_msg))
    cursor.execute("INSERT INTO chat_bot (sender, message) VALUES (%s, %s)", ("bot", ai_reply))
    conn.commit()
    cursor.close()
    conn.close()

    # Gửi real-time về client
    emit("receive_message", {"user": user, "message": user_msg, "response": ai_reply}, broadcast=True)

@app.route("/teacher/help")
def teacher_help():
    user_id = session.get("user_id")
    if not user_id:
        # Nếu chưa login, redirect hoặc trả về lỗi
        return redirect("/login")

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    # Lấy thông tin user + teacher (avatar + name + major)
    cursor.execute("""
        SELECT 
            u.name, u.avatar, t.major
        FROM users u
        LEFT JOIN teacher t ON u.id = t.user_id
        WHERE u.id = %s
    """, (user_id,))
    teacher = cursor.fetchone()  # dict: {"name": ..., "avatar": ..., "major": ...}

    # Lấy danh sách ticket của user
    cursor.execute("""
        SELECT * 
        FROM support_tickets 
        WHERE user_id=%s 
        ORDER BY created_at DESC
    """, (user_id,))
    tickets = cursor.fetchall()  # list of dict

    cursor.close()
    conn.close()

    # Render template với context
    return render_template("GV/help.html", teacher=teacher, tickets=tickets)


# 🧠 Route xử lý chat bot
@app.route("/teacher/help/chat", methods=["POST"])
def teacher_help_chat():
    user_msg = request.form.get("message", "").strip()
    if not user_msg:
        return jsonify({"response": "Vui lòng nhập câu hỏi."})

    # Gọi lại chatbot AI cũ bạn đang dùng
    ai_reply = ask_ai(user_msg, role="teacher")

    # Lưu lại log chat nếu muốn
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO chat_bot (sender, message, user_type) VALUES (%s, %s, %s)",
        ("teacher", user_msg, "teacher")
    )
    cursor.execute(
        "INSERT INTO chat_bot (sender, message, user_type) VALUES (%s, %s, %s)",
        ("bot", ai_reply, "bot")
    )
    conn.commit()
    cursor.close()
    conn.close()

    return jsonify({"response": ai_reply})



# 🎟️ Route thêm ticket hỗ trợ
@app.route("/teacher/help/ticket", methods=["POST"])
def teacher_ticket():
    title = request.form["title"]
    description = request.form["description"]
    user_id = session["user_id"]
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO support_tickets (user_id, title, description) VALUES (%s, %s, %s)",
        (user_id, title, description),
    )
    conn.commit()
    cursor.close()
    conn.close()
    return redirect("/teacher/help")

@app.route("/admin/support")
def admin_support_list():
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT st.*, u.name AS teacher_name, u.email
        FROM support_tickets st
        JOIN users u ON st.user_id = u.id
        ORDER BY st.created_at DESC
    """)
    tickets = cursor.fetchall()
    return render_template("Admin/QL_support.html", tickets=tickets)

# --- Cập nhật trạng thái ticket ---
@app.route("/admin/support/update/<int:id>", methods=["POST"])
def admin_update_ticket(id):
    new_status = request.form['status']
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE support_tickets SET status = %s WHERE id = %s", (new_status, id))
    conn.commit()
    flash("✅ Cập nhật trạng thái thành công!", "success")
    return redirect(url_for('admin_support_list'))

# --- Xóa ticket ---
@app.route("/admin/support/delete/<int:id>", methods=["POST"])
def admin_delete_ticket(id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM support_tickets WHERE id = %s", (id,))
    conn.commit()
    flash("🗑️ Đã xóa ticket thành công!", "danger")
    return redirect(url_for('admin_support_list'))


@app.route("/api/notifications", methods=["GET"])
def get_notifications():
    if "user_id" not in session:
        return jsonify({"error": "Chưa đăng nhập"}), 401

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("""
        SELECT id, title, message, is_read, created_at
        FROM notifications
        WHERE user_id = %s
        ORDER BY created_at DESC
    """, (session["user_id"],))

    notifications = cursor.fetchall()

    cursor.close()
    conn.close()

    return jsonify(notifications)

@app.route("/api/notifications/unread_count", methods=["GET"])
def get_unread_notification_count():
    if "user_id" not in session:
        return jsonify({"error": "Chưa đăng nhập"}), 401

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT COUNT(*) 
        FROM notifications 
        WHERE user_id = %s AND is_read = 0
    """, (session["user_id"],))
    (count,) = cursor.fetchone()

    cursor.close()
    conn.close()

    return jsonify({"unread_count": count})

@app.route("/api/notifications/mark_read/<int:noti_id>", methods=["POST"])
def mark_notification_read(noti_id):
    if "user_id" not in session:
        return jsonify({"error": "Chưa đăng nhập"}), 401

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE notifications 
        SET is_read = 1 
        WHERE id = %s AND user_id = %s
    """, (noti_id, session["user_id"]))
    conn.commit()

    cursor.close()
    conn.close()

    return jsonify({"success": True})


def tinh_tiet(start_time_str, end_time_str):
    tiet_times = [
        ("07:00", "07:55"),
        ("08:00", "08:55"),
        ("09:00", "09:55"),
        ("10:00", "10:55"),
        ("11:00", "11:55"),
        ("12:30", "13:25"),
        ("13:30", "14:25"),
        ("14:30", "15:25"),
        ("15:30", "16:25"),
        ("16:30", "17:25"),
    ]

    def to_minutes(t):
        dt = datetime.strptime(t.strip().replace("::", ":").rstrip(":"), "%H:%M")
        return dt.hour * 60 + dt.minute

    start_m = to_minutes(start_time_str)
    end_m = to_minutes(end_time_str)

    tiet_bat_dau = tiet_ket_thuc = None

    for i, (bd, kt) in enumerate(tiet_times, start=1):
        bd_m = to_minutes(bd)
        kt_m = to_minutes(kt)
        if tiet_bat_dau is None and start_m >= bd_m - 5 and start_m <= kt_m:
            tiet_bat_dau = i
        if end_m > bd_m and end_m <= kt_m + 5:
            tiet_ket_thuc = i

    # Nếu không khớp chính xác, tìm tiết gần nhất
    if tiet_bat_dau is None:
        tiet_bat_dau = next((i for i, (bd, kt) in enumerate(tiet_times, 1) if start_m < to_minutes(kt)), 1)
    if tiet_ket_thuc is None:
        tiet_ket_thuc = next((i for i, (bd, kt) in reversed(list(enumerate(tiet_times, 1))) if end_m > to_minutes(bd)), len(tiet_times))

    return tiet_bat_dau, tiet_ket_thuc


@app.route('/lich_day_gv')
def lich_day_gv():
    if 'user_id' not in session:
        return redirect('/login')

    teacher_id = session['user_id']
    week_offset = int(request.args.get("week_offset", 0))
    today = date.today()
    start_of_week = today - timedelta(days=today.weekday()) + timedelta(weeks=week_offset)
    end_of_week = start_of_week + timedelta(days=6)

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT 
            c.class_name,
            c.room,
            c.day_of_week,
            c.start_date,
            c.weeks,
            c.start_time,
            c.end_time,
            u.name AS teacher_name
        FROM classes c
        JOIN users u ON c.teacher_id = u.id
        WHERE c.teacher_id = %s
    """, (teacher_id,))
    classes = cursor.fetchall()
    cursor.close()
    conn.close()

    teacher_name = classes[0]['teacher_name'] if classes else 'Không rõ'

    # Khởi tạo khung thời khóa biểu
    schedule = { 'Thứ 2': [], 'Thứ 3': [], 'Thứ 4': [],
                 'Thứ 5': [], 'Thứ 6': [], 'Thứ 7': [], 'Chủ Nhật': [] }

    # === Đưa dữ liệu lớp học vào từng thứ ===
    for lop in classes:
        start_time = str(lop["start_time"])[:5]
        end_time = str(lop["end_time"])[:5]
        start_date = lop["start_date"]

        for i in range(lop["weeks"]):
            buoi_date = start_date + timedelta(weeks=i)
            if not (start_of_week <= buoi_date <= end_of_week):
                continue

            tiet_bd, tiet_kt = tinh_tiet(start_time, end_time)
            thu = lop["day_of_week"].strip().capitalize()
            if thu not in schedule:
                continue

            schedule[thu].append({
                "class_name": lop["class_name"],
                "room": lop["room"],
                "session_start": start_time,
                "session_end": end_time,
                "tiet_bat_dau": tiet_bd,
                "tiet_ket_thuc": tiet_kt,
                "teacher_name": teacher_name
            })

    # === Gộp các tiết liền nhau cùng lớp ===
    for thu, buoi_list in schedule.items():
        if not buoi_list:
            continue

        buoi_list.sort(key=lambda x: x["tiet_bat_dau"])  # sắp xếp theo tiết

        merged = []
        current = buoi_list[0]

        for b in buoi_list[1:]:
            # Nếu cùng lớp và tiết bắt đầu của buổi sau = tiết kết thúc của buổi trước + 1
            if b["class_name"] == current["class_name"] and b["tiet_bat_dau"] == current["tiet_ket_thuc"] + 1:
                # gộp tiết
                current["tiet_ket_thuc"] = b["tiet_ket_thuc"]
                current["session_end"] = b["session_end"]
            else:
                merged.append(current)
                current = b

        merged.append(current)
        schedule[thu] = merged

    return render_template(
        'GV/lich_day_gv.html',
        schedule=schedule,
        week_start=start_of_week.strftime("%d/%m/%Y"),
        week_end=end_of_week.strftime("%d/%m/%Y"),
        teacher_name=teacher_name,
        week_offset=week_offset
    )
# ====================== EMAIL ======================
# ====================== SEND EMAIL ======================

import smtplib
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr
def send_email(to_email, subject, content):
    sender = "2124802010398@student.tdmu.edu.vn"
    password = "jtqp bqly zmwm waca"   # App password Gmail

    try:
        # ✅ Tạo email đúng chuẩn UTF-8 (có dấu tiếng Việt)
        msg = MIMEText(content, "plain", "utf-8")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = formataddr(("Hệ thống điểm danh", sender))
        msg["To"] = to_email

        # ✅ Gửi mail qua SMTP SSL
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender, password)
            server.sendmail(sender, [to_email], msg.as_string().encode("utf-8"))

        print(f"📧 Đã gửi email đến {to_email}")

    except Exception as e:
        print(f"⚠️ Lỗi khi gửi email đến {to_email}: {e}")
# ====================== AUTO CHECK ======================
def auto_check_attendance():
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    print("🔍 Auto check đang chạy...")

    cursor.execute("""
        SELECT 
            e.student_id, 
            u.email AS sv_email, 
            u.name  AS sv_name,
            c.id    AS class_id, 
            c.class_name, 
            c.teacher_id, 
            t.email AS gv_email, 
            t.name  AS gv_name,
            c.weeks AS tong_buoi,
            COUNT(
                CASE 
                    WHEN ar.status_in IN ('absent_excused', 'absent_unexcused')
                    THEN 1 
                END
            ) AS so_buoi_nghi
        FROM enrollments e
        JOIN users u ON e.student_id = u.id
        JOIN classes c ON e.class_id = c.id
        JOIN users t ON c.teacher_id = t.id
        LEFT JOIN attendance_records ar ON ar.enrollment_id = e.id
        GROUP BY e.student_id, u.email, u.name, 
                 c.id, c.class_name, c.teacher_id, t.email, t.name, c.weeks
    """)

    data = cursor.fetchall()

    for row in data:
        tong_buoi = row["tong_buoi"] or 0
        gioi_han = tong_buoi * 0.2
        msg = None

        # ===== Xác định nội dung cảnh báo =====
        if row["so_buoi_nghi"] > gioi_han:
            msg = (
                f"Sinh viên {row['sv_name']} (lớp {row['class_name']}) "
                f"đã vắng {row['so_buoi_nghi']} buổi học, vượt quá 20% tổng số buổi quy định ({int(gioi_han)} buổi). "
                "Theo quy định của nhà trường, sinh viên sẽ không đủ điều kiện dự thi học phần này. "
                "Vui lòng liên hệ giảng viên phụ trách để được hướng dẫn và khắc phục sớm."
            )

        elif row["so_buoi_nghi"] == gioi_han:
            msg = (
                f"Sinh viên {row['sv_name']} (lớp {row['class_name']}) "
                f"đã vắng {int(gioi_han)} buổi học, đạt mức 20% tổng số buổi quy định. "
                "Đề nghị sinh viên nghiêm túc tham gia đầy đủ các buổi học còn lại "
                "để tránh bị cấm dự thi học phần này theo quy định của nhà trường."
            )

        # ===== Nếu có cảnh báo thì kiểm tra xem đã gửi chưa =====
        if msg:
            cursor.execute("""
                SELECT COUNT(*) AS cnt 
                FROM notifications
                WHERE user_id = %s 
                  AND title = 'Cảnh báo nghỉ học'
                  AND message LIKE %s
            """, (row["student_id"], f"%{row['class_name']}%"))
            sent_check = cursor.fetchone()

            if sent_check["cnt"] == 0:
                # 🔔 Gửi thông báo cho sinh viên
                cursor.execute("""
                    INSERT INTO notifications(user_id, title, message)
                    VALUES (%s, %s, %s)
                """, (row["student_id"], "Cảnh báo nghỉ học", msg))

                # 🔔 Gửi thông báo cho giáo viên
                gv_msg = (
                    f"Sinh viên {row['sv_name']} trong lớp {row['class_name']} "
                    f"đã vắng {row['so_buoi_nghi']} buổi học, vượt quá giới hạn cho phép ({int(gioi_han)} buổi = 20%). "
                    "Đề nghị giảng viên lưu ý và cập nhật tình trạng học tập của sinh viên trong hệ thống."
                )

                cursor.execute("""
                    INSERT INTO notifications(user_id, title, message)
                    VALUES (%s, %s, %s)
                """, (row["teacher_id"], "Thông báo sinh viên nghỉ học", gv_msg))

                # 📧 Gửi email
                send_email(row["sv_email"], "Cảnh báo nghỉ học", msg)
                send_email(row["gv_email"], "Thông báo sinh viên nghỉ học", gv_msg)

                print(f"📧 Đã gửi cảnh báo cho {row['sv_name']} (lớp {row['class_name']})")
            else:
                print(f"⚠️ {row['sv_name']} (lớp {row['class_name']}) đã được gửi cảnh báo trước đó, bỏ qua.")

    conn.commit()
    cursor.close()
    conn.close()
    print("✅ Auto check done!")



from datetime import datetime, timedelta
from pytz import timezone

# ================== TỰ ĐỘNG ĐÁNH VẮNG ==================
def auto_mark_absent_for_session(session_id):
    """Tự động đánh vắng cho buổi học đã kết thúc"""
    with app.app_context():
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute("SELECT id, date, end_time FROM sessions WHERE id = %s", (session_id,))
        session = cursor.fetchone()
        if not session:
            print("⚠️ Không tìm thấy session này")
            return

        # ✅ Tính thời gian kết thúc thực tế trong Python
        end_time = (datetime.combine(session['date'], datetime.min.time()) +
                    session['end_time'])

        # So sánh với thời gian hiện tại
        if datetime.now() < end_time:
            print("⏳ Buổi học chưa kết thúc, chưa cập nhật.")
            return

        # Update trạng thái vắng
        cursor.execute("""
            UPDATE attendance_records
            SET status_in = 'absent_unexcused'
            WHERE session_id = %s AND status_in = 'none'
        """, (session_id,))
        conn.commit()

        print(f"🔁 UPDATE ảnh hưởng: {cursor.rowcount} hàng")
        cursor.close()
        conn.close()
        print(f"✅ Đã tự động cập nhật vắng cho session {session_id} lúc {datetime.now()}")




def schedule_absent_job(session_id, session_date, end_time):
    """Đặt lịch chạy auto_mark_absent_for_session() sau giờ học"""
    vn_tz = timezone("Asia/Ho_Chi_Minh")

    # Gộp date + time
    run_datetime = datetime.combine(session_date, end_time)
    run_datetime = vn_tz.localize(run_datetime)


    try:
        scheduler.add_job(
            func=auto_mark_absent_for_session,
            trigger='date',
            run_date=run_datetime,
            args=[session_id],
            id=f"auto_absent_{session_id}",
            replace_existing=True
        )
        print(f"✅ Đã tạo job auto-vắng cho session {session_id} lúc {run_datetime}")
    except Exception as e:
        print(f"❌ Lỗi khi tạo job cho session {session_id}: {e}")





# ====================== APScheduler ======================
class Config:
    SCHEDULER_API_ENABLED = True

app.config.from_object(Config())
scheduler = APScheduler()

# Job chạy hằng ngày lúc 6h sáng
# Job chạy hằng ngày lúc 6h sáng
@scheduler.task('cron', id='auto_check_attendance', hour=6, minute=0)
def scheduled_check():
    with app.app_context():
        auto_check_attendance()

# ✅ Job test: chạy mỗi 30 giây để test scheduler
@scheduler.task('interval', id='test_scheduler', seconds=50)
def test_scheduler():
    with app.app_context():
        print("✅ Scheduler đang hoạt động - Test chạy thành công!")
        auto_check_attendance()  # 👉 thêm dòng này để test gửi mail luôn



# =============================
if __name__ == "__main__":
    print("🚀 Starting scheduler...")
    scheduler.init_app(app)  # gắn scheduler vào Flask
    scheduler.start()  # khởi động scheduler
    socketio.run(app, debug=True, use_reloader=False)
