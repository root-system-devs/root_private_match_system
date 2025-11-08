FROM python:3.11-slim


# OSパッケージ（必要に応じて追加）
RUN apt-get update -y && apt-get install -y --no-install-recommends \
tzdata && \
rm -rf /var/lib/apt/lists/*


WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt


COPY . .
ENV TZ=Asia/Tokyo


# プロダクションでは root 以外のユーザーを使うのが推奨（省略可）
# RUN useradd -m bot && chown -R bot:bot /app
# USER bot


CMD ["python", "-m", "app.bot"]