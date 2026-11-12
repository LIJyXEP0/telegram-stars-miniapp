FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv/app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY miniapp ./miniapp
COPY config.yml ./config.yml
COPY .env.example ./env.example
EXPOSE 8080
CMD ["python", "-m", "app.main"]
