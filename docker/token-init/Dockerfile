FROM python:3.12-slim

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

VOLUME ["/shared"]
ENTRYPOINT ["/entrypoint.sh"]
