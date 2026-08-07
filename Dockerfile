FROM python:3.11-slim

WORKDIR /app

# Dependencies first, so editing app code doesn't bust the pip layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Bake the Canada-wide airport/runway dataset into the image at build time.
#
# Without this, the dataset is absent at runtime (it's gitignored), so the first
# request after every cold start blocks on a ~20 MB OurAirports download — which
# defeats the whole point of the scale-to-zero wake in fly.toml. Building it here
# makes ensure_airport_data() a no-op at runtime (it version-checks and skips).
#
# Best-effort: if the fetch fails the build still succeeds and the app falls back
# to the bundled data/*_seed.csv, exactly as it does offline.
RUN python scripts/refresh_airport_data.py || \
    echo "WARNING: airport dataset build failed; falling back to bundled seed"

ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
