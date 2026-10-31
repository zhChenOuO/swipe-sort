FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt
COPY swipesort ./swipesort
ENV SWIPE_ROOT=/media \
    SWIPE_DATA=/data \
    SWIPE_PORT=8080 \
    PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python", "-m", "swipesort"]
