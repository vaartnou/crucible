# Independent grading image: no agent filesystem or live simulator is reused.
FROM python:3.14-slim-bookworm

ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /tests
COPY requirements.txt @@WHEEL_NAME@@ ./
RUN pip install --no-cache-dir -r requirements.txt ./@@WHEEL_NAME@@
COPY test.sh /tests/test.sh
