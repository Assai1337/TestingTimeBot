FROM python:3.12 AS base

WORKDIR /usr/src/app

COPY requirements.txt .

RUN apt-get update && apt-get -y install libpq-dev gcc
RUN pip install -r requirements.txt

COPY . .

FROM base AS bot

CMD ["python", "bot.py"]

FROM base AS web

CMD ["flask", "run", "--host=0.0.0.0", "--port=5000"]

FROM base AS migration-tool

CMD ["python", "tools/init_db.py"]
