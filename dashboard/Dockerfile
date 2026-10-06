FROM python:3.11-alpine

WORKDIR /app

RUN apk add --no-cache docker-cli

COPY dashboard.py ./dashboard.py
COPY supervisor.py ./supervisor.py

EXPOSE 39777

CMD ["python", "dashboard.py"]
