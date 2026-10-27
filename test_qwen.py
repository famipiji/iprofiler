from openai import OpenAI
import sys
import time

client = OpenAI(base_url="http://localhost:1234/v1", api_key="test")

print("[TEST] Sending simple request to Qwen...")
start = time.time()

try:
    response = client.chat.completions.create(
        model="qwen3-vl-2b-instruct",
        messages=[{"role": "user", "content": "Say hello"}],
        max_tokens=10,
    )
    elapsed = time.time() - start
    print(f"[TEST] Response received in {elapsed:.1f}s")
    print(f"[TEST] Content: {response.choices[0].message.content}")
except Exception as e:
    elapsed = time.time() - start
    print(f"[TEST] Error after {elapsed:.1f}s: {e}")
    # Standalone diagnostic script (run directly) - without this, a
    # completely unreachable LM Studio endpoint still exits 0, so anything
    # checking the exit code (a shell script, CI step) sees a false pass.
    sys.exit(1)
