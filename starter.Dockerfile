# Build this from the repository root after editing starter/agent.py.
# It copies only participant code and the HTTP SDK into the submission image.
FROM python:3.13-slim
WORKDIR /app
COPY requirements-client.txt ./
RUN pip install --no-cache-dir -r requirements-client.txt
COPY starter/ ./starter/
COPY aiday/client.py ./aiday/client.py
RUN touch aiday/__init__.py
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 65532:65532
CMD ["python", "-m", "starter.agent"]
