package main

import (
	"bytes"
	"io"
	"log"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"time"
)

const pythonScript = "capture.py"

func main() {
	http.HandleFunc("GET /capture", HandleCapture)
	log.Println("Starting server on http://localhost:8080")
	log.Fatal(http.ListenAndServe(":8080", nil))
}

func HandleCapture(w http.ResponseWriter, r *http.Request) {
	now := time.Now()
	// put it in a func because time.since won't be defered
	defer func() {
		log.Printf("sent fingerprint image and took %s", time.Since(now))
	}()

	pythonExec := "python3"
	venvPythonPath := filepath.Join("venv", "bin", "python3")
	if runtime.GOOS == "windows" {
		venvPythonPath = filepath.Join("venv", "Scripts", "python.exe")
	}

	pythonExec = "python3"
	if runtime.GOOS == "windows" {
		pythonExec = "python"
	}

	if _, err := os.Stat(venvPythonPath); err == nil {
		log.Println("Found virtual environment python executable at", venvPythonPath)
		venvPythonPath, _ = filepath.Abs(venvPythonPath)
		pythonExec = venvPythonPath
	}

	cmd := exec.Command(pythonExec, pythonScript)

	var stdoutBuf, stderrBuf bytes.Buffer
	cmd.Stdout = &stdoutBuf
	// will get nice python panics with this baby
	cmd.Stderr = &stderrBuf

	err := cmd.Run()
	if err != nil {
		log.Printf("Failed to capture fingerprint %v", err)
		log.Printf("Python stdout:\n%s\n", stdoutBuf.String())
		log.Printf("Python stderr:\n%s\n", stderrBuf.String())
		http.Error(w, "Failed to capture fingerprint: "+stderrBuf.String(), http.StatusInternalServerError)
		return
	}

	if stdoutBuf.Len() == 0 {
		log.Println("Python script returned empty output.")
		http.Error(w, "No data received from Python script", http.StatusInternalServerError)
		return
	}

	// made the capture.py script send png to stdout so no need to handle different image types
	w.Header().Set("Content-Type", "image/png")

	_, err = io.Copy(w, &stdoutBuf)
	if err != nil {
		return
	}

}
