FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir --only-binary=:all: .

# The application, Telegram pollers and API need no root privileges.
# All application files and installed wheels are readable by this UID.
USER 65532:65532

EXPOSE 8000
CMD ["uvicorn", "telegram_membership_saas.main:app", "--host", "0.0.0.0", "--port", "8000"]
