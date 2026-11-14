FROM python:3.13-slim
WORKDIR /app
COPY pyproject.toml README.md requirements-server.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements-server.txt
COPY aiday/ ./aiday/
RUN pip install --no-cache-dir --no-deps .
RUN useradd --uid 10001 --create-home event && mkdir /data && chown event:event /data
USER event
EXPOSE 8000
CMD ["aiday", "serve", "--host", "0.0.0.0", "--db", "/data/event.sqlite3", "--evaluation"]
