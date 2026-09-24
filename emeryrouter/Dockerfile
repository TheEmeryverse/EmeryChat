FROM python:3.12-slim
WORKDIR /srv
COPY emeryrouter /srv/emeryrouter
ENV PYTHONUNBUFFERED=1
EXPOSE 8220
CMD ["python", "-m", "emeryrouter"]
