FROM python:3.13-slim

WORKDIR /app
RUN mkdir -p logs data

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "scraper.py", "run"]