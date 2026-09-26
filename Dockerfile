# 把整个程序跑成一个在线服务（可选）。
#
# 用途：部署到任何支持 Docker 的托管平台（Render / Railway / HuggingFace Spaces…），
# 得到一个能从浏览器访问的网址。
#
# 注意：公开部署时**不要把 API Key 放进服务器**——界面上让使用者填自己的 Key。
#      见 README 的"在线部署"一节。
#
# 本地试跑：
#   docker build -t class-review .
#   docker run -p 7860:7860 class-review

FROM python:3.13-slim

WORKDIR /app

# 只装运行时真正需要的依赖
RUN pip install --no-cache-dir "openpyxl>=3.1"

COPY core/ ./core/
COPY web/ ./web/
COPY app.py README.md 使用说明.txt ./

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    HOST=0.0.0.0 \
    PORT=7860

EXPOSE 7860

# 容器里没有浏览器可开，用 --no-browser；端口从 PORT 环境变量读
CMD ["python", "app.py", "--no-browser"]
