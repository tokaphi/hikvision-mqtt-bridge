FROM python:3.11-slim

WORKDIR /app

# Récupère les traces natives des crashs du SDK C (utile pour le debug), et
# force les sorties Python non bufferisées pour que les logs docker soient
# à jour en temps réel.
ENV PYTHONFAULTHANDLER=true \
    PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x entrypoint.sh

ENTRYPOINT ["./entrypoint.sh"]
