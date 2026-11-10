# ==============================================================================
# FreshRoute — Dockerfile
# ==============================================================================
#
# Build image:
#     docker build -t freshroute .
#
# Run container:
#     docker run -p 8000:8000 freshroute
#
# Then visit:
#     http://localhost:8000/docs
#
# WHY python:3.12-slim?
# The slim variant is ~50MB vs ~900MB for the full image. It contains only
# what is needed to run Python. We do not need compilers or system tools
# in a production container.
# ==============================================================================

FROM python:3.12-slim

# --------------------------------------------------------------------------
# System setup
# --------------------------------------------------------------------------

# Prevent Python from writing .pyc files to disk.
# Not needed in containers and wastes I/O.
ENV PYTHONDONTWRITEBYTECODE=1

# Prevent Python from buffering stdout/stderr.
# Ensures logs appear immediately in Docker/Railway/AWS CloudWatch.
ENV PYTHONUNBUFFERED=1

# Set working directory. All subsequent commands run from here.
WORKDIR /app

# --------------------------------------------------------------------------
# Install dependencies
# --------------------------------------------------------------------------

# Copy ONLY requirements first. Docker caches each layer independently.
# If requirements.txt hasn't changed, Docker reuses the cached pip install
# layer even when your application code changes. This makes rebuilds fast.
COPY requirements.txt .

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# --------------------------------------------------------------------------
# Copy application code
# --------------------------------------------------------------------------

# Copy the rest of the project after installing dependencies.
# This ordering (deps first, code second) is intentional — see above.
COPY . .

# --------------------------------------------------------------------------
# Security: run as non-root user
# --------------------------------------------------------------------------

# Running as root inside a container is a security risk. Create a dedicated
# user with no shell and no home directory.
RUN adduser --disabled-password --no-create-home freshroute
USER freshroute

# --------------------------------------------------------------------------
# Expose and run
# --------------------------------------------------------------------------

# Document which port the application listens on.
# This does NOT publish the port — use -p 8000:8000 in docker run.
EXPOSE 8000

# Start the application using uvicorn.
# --host 0.0.0.0  : listen on all network interfaces (required in containers)
# --port 8000     : the port inside the container
# --workers 1     : single worker for MVP; scale with gunicorn in production
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
