import smtplib
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr

def send_email_test():
    sender = "2124802010398@student.tdmu.edu.vn"
    password = "jtqpbqlyzmwmwaca"  # Thử không có khoảng trắng
    to_email = "email_nhan@gmail.com"

    try:
        msg = MIMEText("Đây là email test", "plain", "utf-8")
        msg["Subject"] = Header("Test Email", "utf-8")
        msg["From"] = formataddr(("Hệ thống điểm danh", sender))
        msg["To"] = to_email

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender, password)
            server.sendmail(sender, [to_email], msg.as_string().encode("utf-8"))
        print("✅ Gửi thành công!")

    except Exception as e:
        print(f"❌ Lỗi: {e}")

send_email_test()
