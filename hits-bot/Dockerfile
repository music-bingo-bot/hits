FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg tini && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --upgrade pip && pip install -r requirements.txt

COPY . /app

# папка для загрузок (треков, картинок, рассылок)
RUN mkdir -p /app/uploads/audio /app/uploads/hints /app/uploads/broadcasts

EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini","--"]
CMD ["python","main.py"]
