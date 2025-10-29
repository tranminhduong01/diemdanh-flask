# ai.py
from google.genai import Client

# Tạo client với API key của bạn
client = Client(api_key="AIzaSyDymlFtdZhgmaA8Jw1FQnSaENuz5GZ1cdA")  # Thay bằng API key thật


def get_ai_response(prompt: str, model: str = "gemini-1.5") -> str:
    """
    Gửi prompt tới Gemini AI và trả về kết quả text.

    :param prompt: Chuỗi câu hỏi hoặc yêu cầu
    :param model: Model Gemini muốn dùng, mặc định "gemini-1.5"
    :return: Text trả về từ AI
    """
    response = client.generate_text(model=model, prompt=prompt)
    return response.text
