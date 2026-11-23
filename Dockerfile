# تشغيل «همس» على السيرفر: بايثون + أداة الصوت، والموقع على المنفذ ٧٨٦٠
FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
RUN useradd -m -u 1000 user
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=user server.py index.html ./
COPY --chown=user static ./static
RUN mkdir -p /app/data && chown -R user /app
USER user
ENV HOST=0.0.0.0 PORT=7860
EXPOSE 7860
CMD ["python", "server.py"]
