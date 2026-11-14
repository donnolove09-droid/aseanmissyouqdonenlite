# Telegram Bot Check Liên Quân - Bộ file hoàn chỉnh

## Cài đặt nhanh

```bash
pip install -r requirements.txt
```

## Cấu hình

1. Lấy token từ **@BotFather**
2. Sửa dòng `TOKEN = ...` trong `bot_telegram.py`  
   **hoặc** chạy:
   ```bash
   export TELEGRAM_BOT_TOKEN="123456:ABC..."
   ```

## Chạy

```bash
python bot_telegram.py
```

## Lệnh

- `/checklq tài_khoản-mật_khẩu`
- `/start`
- `/help`

## File trong bộ này

| File            | Mô tả                              |
|-----------------|------------------------------------|
| bot_telegram.py | Bot chính                          |
| goc.py          | Logic check Garena / Liên Quân     |
| requirements.txt| Thư viện                           |
| render.yaml     | Cấu hình deploy Render.com         |
| yaml.render     | Template cấu hình                  |
| request.txt     | Hướng dẫn chi tiết                 |
| README.md       | File này                           |

## Deploy lên Render

1. Upload toàn bộ thư mục lên GitHub
2. Tạo Background Worker mới trên Render
3. Chọn repo → Render sẽ đọc `render.yaml`
4. Set biến môi trường `TELEGRAM_BOT_TOKEN`

## Lưu ý

- Acc HIT được lưu tự động vào thư mục `hits/`
- Nếu bị timeout/port blocked → thêm file `proxy.txt`
- Captcha cần thêm `ddddocr` + `opencv-python` nếu muốn xử lý tốt hơn
