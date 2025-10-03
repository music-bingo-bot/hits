FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --upgrade pip && pip install -r requirements.txt
RUN mkdir -p /app/uploads/audio /app/uploads/hints
COPY . /app
EXPOSE 8080
ENV PORT=8080
CMD ["python","main.py"]
