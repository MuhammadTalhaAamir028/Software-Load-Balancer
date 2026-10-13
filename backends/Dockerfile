# Base image: a small official Python image. Version 3.11 is fixed so that every
# machine in the group builds exactly the same environment.
FROM python:3.11-slim

# Send Python output straight to the container log instead of buffering it,
# so the single startup line appears in "docker logs" immediately.
ENV PYTHONUNBUFFERED=1

# All following commands run inside /app in the image.
WORKDIR /app

# Only app.py is needed. The server uses the standard library exclusively,
# therefore there is no "pip install" step and the image stays small.
COPY app.py .

# Documentation only: the server listens on 8000 by default (see PORT in app.py).
EXPOSE 8000

# Exec form (a JSON list) makes Python the main process (PID 1), so it receives
# "docker stop" and "docker kill" signals directly, without a shell in between.
CMD ["python", "app.py"]