# Apify's Playwright image ships Chromium pre-installed; it is only launched
# when a board is blocked over plain HTTP (see src/utils.py BrowserPool).
FROM apify/actor-python-playwright:3.12

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . ./

CMD ["python", "-m", "src.main"]
