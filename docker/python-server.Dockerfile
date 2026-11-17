FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser
COPY evo-entrypoint /usr/local/bin/evo-entrypoint
COPY evo-dependency-bootstrap.py /usr/local/bin/evo-dependency-bootstrap.py
RUN chmod 0555 /usr/local/bin/evo-entrypoint /usr/local/bin/evo-dependency-bootstrap.py
WORKDIR /workspace
USER appuser
EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/evo-entrypoint"]
CMD ["python"]
