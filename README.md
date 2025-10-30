# support-bridge

simple whatsapp -- zulip bridge running in flask, containerized with gunicorn

## overview
this service bridges whatsapp messages to zulip (and vice versa).  
It also writes transcripts and ticket comments to an RT instance.

## running locally
place .env file in the directory
```bash
docker build -t support-bridge .
docker run --env-file .env -p 8080:5000 -v $(pwd)/data:/app/data support-bridge
```
## port
The flask app listens on port 5000 inside the container.

## proxy configuration

these are the urls you should expose externally to reach the container.

**external → internal mapping**

| public url | internal target | purpose |
|-------------|----------------|----------|
| `https://<domain>/webhook` | `http://localhost:5000/webhook` | whatsapp webhook verify + messages |
| `https://<domain>/webhook/zulip` | `http://localhost:5000/webhook/zulip` | zulip webhook |
| `https://<domain>/health` | `http://localhost:5000/health` | health probe |

