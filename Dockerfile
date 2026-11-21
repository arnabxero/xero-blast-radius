# Team-Xero Blast Radius entry. Base image = the digest the organizers labeled with, so running
# sqlglot's tests inside the selector behaves exactly like their labels.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r /app/requirements.lock

# sqlglot at the pinned commit, from sqlglot's own GitHub repository (allowed build source).
COPY tools/fetch_sqlglot.py /app/tools/fetch_sqlglot.py
RUN python /app/tools/fetch_sqlglot.py /opt/sqlglot \
 && python -m compileall -q -j 0 /opt/sqlglot/sqlglot /opt/sqlglot/tests \
 && chmod -R a+rX /opt/sqlglot

COPY selector /app/selector
COPY maps /app/maps
RUN python -m compileall -q /app/selector && chmod -R a+rX /app

ENV SQLGLOT_SRC=/opt/sqlglot \
    OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1 \
    SKIP_INTEGRATION=0 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
CMD ["python", "/app/selector/main.py"]
