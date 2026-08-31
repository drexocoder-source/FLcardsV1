FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py app.py config.py ./
COPY database ./database
COPY dhandlers ./dhandlers
COPY plugins ./plugins
COPY services ./services
COPY assets ./assets

EXPOSE 8080

CMD ["python", "bot.py"]
