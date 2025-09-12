#!/bin/bash


LINUX_OUTPUT="build/fingerprint-server-linux"
WINDOWS_OUTPUT="build/fingerprint-server-windows.exe"
SOURCE_FILE="main.go"

echo "Starting build process..."


echo "Compiling for Linux..."
go build -installsuffix cgo -ldflags="-w -s" -o "$LINUX_OUTPUT" "$SOURCE_FILE"
if [ $? -eq 0 ]; then
  echo "Successfully compiled for Linux: $LINUX_OUTPUT"
else
  echo "Error: Failed to compile for Linux."
  exit 1
fi

echo "" 

echo "Compiling for Windows..."
env GOOS=windows GOARCH=amd64 go build -a -installsuffix cgo -ldflags="-w -s" -o "$WINDOWS_OUTPUT" "$SOURCE_FILE"
if [ $? -eq 0 ]; then
  echo "Successfully compiled for Windows: $WINDOWS_OUTPUT"
else
  echo "Error: Failed to compile for Windows."
  exit 1
fi

# just copying the script so I can still use hot reloading and ignore everything in build file
cp capture.py build/capture.py

echo ""
echo "Build process complete."
echo "Executables are located in the current directory."
