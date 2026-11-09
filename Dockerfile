FROM python:3.13.15-slim
WORKDIR /app
COPY requirements.lock .
RUN python -m pip install --no-cache-dir -r requirements.lock
COPY services services
COPY apps apps
COPY contracts contracts
COPY migrations migrations
COPY fixtures fixtures
COPY evals evals
COPY tests tests
ENV HOST=0.0.0.0 PORT=8765 PYTHONUNBUFFERED=1
EXPOSE 8765
CMD ["python", "-m", "services.api"]
