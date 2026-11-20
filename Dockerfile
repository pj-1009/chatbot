FROM python:3.14-slim

# Print logs straight away and don't write .pyc files (the filesystem is read-only in Kubernetes).
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt ./
RUN pip install -r requirements.txt
COPY chat.py db.py server.py ./
COPY static/ ./static/

# Run as an unprivileged user.
USER 10001
EXPOSE 8080
CMD ["python", "server.py"]
