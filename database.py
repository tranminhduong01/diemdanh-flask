import mysql.connector

# Hàm kết nối MySQL
def get_connection():
    return mysql.connector.connect(
        host="localhost",
        user="root",        # thay bằng user MySQL của bạn
        password="123456",  # thay bằng password MySQL của bạn
        database="face_attendance"  # database bạn đã tạo
    )

# Kết nối
conn = get_connection()
cursor = conn.cursor()

# Tạo bảng users (nếu chưa có)
cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    id INT AUTO_INCREMENT PRIMARY KEY,
    username VARCHAR(50) UNIQUE NOT NULL,
    password VARCHAR(255) NOT NULL,
    role ENUM('student', 'teacher', 'admin') NOT NULL DEFAULT 'student'
)
""")


cursor.close()
conn.close()
