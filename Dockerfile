FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Copy requirements and install dependencies
COPY src/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY src/scan_adapter.py .

# Create non-root user
RUN useradd -m -u 1000 scanner && \
    chown -R scanner:scanner /app

# Switch to non-root user
USER scanner

# Run the application
CMD ["python", "-u", "scan_adapter.py"]
