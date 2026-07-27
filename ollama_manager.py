import subprocess
import threading
import time
import os
import signal
import sys

class OllamaManager:
    def __init__(self, log_callback=None):
        self.log_callback = log_callback
        self.process = None
        self.running = False
        self.output_thread = None

    def log(self, msg):
        if self.log_callback:
            self.log_callback(msg)
        else:
            print(f"[OLLAMA_MGR] {msg}")

    def is_ollama_running(self):
        """Checks if Ollama is already running via tasklist."""
        try:
            # Windows-only check; treat any failure as "not running".
            output = subprocess.check_output('tasklist /FI "IMAGENAME eq ollama.exe"', shell=True).decode()
            return 'ollama.exe' in output
        except:
            return False

    def start(self):
        if self.is_ollama_running():
            self.log("Ollama is already running (External Process). Attaching to API...")
            self.running = True
            return

        self.log("Starting local Ollama server...")
        try:
            # Hide the console window Ollama would otherwise pop up.
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

            self.process = subprocess.Popen(
                ['ollama', 'serve'],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                startupinfo=startupinfo,
                encoding='utf-8',
                errors='replace'
            )
            self.running = True

            # Drain stdout so the pipe never fills and blocks the server.
            self.output_thread = threading.Thread(target=self._monitor_output, daemon=True)
            self.output_thread.start()
            self.log("Ollama server process spawned successfully.")

        except FileNotFoundError:
            self.log("ERROR: 'ollama' command not found. Is it installed and in PATH?")
        except Exception as e:
            self.log(f"ERROR: Failed to start Ollama: {e}")

    def _monitor_output(self):
        while self.running and self.process:
            line = self.process.stdout.readline()
            if not line and self.process.poll() is not None:
                break
            if line:
                self.log(line.strip())

        if self.running:
            self.log("Ollama process exited unexpectedly.")
            self.running = False

    def stop(self):
        if self.process:
            self.log("Stopping Ollama server...")
            self.running = False
            try:
                self.process.terminate()
                # Give it a moment to shut down cleanly before forcing.
                self.process.wait(timeout=5)
            except:
                try:
                    self.process.kill()
                except:
                    pass
            self.process = None
            self.log("Ollama server stopped.")
