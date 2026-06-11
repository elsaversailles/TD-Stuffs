import subprocess
import requests
import json
import shlex

OLLAMA_URL = "http://localhost:11434/api/generate"

def ask_llm(prompt, model="LiquidAI/lfm2.5-350m"):
    resp = requests.post(OLLAMA_URL, json={
        "model": model,
        "prompt": prompt,
        "stream": False
    })
    return resp.json()["response"]

def run_nmap(target, flags="-sV"):
    """Run nmap with given flags. Target must be validated first."""
    # Basic input validation — never trust LLM output directly
    if any(c in target for c in [";", "|", "&", "`"]):
        raise ValueError("Suspicious characters in target")

    cmd = f"nmap {shlex.quote(flags)} {shlex.quote(target)}"
    print(f"[*] Running: {cmd}")  # Always show what's being executed

    result = subprocess.run(
        shlex.split(cmd),
        capture_output=True, text=True, timeout=300
    )
    return result.stdout

# Example workflow
target = "scanme.nmap.org"  # Your authorized test target
scan_output = run_nmap(target)

analysis = ask_llm(
    f"Analyze this Nmap scan output. Identify open services, "
    f"potential vulnerabilities, and suggest next steps.\n\n{scan_output}"
)
print(analysis)
