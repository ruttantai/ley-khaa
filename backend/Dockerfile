FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
RUN pip install --no-cache-dir .
COPY . .
CMD ["uvicorn", "ley_khaa.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
