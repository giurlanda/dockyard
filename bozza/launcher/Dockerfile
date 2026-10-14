FROM python:3.12-slim

RUN useradd -m -u 10001 app
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# /data diventa il volume launcher-data: la proprietà viene copiata al primo montaggio
RUN mkdir -p /data && chown app:app /data

USER app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
