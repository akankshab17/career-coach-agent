# The web app (Streamlit + the FastAPI API) in one container (deployed on Render's free web service, see render.yaml).
# Only Streamlit is exposed (on $PORT); the API listens on 127.0.0.1:8000 inside the container.
# API keys and APP_ACCESS_CODE come from the platform's secrets as environment variables: nothing secret is in the image.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=7860 \
    REQUIRE_ACCESS_CODE=1 \
    PRIVATE_BRIEFS=1 \
    MAX_BRIEFS_PER_DAY=5 \
    EXTRACT_CONCURRENCY=2

# The app runs as an unprivileged user (uid 1000) and writes .cache/ (jobs.db, extraction cache, spend.json) under its working
# directory. WORKDIR alone would create /home/user/app owned by root (PermissionError on '.cache'), so the directory and
# .cache/ are created and handed to the user here, before USER.
RUN useradd --create-home --uid 1000 user \
    && mkdir -p /home/user/app/.cache \
    && chown -R user:user /home/user/app
ENV HOME=/home/user
WORKDIR /home/user/app

COPY --chown=user requirements.txt .
RUN pip install -r requirements.txt

COPY --chown=user src/ src/
COPY --chown=user app.py start.sh ./
COPY --chown=user .streamlit/ .streamlit/
RUN chmod +x start.sh

USER user
EXPOSE 7860
CMD ["./start.sh"]
