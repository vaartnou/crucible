FROM python@sha256:3fe1a0b6c2d6b2ebb572906cdabeca75aeb558c0c17db5ffefbcf8094871c32b
ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin probe
COPY app.py ./
USER probe
EXPOSE 8000
CMD ["uvicorn", "--factory", "app:create_app", "--host", "0.0.0.0", "--port", "8000"]
