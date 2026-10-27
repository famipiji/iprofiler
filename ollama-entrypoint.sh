#!/bin/sh
# Start Ollama in background, pull the configured model, then keep it running
set -u

# Must match the OLLAMA_MODEL the app actually requests chat-completions
# against (see docker-compose.yml / qwen_service.py) - otherwise this script
# provisions a model the app never asked for, and every chat-completion call
# fails with a model-not-found error.
MODEL="${OLLAMA_MODEL:-qwen2.5vl:7b}"

ollama serve &
OLLAMA_PID=$!

# Wait for Ollama to be ready, bounded so a crashed/never-starting server
# doesn't hang this script (and the container startup) forever.
echo "Waiting for Ollama to start..."
READY=0
attempt=0
max_attempts=30
while [ "$attempt" -lt "$max_attempts" ]; do
    if ollama list > /dev/null 2>&1; then
        READY=1
        break
    fi
    attempt=$((attempt + 1))
    sleep 1
done
if [ "$READY" -ne 1 ]; then
    echo "FATAL: Ollama server did not become ready within ${max_attempts}s." >&2
    exit 1
fi

echo "Pulling ${MODEL}..."
if ! ollama pull "${MODEL}"; then
    echo "FATAL: Failed to pull model '${MODEL}'." >&2
    exit 1
fi

echo "Model ready."
wait "$OLLAMA_PID"
