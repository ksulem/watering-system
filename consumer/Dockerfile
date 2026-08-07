FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copied as a package (unlike cmd_dispatcher's flat main.py) so the modules can
# import each other as `from consumer import db`. `python -m` puts /app on
# sys.path, which is what makes that resolve.
COPY . ./consumer/

CMD ["python", "-m", "consumer.main"]
