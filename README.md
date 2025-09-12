# server for fingerprint sensor 

## build

```bash
    docker build -t fingerprint-app .
    # or 
    padman build -t fingerprint-app .
```

## run

```bash
    docker run -p 8080:8080 --device=/dev/ttyUSB0 --name my-fingerprint-app fingerprint-app
    # or 
    padman run -p 8080:8080 --device=/dev/ttyUSB0 --name my-fingerprint-app fingerprint-app
```


