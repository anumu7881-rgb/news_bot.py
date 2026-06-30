FROM python:3.10-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY news_bot.py ./

# Default runs in daemon mode; set SINGLE_RUN=1 to run once
CMD ["python", "news_bot.py"]