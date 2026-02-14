FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render expects the app to listen on the port defined by $PORT
# We default to 8080 in the python script if not set
EXPOSE 8080

CMD ["python", "main.py"]
