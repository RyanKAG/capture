# captureandmail

A Windows client takes a screenshot of the whole screen and POSTs it to the server. The server emails it.
Screenshots are held in memory only. Neither the client nor the server writes them to disk.

## Server (Docker Compose, port 8989)
1. Set up Gmail. You don't need a Google Cloud project or any API registration:
   - Turn on 2-Step Verification at https://myaccount.google.com/security
   - Create an App Password at https://myaccount.google.com/apppasswords
2. Run:

       cp config.env.example .env      # fill in your Gmail address and the App Password
       docker compose up -d --build
       curl http://localhost:8989/health                  # {"ok": true}
       docker compose logs -f                             # "OK: emailed ..." for each screenshot

Other machines reach it at `http://<server-ip>:8989`. Open port 8989 in the server's firewall if needed.
The container is read-only, so it can't write files even by accident.

## API
`POST /send` takes multipart/form-data with a field `file` containing a jpeg, png, gif, webp or bmp image. There's no authentication.

| Status | Meaning |
|---|---|
| `200 {"ok": true, "sha256": "...", "bytes": N}` | the email server accepted the email |
| `400` | missing file or not an image |
| `413` | larger than `MAX_UPLOAD_MB` (default 25) |
| `502` | the email could not be sent (the server already tried 3 times) |

## Client (Windows)
    pip install -r client\requirements.txt
    python client\client.py --server http://192.168.1.50:8989 --once    # test with one screenshot
    python client\client.py --server http://192.168.1.50:8989           # every 10 minutes

The client counts a send as done only when the server returns 200 **and** the server's sha256
matches the screenshot the client took. Otherwise it resends the **same** screenshot
(5 attempts by default, waiting 5s, 10s, 20s and so on). Options: `--interval 600`, `--retries 5`,
`--format jpeg` (much smaller files than png).

To start it automatically at login, use Task Scheduler: set the trigger to "At log on" and the action to
`pythonw.exe C:\path\client\client.py --server http://...`. It must run in the logged-in user's
session. If it runs as a Windows service, the screenshots come out black.

## Tests (no email account needed)
    python3 -m unittest -v
The tests run a real HTTP server and a fake SMTP server in the test process. They cover:
- the image arriving byte-for-byte in the email
- rejecting files that aren't images and files that are too big
- SMTP failures
- nothing being written to disk
- the client retrying until the send succeeds, without sending duplicate emails
- the client giving up after its retries
- the client treating a wrong checksum as a failure
