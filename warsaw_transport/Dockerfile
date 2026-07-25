ARG BUILD_FROM
FROM ${BUILD_FROM}

ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /opt/warsaw_transport

# Python dependencies
COPY requirements.txt ./
RUN pip3 install --no-cache-dir -r requirements.txt

# Application code
COPY app/ ./app/
# Lovelace card — run.sh copies this into the HA config www/ folder on start.
COPY lovelace/ ./lovelace/
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
