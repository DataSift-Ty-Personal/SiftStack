# Apify Actor Dockerfile — Python 3.12 + Playwright + Chromium
FROM apify/actor-python-playwright:3.12

# Copy requirements first for better layer caching
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY src/ ./src/
COPY .actor/ ./.actor/

# Playwright browsers are pre-installed in the base image.
# Set working directory so imports from src/ work.
ENV PYTHONPATH=/home/myuser/src

CMD ["python", "src/main.py"]
