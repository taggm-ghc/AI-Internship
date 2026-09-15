FROM python:3.12-slim

WORKDIR /app

# requirements.txt only — never requirements-dev.txt here. That file pulls
# in Playwright for scripts/capture_openai_pricing.py (dev-only pricing-page
# screenshot verification, not imported by any deployed code path); this
# slim base image has neither Chromium's binary (needs `playwright install
# chromium`) nor the system shared libraries it links against (see
# scripts/chromium-deps.sh) — adding it here would bloat this image by
# several hundred MB for a browser that still wouldn't actually launch.
# requirements-dev.txt and scripts/ are excluded from the build context
# entirely via .dockerignore, not just left uninstalled.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# Pass the key at runtime — do not bake secrets into the image:
#   docker build -t week-1v2 .
#   docker run --rm -p 8000:8000 -e OPENAI_API_KEY=sk-... week-1v2
#
# Optional Streamlit UI, standalone (bind to the port this image EXPOSEs,
# 8000, not Streamlit's default 8501 — matters on Render, which routes to
# the Dockerfile's EXPOSE'd port; see README's Deploy section):
#   docker run --rm -p 8000:8000 -e API_BASE_URL=https://your-api.example.com week-1v2 \
#     streamlit run demo_page.py --server.address 0.0.0.0 --server.port 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
