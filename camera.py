import cv2
import face_recognition
from database import get_connection

def diem_danh(ma_sv):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO diem_danh (ma_sv, trang_thai) VALUES (%s, %s)", (ma_sv, "Có mặt"))
    conn.commit()
    conn.close()

# Ví dụ: nhận diện một sinh viên
known_image = face_recognition.load_image_file("static/sv1.jpg")
known_encoding = face_recognition.face_encodings(known_image)[0]

cap = cv2.VideoCapture(0)

while True:
    ret, frame = cap.read()
    rgb_frame = frame[:, :, ::-1]

    face_locations = face_recognition.face_locations(rgb_frame)
    face_encodings = face_recognition.face_encodings(rgb_frame, face_locations)

    for face_encoding in face_encodings:
        results = face_recognition.compare_faces([known_encoding], face_encoding)
        if results[0]:
            print("✅ Nhận diện đúng sinh viên → Điểm danh")
            diem_danh("SV001")

    cv2.imshow('Diem danh', frame)
    if cv2.waitKey(1) & 0xFF == ord('q'):
        break

cap.release()
cv2.destroyAllWindows()
