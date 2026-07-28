FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8765

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY flask_app.py pinecone_memory.py mem0_memory.py tool_calling.py ./
COPY static/inbox.html ./static/inbox.html

EXPOSE 8765
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT} --workers 2 --threads 4 flask_app:app"]
