FROM python:3.10-slim AS builder

WORKDIR /app

# Create a self-contained virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install dependencies into the virtual environment
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

#production runner

FROM python:3.10-slim

WORKDIR /app

# Enforce unbuffered stdout for accurate Log Analytics streaming
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Copy the entire virtual environment from the builder stage
COPY --from=builder /opt/venv /opt/venv

# Activate the virtual environment globally for the container
ENV PATH="/opt/venv/bin:$PATH"

# Copy execution logic and default configurations
COPY src/ ./src/
COPY data/ ./data/
COPY simulator/ ./simulator/

EXPOSE 8080

# Create persistent directories and enforce least-privilege non-root execution
RUN useradd -m -u 1000 appuser && \
    mkdir -p /app/reports /app/data && \
    chown -R appuser:appuser /app

# Switch to the secure non-root user
USER appuser


# Execute startup dependency checks, then launch the orchestrator.
CMD ["python", "-m", "src.run_with_dependency_checks"]