FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .
COPY zai_respond.py .
COPY data/leads/ /data/agencia-riopreto/leads/
ENV SOULS_DIR=/app/souls
ENV WORKSPACE=/data/agencia-riopreto
EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
