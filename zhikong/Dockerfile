FROM python:3.11-slim

WORKDIR /app

# 依赖均为预编译 wheel（numpy / langgraph / fastapi 等），无需 gcc 编译，
# 故不安装编译工具链，加快构建并缩小镜像。
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 持久化数据目录（挂载卷以免数据随容器丢失）
VOLUME ["/app/data"]

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/api/v1/health').status==200 else 1)"

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
