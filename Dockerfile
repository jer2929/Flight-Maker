FROM python:3.11-slim

WORKDIR /app

# Dependencies first, so editing app code doesn't bust the pip layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Bake the Canada-wide airport/runway dataset into the image at build time.
#
# Without this, the dataset is absent at runtime (it's gitignored) and the app
# boots into a 14 MB OurAirports download, which defeats the whole point of the
# scale-to-zero wake in fly.toml. Building it here makes prepare_dataset() a
# no-op at runtime (it version-checks and skips).
#
# This used to end in `|| echo WARNING`, and that fallback was worse than no
# fallback. The bundled data/*_seed.csv is 28 aerodromes: an image built while
# OurAirports was briefly unreachable shipped *silently*, and the first pilot to
# use it waited out the download and got "unknown destination" for anywhere not
# in those 28. Offline tolerance belongs at runtime, where a laptop with no
# egress still wants a working app; it has no business in a production image.
#
# A failed build is the right outcome: the deploy stops and Fly keeps serving
# the previous image, which has a good dataset baked in.
RUN python scripts/refresh_airport_data.py

ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
