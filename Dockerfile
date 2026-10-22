FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p instance uploads

EXPOSE 5050

CMD ["sh", "-c", "python load_dummy_data.py && exec gunicorn -w 2 -b 0.0.0.0:${PORT:-5050} --timeout 120 run:app"]
