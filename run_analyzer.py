import os, sys, subprocess

# Load OPENROUTER_API_KEY from Hermes .env
env_path = os.path.join(os.environ.get("HERMES_HOME", r"C:\Users\User\AppData\Local\hermes"), ".env")
key_found = False
with open(env_path) as f:
    for line in f:
        line = line.strip()
        if line.startswith("OPENROUTER_API_KEY") and "=" in line:
            key = line.split("=", 1)[1]
            os.environ["OPENROUTER_API_KEY"] = key
            print(f"[wrapper] key loaded: {key[:8]}...", file=sys.stderr)
            key_found = True
            break

if not key_found:
    print("[wrapper] ERROR: key not found in .env", file=sys.stderr)
    sys.exit(1)

# Run the analyzer
os.environ["PYTHONIOENCODING"] = "utf-8"
result = subprocess.run(
    [sys.executable, "G:/LLM/memory/memory_analyzer.py", "--file", "G:/LLM/test/telegram_session.json"],
    env=os.environ,
    capture_output=True,
    text=True,
    timeout=600,
)
print(result.stdout)
if result.stderr:
    print("[stderr]", result.stderr, file=sys.stderr)
sys.exit(result.returncode)
